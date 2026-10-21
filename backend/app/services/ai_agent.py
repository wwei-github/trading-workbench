"""Trader Agent：GLM 工具循环 + submit_decision 校验即工具（docs/04 §5 P1）

- 事实包（信号/摆动结构/EMA/ATR/量能）随首条 user 消息给足，程序能算的不让模型算
- 工具只做"补充查询"：更多K线、资金费率、大盘、恐贪、技能阅读
- 结构位锚点：模型不报绝对价格，报 (ref, offset_pct)，程序换算到摆动结构位后过
  Risk Guard——消灭幻觉价位（2026-09-14 起锚点解析自 recent_swings，docs/09）
- 校验即工具：submit_decision 内嵌 Risk Guard，违规明细作为工具结果返回，模型看着错处改（Reflection）
- spke 已验证 GLM tool-calling 稳定（3/3，2 轮收敛），故手写循环不引入框架
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from app.config import settings
from app.services import market_data
from app.services import skill_library
from app.services.llm_client import get_client, stream_completion
from app.services.risk_guard import (
    TRADE_TYPES,
    TradeDecision,
    build_forced_skip,
    validate_decision,
)
from app.services.strategy.indicators import calc_atr
from app.services.strategy.technical import atr_series
from app.services.ai_analyzer import (
    _RULE_BTC_ENV,
    _RULE_SL,
    SIGNAL_TYPE_LABELS,
    _RULE_TP_BASE,
    build_signal_facts,
    render_rule_score,
    render_rule_struct,
    render_rule_vote,
    render_signal_gate_block,
    render_tp1_cap_desc,
)

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5

# ── 上下文预算（2026-10-16）─────────────────────────────────────────
# messages 只增不减：每轮工具结果最长 12000 字符（get_recent_klines n=200 ≈11k），
# 5 轮累积可逼近模型上下文上限。超预算时把较早的 tool 结果压成头尾摘要，保留最近
# 若干条原文（决策链依赖最新数据；旧查询结果已可重查）。
_CONTEXT_BUDGET_CHARS = 120_000   # 消息正文总字符预算（近似 token×2.5 的保守口径）
_TOOL_RESULT_MAX_CHARS = 12_000   # 单条工具结果回填上限（原有口径）
_TOOL_RESULT_TRIM_CHARS = 1_200   # 裁剪后保留的头+尾字符数
_TOOL_RESULT_KEEP = 4             # 裁剪时保留原文的最近工具结果条数

# AGENT_SYSTEM 内嵌片段：公共纪律段的 P1 适配版（P1 落库字段名是 reason，
# P0 是 analysis）——显式参数化渲染，无 replace/手工转义（2026-10-16）
_RULE_VOTE_BRIEF = render_rule_vote("reason")
_RULE_STRUCT_BRIEF = render_rule_struct("reason")


def _render_shared_sl_tp(rr_min: float) -> tuple[str, str]:
    """P1 复用 P0 的止损/止盈纪律段（2026-10-21 T3-3）。

    此前 P1 手写第 11/12 条，与 P0 的 `_RULE_SL` / `compose_rule_tp` 双份维护，
    改一处漏一处；抽出渲染器后两管线同源，P1 只额外声明「价格用锚点表达」。
    止盈只取公共段 `_RULE_TP_BASE`：止盈二与三情形兜底已由 P1 专用的
    `_TP2_RULE_ON/OFF`（锚点语言）完整覆盖，再拼 P0 的 TP2 条目会重复占 token。
    """
    far = round(settings.TP1_MAX_ATR / rr_min, 2)
    sl = _RULE_SL.format(far_atr=far)
    tp = _RULE_TP_BASE.format(
        rr_min=rr_min,
        tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
        tp1_cap_desc=render_tp1_cap_desc(rr_min),
    )
    return sl, tp

# 近锚降级链（docs/23，2026-09-29，仅突破类信号注入；P1 版：锚点用 signal_low/signal_high
# 表达，stop_anchor 由程序从 stop_ref 推导、无需自报）。近锚只救"万事俱备、只差盈亏比"
# 的突破单，其余条件任一不满足时无近锚资格
NEAR_STOP_RULE_AGENT = """11.5 突破类止损近锚降级链：结构锚优先；按结构锚复算 RR <{rr_min} 且调入场价仍不达标
    （本应 skip）时，才降级近锚——stop_ref=signal_low（多）/signal_high（空）
    + offset_pct=-0.3，止盈按新锚复算；仍不达标 → skip。硬约束：仅当 skip 唯一原因是
    RR 不达标（其余开单条件全满足）才可用近锚；量比 <1 无资格；近锚后 RR >4 需重估或
    skip；止损宽度仍须 ≥0.3×ATR。{stop_cap_clause}结构锚宽度已超硬上限时无资格，直接
    skip。程序校验近锚须贴近金K窗口极值 ±0.5%。结构锚距入场超 11 条分档线时直接降级
    金K锚（不受"RR 唯一不达标"前提约束）。"""

def render_system_prompt(
    risk_label: str = "", rr_min: Optional[float] = None,
    tp2_enabled: bool = True, min_recommendation: Optional[float] = None,
    consecutive_losses: Optional[int] = None, trigger_type: Optional[str] = None,
) -> str:
    """渲染最终生效的 P1 系统提示词（页面「查看当前生效提示词」与实际调用同源）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案（如"固定 3 USDT"/
    "账户风险基数 × 3%"），空时回落默认百分比口径。
    near_stop_rule 按突破类注入完整文案（预览无信号上下文，展示通用版）。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，system_config.ai_rr_min 覆盖时
    由调用方传入；None 回落 env 默认。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时剔除止盈二锚点语义
    （system_config.tp2_enabled，默认关），与实际调用（analyze_coin_agent）同源。
    min_recommendation（2026-10-21）：AI 分析阶段静态评分门槛（system_config 优先、None 回落 env）；
    连损动态门槛仍由执行层按账号单独校验，不作为统一分析门槛。"""
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _tp1_atr = f"{settings.TP1_MAX_ATR:g}"
    _tp1_r = f"{settings.TP1_MAX_R:g}"
    _far_atr = round(settings.TP1_MAX_ATR / _rr, 2)
    _risk_label = risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%"
    _sl_agent, _tp_agent = _render_shared_sl_tp(_rr)
    return AGENT_SYSTEM.format(
        max_rounds=MAX_ROUNDS, _ref_desc=_REF_DESC,
        rr_min=_rr,
        sl_min_atr=f"{settings.SL_MIN_ATR:g}",
        tp1_max_atr=_tp1_atr,
        tp1_max_r=_tp1_r,
        tp1_cap_desc=render_tp1_cap_desc(_rr),
        far_atr=_far_atr,
        risk_label=_risk_label,
        _RULE_SCORE=render_rule_score(
            min_recommendation, _rr, _risk_label,
            consecutive_losses=consecutive_losses,
            trigger_type=trigger_type,
        ),
        _RULE_BTC_ENV=_RULE_BTC_ENV,
        _RULE_VOTE_BRIEF=_RULE_VOTE_BRIEF,
        _RULE_STRUCT_BRIEF=_RULE_STRUCT_BRIEF,
        near_stop_rule=NEAR_STOP_RULE_AGENT.format(
            rr_min=_rr,
            stop_cap_clause="止损宽度硬上限值以信号门槛块为准。",
        ),
        _RULE_SL_AGENT=_sl_agent,
        _RULE_TP_AGENT=_tp_agent,
        _price_anchor_desc=_PRICE_ANCHOR_ON if tp2_enabled else _PRICE_ANCHOR_OFF,
        _tp_anchor_desc=_TP_ANCHOR_DESC_ON if tp2_enabled else _TP_ANCHOR_DESC_OFF,
        _tp2_rule=(_TP2_RULE_ON if tp2_enabled else _TP2_RULE_OFF).format(
            rr_min=_rr, tp1_max_atr=_tp1_atr, tp1_max_r=_tp1_r,
            tp1_cap_desc=render_tp1_cap_desc(_rr),
            tp2_gap_atr=f"{settings.TP2_MIN_GAP_ATR:g}",
            tp2_gap_r=f"{settings.TP2_MIN_GAP_R:g}",
        ),
        skill_index="",
    )

# 锚点枚举：结构位两类（support/resistance，解析到现价下/上方最近的摆动点）
# + 线外锚点（做多止盈锚摆动高点下方、做空锚摆动低点上方，枚举名沿用 zone
# 时期历史命名）+ market（市价锚）。next_ 前缀取更前一档（止盈二用）。
# signal_low/signal_high（docs/23，2026-09-29；2026-10-12-5 起兼作距离分档
# 降级金K锚，全信号类型可用；2026-10-12-8 恢复「最近5根已收盘K线共同极值」
# 窗口口径，见 _resolve_price）：金K窗口极值，仅止损锚用
# （stop_ref 命中即推导 stop_anchor=near）
LEVEL_REFS = (
    "support", "resistance", "market",
    "resistance_zone_low", "support_zone_high",
    "next_resistance_zone_low", "next_support_zone_high",
    "signal_low", "signal_high",
)

_REF_DESC = "、".join(LEVEL_REFS)

AGENT_SYSTEM = """你是加密货币合约交易决策 Agent。事实包已随消息给出，规则如下：
4h 背景以事实包数据行给出（state/macro），仅作趋势强弱与风险参考，不限制多空方向。
1. 程序能算的不让你算——事实包直接用；需补充数据才调工具，最多 {max_rounds} 轮。
2. **禁止编造绝对价格**：价格一律用结构位锚点表达{_price_anchor_desc}，
   ref 取值: {_ref_desc}；offset_pct 为相对锚点的百分比偏移。
   {_tp_anchor_desc}
3. 决策必须通过 submit_decision 提交；校验不通过时工具返回违规明细，按明细修正后重提。
4. **入场价纪律**：只允许现价入场（entry 用 market 锚，offset≈0），**禁止**放到现价
   回踩侧（多单低于现价/空单高于现价）；RR 不达标 → 直接 skip，**不得给回踩等待计划**。
5. 开单类型归类（trade_type，suggest 必填其一）：trend_follow 顺势（纯 EMA 排列不是硬门槛）/
   structure_break 结构破位回踩。
6. 标注【当前命中】的技能建议先 load_skill 再决策。
7. **效率与数据边界**：事实包含 40 根已收盘K线/摆动/均线/ATR/指标/4h背景，首轮即可
   submit_decision；资金费率、大盘、恐贪**不在**包内，确需时同一轮批量调用
   get_funding / get_market_breadth / get_fear_greed。
8. 不要输出不带工具调用的纯文字回复；文字只用于简短说明查询意图。
9. 盈亏比铁律 ≥{rr_min}（无豁免）；止损宽度见第 11 条；仓位由程序按固定亏损法
   计算（预算 ÷ 止损距离），单笔止损预算={risk_label}。
10. 方向票以事实包「方向票」行为准（MACD/布林/RSI 同向≥2），reason 第一条写明
    "方向票: 看多N项 vs 看空M项"；SuperTrend/ADX/OBV/4h背景不计票；回踩单方向票仅作确认度参考。
11. 止损锚定（P0 同源；stop_ref+offset_pct 落点须满足下式）：
{_RULE_SL_AGENT}
{near_stop_rule}
12. 止盈锚定（P0 同源；tp_ref+offset_pct 落点须满足下式）：
{_RULE_TP_AGENT}
{_tp2_rule}
12.5 分析格式（reason 落库）：两节、标题逐字含冒号、每条独占一行（JSON 内用 \n）：
    「公共检查：」1. 4h方向 2. 方向票 3. 回踩触点 4. 形态与量能 5. 风险点（无则写"无"）；
    「决策结论：」分条（1. 2. 3.），末行"结论：建议开单/不建议开单"；skip 同样完整两节。
13. 方向纪律：优先支撑做多、压力做空；离开结构位时按动能与盈亏比判断
    （breakout 用 market 锚），不做"待回踩"计划。

{_RULE_SCORE}

{_RULE_BTC_ENV}

{_RULE_STRUCT_BRIEF}

{skill_index}"""


# TP2 分批开关（2026-10-08）槽位变体：关闭时提示词整体剔除止盈二锚点语义，
# 与 P0 管线（ai_analyzer.compose_rule_tp）同目的——AI 不产出第二档止盈价。
# 含 {rr_min}/{tp1_max_atr}/{tp1_max_r} 的变体在渲染前须先显式 .format 再传入 AGENT_SYSTEM.format。
_PRICE_ANCHOR_ON = "（entry_ref/stop_ref/tp1_ref/tp2_ref + offset_pct）"
_PRICE_ANCHOR_OFF = "（entry_ref/stop_ref/tp1_ref + offset_pct）"

_TP_ANCHOR_DESC_ON = """止盈用线外锚点：做多 tp1_ref=resistance_zone_low、止盈二 tp2_ref=next_resistance_zone_low
   （现价上方 >1×ATR 的第一档及其更远一档摆动高点）；做空用 support_zone_high、
   止盈二 tp2_ref=next_support_zone_high。offset 留余地：做多 -0.2~-0.5，做空 0.2~0.5。"""

_TP_ANCHOR_DESC_OFF = """止盈用线外锚点：做多 tp1_ref=resistance_zone_low（现价上方 >1×ATR 的第一档摆动高点）；
   做空用 support_zone_high。offset 留余地：做多 -0.2~-0.5，做空 0.2~0.5。"""

_TP2_RULE_ON = """止盈二取止盈一锚定档的下一档同类结构位（做多=更高一档摆动高点下方，
    做空=更低一档摆动低点上方），须比止盈一更远。前方无结构位（如创新高）时
    止盈二不设（仅一档），止盈一 = 入场 ± {rr_min}×止损距离；第一档结构位存在但
    距入场超过 {tp1_cap_desc} 时，止盈二保留锚定该远端（做多 tp2_ref=resistance_zone_low，
    做空 tp2_ref=support_zone_high，即止盈一因过远弃用的那一档，作趋势跑手档）。
    间距 ≥ max({tp2_gap_atr}×ATR, {tp2_gap_r}×止损距离)，不足/贴档/反向时
    止盈二不设（仅一档），程序亦会静默撤档。"""

_TP2_RULE_OFF = """当前未启用止盈二（TP2 已关闭）：不要提交 tp2_ref/tp2_offset_pct
    （提交也会被置 0），止盈一只挂第一档结构位；无结构位或第一档距入场超过
    {tp1_cap_desc} 时，止盈一 = 入场 ± {rr_min}×止损距离。"""

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "get_recent_klines",
        "description": "获取该币种最近 N 根已收盘K线（事实包里已有60根摘要，需要更长历史时用）。列与事实包相同：timestamp,open,high,low,close,vol,quote_vol(USDT)",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "根数，30~200"},
        }, "required": ["n"]},
    }},
    {"type": "function", "function": {
        "name": "get_funding",
        "description": "获取该币种当前资金费率与持仓量",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_market_breadth",
        "description": "获取大盘状态（BTC/ETH 的 EMA 形态与 24h 涨跌幅）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_fear_greed",
        "description": "获取恐惧贪婪指数",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "阅读一个打法技能的完整内容（索引中标注【当前命中】的建议先读）",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名，见索引"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "submit_decision",
        "description": "提交最终决策（唯一出口，程序风控校验）。价格全部用锚点表达。",
        "parameters": {"type": "object", "properties": {
            "trade_decision": {"type": "string", "enum": ["suggest", "skip"]},
            "skip_reason": {"type": "string", "description": "skip 时必填，必须以类别前缀开头（[RR不足]/[止损过宽]/[环境逆风]/[形态质量]/[结构不清]/[波动极端]/[其他] 之一），前缀后 1. 2. 3. 序号列原因，最多3条"},
            "direction": {"type": "string", "enum": ["long", "short"], "description": "suggest 时必填"},
            "trade_type": {
                "type": "string",
                "enum": list(TRADE_TYPES),
                "description": "开单类型归类，suggest 时必填：trend_follow 顺势交易 / "
                               "structure_break 结构破位回踩（123·N字 合并）",
            },
            "entry_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "入场锚点"},
            "entry_offset_pct": {"type": "number", "description": "入场相对锚点偏移%，默认0"},
            "stop_ref": {"type": "string", "enum": list(LEVEL_REFS),
                         "description": "止损锚点。signal_low/signal_high=金K窗口（最近5根已收盘K线，含本信号金K/突破K线）共同最低/最高价，仅用于金K极值锚——距离分档降级（规则11，远结构锚超距离分档线时）或突破类近锚降级链（11.5），其余情况用结构锚点"},
            "stop_offset_pct": {"type": "number", "description": "止损相对锚点偏移%（通常负/反向）"},
            "tp1_ref": {"type": "string", "enum": list(LEVEL_REFS),
                        "description": "止盈1锚点：做多=resistance_zone_low（更远的第一条压力位价格线，距现价≤1×ATR的争夺位线不算），做空=support_zone_high（更远的第一条支撑位价格线）；signal_low/signal_high 仅限止损近锚，不用于止盈"},
            "tp1_offset_pct": {"type": "number",
                               "description": "止盈1相对锚点偏移%，留余地：做多 -0.2~-0.5（线下方），做空 0.2~0.5（线上方）"},
            "tp2_ref": {"type": "string", "enum": list(LEVEL_REFS),
                        "description": "止盈2锚点（可选）：做多=next_resistance_zone_low（下一档压力线），做空=next_support_zone_high（下一档支撑线）"},
            "tp2_offset_pct": {"type": "number", "description": "止盈2相对锚点偏移%，同止盈1留余地"},
            "recommendation": {"type": "integer", "description": "推荐度档位（不内插）：skip≤30 / 60=达标 / 70=标准共振 / 85=强共振；低于生效门槛=程序不下单；禁止用 50-59 表达保守（细则见系统提示词「评分纪律」）"},
            "tactic_name": {"type": "string",
                            "description": "战法命中名（仅借鉴标注）：用户消息出现【战法库判定】块且信号符合某战法时逐字输出该名，均不符合输出空字符串"},
            "tactic_reason": {"type": "string", "description": "战法判定依据 ≤60字；未命中输出空字符串"},
            "reason": {
                "type": "string",
                "description": (
                    "决策分析，≤300字中文，直接作为分析结果落库展示。按固定节标题两节输出、"
                    "每条一行（JSON 字符串内换行写\\n，节标题逐字含冒号）："
                    "公共检查：（1. 4h方向 2. 方向票 3. 回踩触点 4. 形态与量能 5. 风险点）→"
                    "决策结论：（按条分行，以 1. 2. 3. 序号逐条列满足/不满足什么，"
                    "最后单独一行\"结论：建议开单/不建议开单\"收尾，禁止写成一行长句，"
                    "见系统规则12.5）"
                ),
            },
        }, "required": ["trade_decision"]},
    }},
]


def _tools_schema(tp2_enabled: bool = True) -> list:
    """submit_decision 工具 schema 按 TP2 开关过滤（2026-10-08）：
    关闭时对模型隐藏 tp2_ref/tp2_offset_pct 参数——模型无法提交止盈二。
    返回深拷贝，不污染模块级 TOOLS_SCHEMA 常量。"""
    if tp2_enabled:
        return TOOLS_SCHEMA
    import copy
    tools = copy.deepcopy(TOOLS_SCHEMA)
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") == "submit_decision":
            props = (fn.get("parameters") or {}).get("properties") or {}
            props.pop("tp2_ref", None)
            props.pop("tp2_offset_pct", None)
    return tools


# 线外锚点：ref -> (swings 侧, 第几档)；第几档 0=现价侧第一档、1=下一档（止盈二用）。
# 锚点解析到摆动结构位价格本身（docs/09），枚举名沿用 zone/关键位时期历史命名。
# 2026-10-12：第几档在给定 ATR 时先剔除距现价 ≤1×ATR 的"争夺位"（见 _nth_swing），
# 与工具 schema 描述"更远的第一条压力/支撑线"对齐
_ZONE_REF_MAP = {
    "resistance_zone_low": ("highs", 0),
    "support_zone_high": ("lows", 0),
    "next_resistance_zone_low": ("highs", 1),
    "next_support_zone_high": ("lows", 1),
}


def _nth_swing(signal: dict, side: str, nth: int,
               atr: Optional[float] = None) -> Optional[float]:
    """现价侧第 nth 近的摆动结构位价格：highs 取现价上方升序（最近为 0），
    lows 取现价下方降序（最近为 0）。数据缺失返回 None。

    atr（2026-10-12）：给定时先剔除距现价 ≤atr（即 ≤1×ATR）的"争夺位"——它们只
    回答是否被测试/突破，不作止盈目标（与工具 schema/提示词同口径）；剔除后取第
    nth 档。atr 缺失（无 K 线）时不剔除，回落原"最近位"语义（旧行为不变）。"""
    sw = signal.get("recent_swings") or {}
    try:
        cur = float(signal.get("current_price") or 0)
    except (TypeError, ValueError):
        cur = 0.0
    if cur <= 0:
        return None
    prices = sorted(
        (float(s["price"]) for s in (sw.get(side) or []) if float(s.get("price", 0) or 0) > 0),
        reverse=(side == "lows"),
    )
    prices = [p for p in prices if (p > cur if side == "highs" else p < cur)]
    if atr and atr > 0:
        prices = [p for p in prices if abs(p - cur) > atr]
    if len(prices) <= nth:
        return None
    return prices[nth]


def _resolve_price(ref: Optional[str], offset_pct, signal: dict,
                   purpose: str = "entry", klines: Optional[list] = None) -> Optional[float]:
    """锚点 + 偏移 → 绝对价格；非法/未知锚点返回 None。

    结构锚点一律解析到摆动结构位（docs/09 口径）。ref 必须在 LEVEL_REFS
    枚举内——关键位时代的 key_ 前缀锚点已移除，模型沿旧习惯报出时按非法
    锚点打回（走 _handle_submit 的 errs 重试路径），而非静默落到错误侧。
    signal_low/signal_high（docs/23；2026-10-12-8 恢复窗口口径）解析到「金K窗口」
    =最近 5 根已收盘K线（klines[-6:-1]，含信号K线 klines[-2]；klines[-1] 未收盘
    不参与）的共同极值——做多取 5 根最低点、做空取 5 根最高点，仅止损金K锚使用。
    klines（2026-10-12）：zone 系止盈锚据此算 1×ATR，剔除距离 ≤1×ATR 的争夺位
    （见 _nth_swing）；未传 K 线时不做该剔除（旧行为）。
    purpose 保留参数以维持调用方签名稳定。"""
    if not ref:
        return None
    if ref == "market":
        base = float(signal.get("current_price") or 0)
        return base * (1 + _safe_off(offset_pct) / 100.0)
    if ref not in LEVEL_REFS:
        return None

    if ref in ("signal_low", "signal_high"):
        # 金K窗口极值锚（2026-10-12-8）：取最近 5 根已收盘K线（klines[-6:-1]，
        # 含信号K线 klines[-2]；klines[-1] 未收盘不参与）的共同极值——只取信号K
        # 单根时，前一根更低/更高会把止损压进近期波动区间内部、必被扫损
        # （C98USDT.P 案例）。数据不足/极值非法视为非法（走 errs 重试路径），不静默落 0
        ks = klines or []
        window = ks[max(0, len(ks) - 6):len(ks) - 1]
        idx = 3 if ref == "signal_low" else 2  # low / high
        vals = []
        for c in window:
            try:
                v = float(c[idx])
            except (IndexError, TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
        if not vals:
            return None
        base = min(vals) if ref == "signal_low" else max(vals)
        return base * (1 + _safe_off(offset_pct) / 100.0)

    cur = float(signal.get("current_price") or 0)
    # zone 系止盈锚：有 K 线时算 1×ATR 并剔除近端争夺位（2026-10-12）。
    # 2026-10-12-3 统一口径：改用 atr_series（Wilder 平滑）而非 calc_atr
    # （简单均值），与 risk_guard 的"止损宽度下限 / 止盈一距离上限"同源，
    # 避免模型与校验器对"1×ATR"边界判断不一致
    _atr = None
    if ref in _ZONE_REF_MAP and klines:
        try:
            arr = atr_series(klines)
            _atr = float(arr[-1]) if arr is not None and np.isfinite(arr[-1]) else None
        except Exception:
            _atr = None
    base = _resolve_from_swings(ref, signal, cur, atr=_atr)
    if base is None:
        return None
    return base * (1 + _safe_off(offset_pct) / 100.0)


def _safe_off(offset_pct) -> float:
    try:
        return float(offset_pct or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_from_swings(ref: str, signal: dict, cur: float,
                         atr: Optional[float] = None) -> Optional[float]:
    """摆动点路径：结构锚点解析到现价同侧最近的摆动点。

    zone 系（止盈锚）在给定 atr 时先剔除 ≤1×ATR 争夺位（见 _nth_swing）；
    support/resistance（止损结构锚）不受该剔除影响，保持"最近摆动点"语义。"""
    if ref in _ZONE_REF_MAP:
        side, nth = _ZONE_REF_MAP[ref]
        return _nth_swing(signal, side, nth, atr=atr)
    sw = signal.get("recent_swings") or {}
    side = "lows" if ref == "support" else "highs"
    prices = [
        float(s.get("price", 0))
        for s in (sw.get(side) or [])
        if float(s.get("price", 0)) > 0
    ]
    if cur > 0:
        prices = [p for p in prices
                  if (p < cur if side == "lows" else p > cur)]
    if not prices:
        return None
    return max(prices) if side == "lows" else min(prices)


def _handle_submit(
    args: dict, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
    rr_min: Optional[float] = None,
    risk_pct: Optional[float] = None,
    tp2_enabled: bool = True,
    rr_tol: Optional[float] = None,
) -> tuple[bool, str, Optional[dict]]:
    """submit_decision：锚点换算 → Risk Guard。返回 (done, tool_result, fixed)

    risk_budget（2026-09-27 止损金额模式）：fixed 模式预算实额，透传校验。
    战法字段（2026-10-02）：随 args 携带，白名单闸门在 validate_decision。
    tp2_enabled（2026-10-08）：关闭时 tp2 参数一律置 0（提示词与工具 schema
    已隐藏止盈二，此处兜底防模型沿旧习惯提交）。"""
    decision = args.get("trade_decision")
    if decision not in ("suggest", "skip"):
        return False, "trade_decision 必须是 suggest 或 skip", None

    # 战法字段预归一（空串→None；白名单校验在 validate_decision/_tactic_gate）
    _tname = str(args.get("tactic_name") or "").strip()[:64] or None
    _treason = str(args.get("tactic_reason") or "").strip()[:255] or None

    if decision == "skip":
        if not (args.get("skip_reason") or "").strip():
            return False, "skip 必须给出 skip_reason（1. 2. 3. 序号原因）", None
        fixed = {
            "trade_decision": "skip",
            "skip_reason": args["skip_reason"].strip(),
            "direction": None,
            "trade_type": None,
            "analysis": "",
            "tactic_name": _tname,
            "tactic_reason": _treason,
            "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
            "take_profit_2": 0, "risk_reward_ratio": 0,
            "position_pct": 0,
            "recommendation": min(float(args.get("recommendation") or 10), 30),
        }
        return True, "决策已提交：skip", fixed

    # suggest：锚点换算（结构锚点一律解析到摆动结构位，docs/09 口径）
    entry = _resolve_price(args.get("entry_ref"), args.get("entry_offset_pct"),
                           signal, purpose="entry")
    stop = _resolve_price(args.get("stop_ref"), args.get("stop_offset_pct"),
                          signal, purpose="stop", klines=klines)
    tp1 = _resolve_price(args.get("tp1_ref"), args.get("tp1_offset_pct"),
                         signal, purpose="tp", klines=klines)
    # TP2 关闭硬清零（2026-10-08）：不解析 tp2 锚点，落库 take_profit_2=0
    tp2 = (
        _resolve_price(args.get("tp2_ref"), args.get("tp2_offset_pct"),
                       signal, purpose="tp", klines=klines)
        if tp2_enabled else 0
    )
    errs = []
    if entry is None:
        errs.append(f"entry_ref 非法（{_REF_DESC} 中选择）")
    if stop is None:
        errs.append(f"stop_ref 非法")
    if tp1 is None:
        errs.append("tp1_ref 非法")
    if errs:
        return False, "锚点换算失败：" + "；".join(errs) + "。请改用有效锚点重新提交。", None

    d = TradeDecision(
        trade_decision="suggest",
        direction=args.get("direction"),
        trade_type=args.get("trade_type"),
        # 止损锚类型程序推导（docs/23）：stop_ref 用信号K线极值锚即 near，
        # 其余（含缺省）structure——比 AI 自报更难滥用
        stop_anchor="near" if args.get("stop_ref") in ("signal_low", "signal_high") else "structure",
        tactic_name=_tname,
        tactic_reason=_treason,
        entry_price=entry, stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2 or 0.0,
        recommendation=float(args.get("recommendation") or 60),
        analysis=args.get("reason") or "",
    )
    ok, violations, fixed = validate_decision(
        d, signal, klines, risk_budget=risk_budget,
        rr_min=rr_min, risk_pct=risk_pct, rr_tol=rr_tol,
    )
    if ok:
        return True, "决策已提交并通过风控校验", fixed
    return False, (
        "风控校验未通过：\n" + "\n".join(f"- {v}" for v in violations)
        + "\n请按违规明细修正（调整锚点/offset/方向）后重新提交。"
    ), None


def _dispatch_tool(name: str, args: dict, ctx: dict) -> tuple[bool, str, Optional[dict]]:
    """执行工具。返回 (done, result_text, fixed_decision)"""
    if name == "get_recent_klines":
        n = max(30, min(int(args.get("n", 100)), 200))
        closed = ctx["klines"][: len(ctx["klines"]) - 1]  # 去掉未收盘
        rows = closed[-n:] if len(closed) > n else closed
        # 与事实包同列的 CSV（紧凑，200根 ≈ 11k 字符，在工具结果截断上限内）
        body = "\n".join(
            f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}"
            for k in rows
        )
        return False, "ts,open,high,low,close,vol,quote_vol(USDT)\n" + body, None
    if name == "get_funding":
        f = market_data.get_funding(ctx["signal"]["symbol"])
        return False, json.dumps(f or {"error": "不可用"}, ensure_ascii=False), None
    if name == "get_market_breadth":
        b = market_data.get_market_breadth(ctx["pool"])
        return False, json.dumps(b or {"error": "不可用"}, ensure_ascii=False), None
    if name == "get_fear_greed":
        fg = market_data.get_fear_greed()
        return False, json.dumps(fg or {"error": "不可用"}, ensure_ascii=False), None
    if name == "load_skill":
        return False, skill_library.load_skill(str(args.get("name", ""))), None
    if name == "submit_decision":
        return _handle_submit(
            args, ctx["signal"], ctx["klines"], risk_budget=ctx.get("risk_budget"),
            rr_min=ctx.get("rr_min"), risk_pct=ctx.get("risk_pct"),
            tp2_enabled=ctx.get("tp2_enabled", True),
            rr_tol=ctx.get("rr_tol"),
        )
    return False, f"unknown tool: {name}", None


# ── 流式工具调用访问器（llm_client.stream_completion 聚合出的 OpenAI 形状 dict）──
def _tc_name(tc: dict) -> str:
    return ((tc.get("function") or {}).get("name")) or ""


def _tc_args(tc: dict) -> str:
    return ((tc.get("function") or {}).get("arguments")) or "{}"


def _trim_context(
    messages: list,
    budget: int = _CONTEXT_BUDGET_CHARS,
    keep_recent: int = _TOOL_RESULT_KEEP,
) -> int:
    """上下文预算裁剪：正文总字符超预算时，把较早的 tool 结果压成头尾摘要。

    只裁 tool 消息（system/user/assistant 是决策链上下文，裁掉会破坏语义），
    保留最近 keep_recent 条原文（决策依赖最新查询结果）。返回裁剪条数（0=未触发）。
    """
    total = sum(len(m.get("content") or "") for m in messages)
    if total <= budget:
        return 0
    idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    older = idxs[:-keep_recent] if keep_recent > 0 else idxs
    trimmed = 0
    half = _TOOL_RESULT_TRIM_CHARS // 2
    for i in older:
        c = messages[i].get("content") or ""
        if len(c) <= _TOOL_RESULT_TRIM_CHARS:
            continue
        messages[i]["content"] = (
            f"{c[:half]}\n…（结果过长已裁剪 {len(c)} 字符，如需完整数据请重新调用工具）…\n"
            f"{c[-half:]}"
        )
        trimmed += 1
        total = sum(len(m.get("content") or "") for m in messages)
        if total <= budget:
            break
    return trimmed


def _summarize_trace(trace: list) -> str:
    """耗尽兜底的轮次摘要（写在 forced skip 理由后，供复盘定位"卡在哪一轮"）。"""
    parts = []
    for st in trace:
        tools = "、".join(st.get("tools") or []) or "空转未调工具"
        parts.append(f"R{st.get('round')} {tools}")
    return "（轮次：" + "；".join(parts) + "）" if parts else ""


def analyze_coin_agent(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    pool=None,
    progress_cb=None,
    htf_klines: Optional[list] = None,
    risk_mode: str = "pct", risk_amount: float = 0.0, risk_label: str = "",
    stop_cap_pct: Optional[float] = None, tactics_block: str = "",
    rr_min: Optional[float] = None, risk_pct: Optional[float] = None,
    tp2_enabled: bool = True, flt=None,
    rr_tol: Optional[float] = None, min_recommendation: Optional[float] = None,
    consecutive_losses: Optional[int] = None,
) -> dict:
    """Agent 循环入口：返回与 Risk Guard 相同结构的决策 dict（可直接落库）。

    返回值额外带 "stage_trace" 键（工具循环 trace，docs/04 §5.9），落库时一并存储。
    progress_cb: 可选回调，接收 {"t": "round"/"tool", ...} 进度事件供前端流式展示
    （每轮工具执行前推 round=轮次+tools，每次工具调用推 tool=工具名+args）。
    risk_*（2026-09-27 止损金额模式）：调用方（ai_tasks）从 system_config 解析的
    预算口径，注入提示词 risk_label 槽位 + submit_decision 校验的 risk_budget；
    缺省时回落百分比口径（pct），与历史行为一致。
    stop_cap_pct（2026-09-29）：止损宽度硬上限（max_stop_pct 补丁参数，比例；
    None=不限制），注入门槛块与近锚规则供 AI 自验。
    tactics_block（2026-10-02）：战法库判定块（调用方预渲染；空=无战法不注入）。
    rr_min / risk_pct（2026-10-02 上页面）：盈亏比铁律下限与单笔风险比例，
    注入提示词并透传 submit_decision 校验（None=env 默认）。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时提示词剔除止盈二锚点语义、
    submit_decision 工具 schema 隐藏 tp2_* 参数、提交的 tp2 硬清零
    （system_config.tp2_enabled，默认关）。
    flt（2026-10-12）：筛选参数组（resolve_scan_filters(cfg)），透传门槛块
    渲染与页面配置同源；None=env/代码默认。
    min_recommendation（2026-10-21）：AI 分析阶段静态评分门槛（system_config 优先、None 回落 env）；
    连损动态门槛仍由执行层按账号单独校验，不作为统一分析门槛。"""
    client = get_client()

    # 布尔事实（技能 use_when 判定）
    atr = calc_atr(klines) or 0.0
    facts = _build_facts(signal, klines, atr)

    skill_index = skill_library.render_index(facts)
    # 止损宽度硬上限子句（2026-09-29）：配置了 max_stop_pct 才注入具体数值
    stop_cap_clause = (
        f"程序止损宽度硬上限 {stop_cap_pct * 100:g}%（见信号门槛块），"
        if stop_cap_pct is not None and stop_cap_pct > 0 else ""
    )
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _tp2_on = bool(tp2_enabled)
    _sl_agent, _tp_agent = _render_shared_sl_tp(_rr)
    # 近锚降级链仅突破类注入（docs/23）；回踩/其他类槽位留空
    near_stop_rule = (
        NEAR_STOP_RULE_AGENT.format(rr_min=_rr,
                                    stop_cap_clause=stop_cap_clause)
        if (signal.get("signal_type") or "") in ("trigger_breakout", "trigger_trend_breakout")
        else ""
    )
    system = AGENT_SYSTEM.format(
        max_rounds=MAX_ROUNDS, _ref_desc=_REF_DESC,
        rr_min=_rr,
        sl_min_atr=f"{settings.SL_MIN_ATR:g}",
        tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
        tp1_max_r=f"{settings.TP1_MAX_R:g}",
        tp1_cap_desc=render_tp1_cap_desc(_rr),
        far_atr=round(settings.TP1_MAX_ATR / _rr, 2),
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
        _RULE_SCORE=render_rule_score(
            min_recommendation, _rr,
            risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
            consecutive_losses=consecutive_losses,
            trigger_type=signal.get("signal_type"),
        ),
        _RULE_BTC_ENV=_RULE_BTC_ENV,
        _RULE_VOTE_BRIEF=_RULE_VOTE_BRIEF,
        _RULE_STRUCT_BRIEF=_RULE_STRUCT_BRIEF,
        near_stop_rule=near_stop_rule,
        _RULE_SL_AGENT=_sl_agent,
        _RULE_TP_AGENT=_tp_agent,
        _price_anchor_desc=_PRICE_ANCHOR_ON if _tp2_on else _PRICE_ANCHOR_OFF,
        _tp_anchor_desc=_TP_ANCHOR_DESC_ON if _tp2_on else _TP_ANCHOR_DESC_OFF,
        _tp2_rule=(_TP2_RULE_ON if _tp2_on else _TP2_RULE_OFF).format(
            rr_min=_rr,
            tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
            tp1_max_r=f"{settings.TP1_MAX_R:g}",
            tp1_cap_desc=render_tp1_cap_desc(_rr),
            tp2_gap_atr=f"{settings.TP2_MIN_GAP_ATR:g}",
            tp2_gap_r=f"{settings.TP2_MIN_GAP_R:g}",
        ),
        skill_index=skill_index,
    )
    if strategy_prompt:
        system += (
            "\n\n用户自定义交易策略（冲突时以自定义策略为准）：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    user_msg = _build_user_msg(signal, klines, user_input, htf_klines,
                               stop_cap_pct=stop_cap_pct, tactics_block=tactics_block,
                               flt=flt, min_recommendation=min_recommendation,
                               consecutive_losses=consecutive_losses)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    ctx = {
        "signal": signal, "klines": klines, "pool": pool,
        # 止损金额 fixed 模式预算实额（pct 模式为 None → validate_decision 内走
        # 风险比例% 口径）；2026-09-27 统一口径
        "risk_budget": risk_amount if risk_mode == "fixed" and risk_amount > 0 else None,
        # 盈亏比铁律/风险比例（2026-10-02 上页面）：透传 submit_decision 校验
        # rr_tol（2026-10-14）：RR/TP1 容差带同源透传（None=env 默认 0.02）
        "rr_min": rr_min, "risk_pct": risk_pct, "rr_tol": rr_tol,
        # TP2 分批开关（2026-10-08）：透传 submit_decision 硬清零
        "tp2_enabled": _tp2_on,
    }
    fixed: Optional[dict] = None
    tool_rounds = 0
    trace: list[dict] = []
    t0 = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        rt = time.time()
        # 主循环走统一流式收口（2026-10-16，docs/03 §5.1）：消除全仓流式治理的唯一
        # 非流式例外（GLM 思考型非流式长推理零字节返回会被读超时误杀）。
        # stream_completion 已聚合 tool_calls，返回的 dict 与非流式 message 同形状。
        resp = stream_completion(
            client,
            messages,
            tools=_tools_schema(tp2_enabled=_tp2_on),
            temperature=0.3,
            max_tokens=32000,  # GLM 思考型：reasoning 计入配额，12000 被思考耗尽过（同 ai_analyzer 口径）
        )
        tool_calls = list(resp.tool_calls or [])
        content = resp.content or ""
        trace.append({
            "round": rnd,
            "llm_ms": int((time.time() - rt) * 1000),
            "tools": [_tc_name(tc) for tc in tool_calls],
        })
        logger.info(
            "Agent %s round%d: tools=%d content=%dB",
            signal.get("symbol"), rnd, len(tool_calls), len(content),
        )
        # round 事件（2026-10-16，docs/03 §5.2）：此前只发 tool 事件、round 声明缺失，
        # 前端无法显示轮次进度；每轮（含空转轮）工具执行前推一条，tools=本轮工具名
        if progress_cb:
            progress_cb({"t": "round", "round": rnd,
                         "tools": [_tc_name(tc) for tc in tool_calls]})

        if not tool_calls:
            # 空转轮（模型未调工具只出正文）：记入 trace 供复盘，塞 nudge 进下一轮
            trace[-1]["nudge"] = True
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "请调用 submit_decision 提交决策。"})
            continue

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        def _run_tool(tc):
            """单工具执行：参数 JSON 非法 → 明确错误文本回喂；工具内异常 → 护栏转文本。

            2026-10-16：此前参数解析失败静默当 {}、_dispatch_tool 无 try——畸形参数
            （如 get_recent_klines n="abc" 触发 int() ValueError）会穿出整个 Agent 循环，
            导致不写 stage_trace、不返回 forced skip（外层记"分析失败"落库缺结论）。
            """
            name = _tc_name(tc)
            raw = _tc_args(tc)
            tt = time.time()
            try:
                args = json.loads(raw or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数须为 JSON 对象")
            except (json.JSONDecodeError, ValueError) as e:
                return tc, {}, False, (
                    f"工具参数 JSON 非法（{str(e)[:120]}）：{raw[:200]}。"
                    "请重新以合法 JSON 对象调用。"
                ), None, int((time.time() - tt) * 1000)
            try:
                done, result, decision = _dispatch_tool(name, args, ctx)
            except Exception as e:  # 工具级异常护栏（含 submit 的畸形数值转换）
                logger.warning("Agent 工具 %s 执行异常: %s", name, e, exc_info=True)
                done, decision = False, None
                result = (
                    f"工具执行异常（{type(e).__name__}: {str(e)[:200]}），"
                    "请检查参数后重试。"
                )
            return tc, args, done, result, decision, int((time.time() - tt) * 1000)

        # 轮内多工具并行执行（GLM 常在一轮里同时要数据工具+技能），结果按 tool_call 顺序回填
        with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as ex:
            outcomes = list(ex.map(_run_tool, tool_calls))

        for tc, args, done, result, decision, ms in outcomes:
            tool_rounds += 1
            trace[-1].setdefault("calls", []).append({
                "tool": _tc_name(tc),
                "args": {k: (str(v)[:80]) for k, v in args.items()},
                "result_len": len(result),
                "ms": ms,
            })
            if progress_cb:
                progress_cb({
                    "t": "tool", "tool": _tc_name(tc),
                    "args": "; ".join(f"{k}={v}" for k, v in args.items())[:60],
                })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": result[:_TOOL_RESULT_MAX_CHARS],
            })
            # 单 submit 语义（2026-10-16）：一轮仅接受首个成功提交，同轮后续 submit
            # 不再改写结论（原先 `if done: fixed = decision` 为后到覆盖）
            if done and fixed is None:
                fixed = decision
        _trim_context(messages)
        if fixed:
            break

    if not fixed:
        fixed = build_forced_skip(
            f"Agent {MAX_ROUNDS} 轮内未提交有效决策{_summarize_trace(trace)}"
        )

    fixed["stage_trace"] = {
        "rounds": len(trace),
        "tool_calls": tool_rounds,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "steps": trace,
    }
    logger.info(
        "Agent 完成 %s: %s rr=%s rounds=%d %.1fs",
        signal.get("symbol"), fixed.get("trade_decision"),
        fixed.get("risk_reward_ratio"), tool_rounds, time.time() - t0,
    )
    return fixed


def _build_facts(signal: dict, klines: list, atr: float) -> dict:
    """技能 use_when 判定用的布尔事实（委托 skill_library，与 P0 管线共用）"""
    return skill_library.build_facts(signal, klines)


def _build_user_msg(
    signal: dict, klines: list,
    user_input: Optional[str], htf_klines: Optional[list] = None,
    stop_cap_pct: Optional[float] = None, tactics_block: str = "", flt=None,
    min_recommendation: Optional[float] = None, consecutive_losses: Optional[int] = None,
) -> str:
    """P1 用户消息（T3-3，2026-10-21）：事实包与 P0 共用 build_signal_facts。

    此前 P1 自渲染一份事实（带 timestamp/quote_vol/单独 ATR 行、字段顺序不同），
    与 P0 的同一信号事实文本不一致；统一后两管线 diff 仅剩系统提示词的工具
    能力段。删除 timestamp/quote_vol 与 P0 2026-09-28 的瘦身口径一致
    （K 线等间隔，顺序即可；量能看量比倍数，绝对成交额无行为指向）。
    """
    msg = build_signal_facts(
        signal, klines, htf_klines,
        stop_cap_pct=stop_cap_pct, flt=flt,
        min_recommendation=min_recommendation,
        consecutive_losses=consecutive_losses,
        market_summary="",  # P1 走工具循环按需取环境数据
    ) + tactics_block
    if user_input:
        msg += (
            "\n\n用户补充说明（请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )
    return msg
