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
from app.services.llm_client import get_client
from app.services.risk_guard import (
    TRADE_TYPES,
    TradeDecision,
    build_forced_skip,
    validate_decision,
)
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import calc_atr
from app.services.strategy.technical import atr_series, render_indicator_block
from app.services.strategy.types import POSITION_LABEL_MAP
from app.services.ai_analyzer import (
    _RULE_HTF_HARD,
    _RULE_STRUCT,
    _RULE_VOTE,
    SIGNAL_TYPE_LABELS,
    render_signal_gate_block,
)

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5

# AGENT_SYSTEM 内嵌片段：公共片段的 P1 适配版（P1 有工具循环，analysis 字段名是 reason）
_RULE_VOTE_BRIEF = _RULE_VOTE.replace("analysis 第一条", "reason 第一条")
_RULE_STRUCT_BRIEF = _RULE_STRUCT.replace("analysis", "reason")
# AGENT_SYSTEM 用 str.format 渲染，公共片段中可能存在的 { } 需先转义
_RULE_VOTE_BRIEF = _RULE_VOTE_BRIEF.replace("{", "{{").replace("}", "}}")
_RULE_STRUCT_BRIEF = _RULE_STRUCT_BRIEF.replace("{", "{{").replace("}", "}}")

# 近锚降级链（docs/23，2026-09-29，仅突破类信号注入；P1 版：锚点用 signal_low/signal_high
# 表达，stop_anchor 由程序从 stop_ref 推导、无需自报）。近锚只救"万事俱备、只差盈亏比"
# 的突破单，其余条件任一不满足时无近锚资格
NEAR_STOP_RULE_AGENT = """11.5 突破类止损近锚降级链：结构锚（摆动前低/前低）优先；按结构锚复算盈亏比 <{rr_min}
    且调整入场价仍无法达标（本应 skip）时，才降级近锚——stop_ref=signal_low
    （做多，金K窗口最低价）或 signal_high（做空，金K窗口最高价）+ offset_pct=-0.3，
    止盈与盈亏比按新锚复算；近锚仍不达标 → skip。硬约束：仅当 skip 的唯一原因是
    盈亏比不达标（信号方向、4h 背景、趋势状态、量能特征、质量分等其余开单条件
    必须全部满足）才允许用近锚；量比 <1 的缩量突破无近锚资格；近锚后盈亏比 >4
    时重新评估或 skip；近锚止损宽度仍须 ≥0.3×ATR。{stop_cap_clause}结构锚止损宽度
    已超程序硬上限（见信号门槛块）时无近锚资格，直接 skip。程序会校验近锚止损必须
    贴近金K窗口极值 ±0.5%，偏离过远会被打回。距离分档衔接（2026-10-12-5）：
    结构锚距入场超过 11 条距离分档线时按 11 条直接降级金K锚，不受本条
    "RR 唯一不达标"前提约束（量比 <1 无资格同样适用）；本条仅处理结构锚
    不远但盈亏比仍不达标的场景。"""

def render_system_prompt(
    risk_label: str = "", rr_min: Optional[float] = None,
    tp2_enabled: bool = True,
) -> str:
    """渲染最终生效的 P1 系统提示词（页面「查看当前生效提示词」与实际调用同源）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案（如"固定 3 USDT"/
    "账户风险基数 × 3%"），空时回落默认百分比口径。
    near_stop_rule 按突破类注入完整文案（预览无信号上下文，展示通用版）。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，system_config.ai_rr_min 覆盖时
    由调用方传入；None 回落 env 默认。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时剔除止盈二锚点语义
    （system_config.tp2_enabled，默认关），与实际调用（analyze_coin_agent）同源。"""
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _tp1_atr = f"{settings.TP1_MAX_ATR:g}"
    _tp1_r = f"{settings.TP1_MAX_R:g}"
    _far_atr = round(settings.TP1_MAX_ATR / _rr, 2)
    return AGENT_SYSTEM.format(
        max_rounds=MAX_ROUNDS, _ref_desc=_REF_DESC,
        rr_min=_rr,
        sl_min_atr=f"{settings.SL_MIN_ATR:g}",
        tp1_max_atr=_tp1_atr,
        tp1_max_r=_tp1_r,
        far_atr=_far_atr,
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
        _RULE_HTF_HARD=_RULE_HTF_HARD,
        _RULE_VOTE_BRIEF=_RULE_VOTE_BRIEF,
        _RULE_STRUCT_BRIEF=_RULE_STRUCT_BRIEF,
        near_stop_rule=NEAR_STOP_RULE_AGENT.format(
            rr_min=_rr,
            stop_cap_clause="止损宽度硬上限值以信号门槛块为准。",
        ),
        _price_anchor_desc=_PRICE_ANCHOR_ON if tp2_enabled else _PRICE_ANCHOR_OFF,
        _tp_anchor_desc=_TP_ANCHOR_DESC_ON if tp2_enabled else _TP_ANCHOR_DESC_OFF,
        _tp2_rule=(_TP2_RULE_ON if tp2_enabled else _TP2_RULE_OFF).format(
            rr_min=_rr, tp1_max_atr=_tp1_atr, tp1_max_r=_tp1_r,
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
{_RULE_HTF_HARD}
1. 程序能算的不让你算——事实包数据直接用；需要补充数据才调用工具，最多 {max_rounds} 轮。
2. **禁止编造绝对价格**：所有价格必须用结构位锚点表达{_price_anchor_desc}，
   ref 取值: {_ref_desc}；offset_pct 为相对锚点的百分比偏移（如支撑位下方 0.5×ATR 用负 offset）。
   {_tp_anchor_desc}
3. 决策必须通过 submit_decision 工具提交，提交后程序会做风控校验：
   校验不通过时工具会返回违规明细，请按明细修正后重新提交。
4. **入场价纪律**：当前价格是分析时刻的最新价。**只允许现价入场（2026-09-30 二次拍板）**：
   entry 用 market 锚（offset 0 附近），**禁止**把入场价放到现价回踩侧（多单低于现价/
   空单高于现价）——"待回踩"建议程序不会下单（等于放弃该笔）。若按现价入场 RR 无法
   达标 → 直接 skip（**不得给出回踩等待计划**，等回踩建议一律被程序丢弃）；
   不要给出既不贴近现价也不贴近结构位的模糊入场价。
5. 开单类型归类（trade_type，suggest 必填其一）：
   trend_follow 顺势交易（趋势方向由 EMA 排列或指标多数决确立，纯 EMA 排列不是硬门槛）/
   structure_break 结构破位回踩（涵盖 123法则·N字结构·2B法则，见下方打法详解）/
   range_edge 区间边缘反转。
6. "可用技能"列表中标注【当前命中】的技能，建议先 load_skill 阅读再决策。
7. **效率与数据边界（重要）**：事实包只含信号与行情数据（60根K线含每根成交量与成交额/摆动结构/均线形态/ATR/技术指标MACD·布林带·RSI·SuperTrend·ADX·OBV·4h背景），
   首轮即可直接 submit_decision；资金费率、大盘状态、恐贪指数等环境数据**不在**事实包中，
   确需时在同一轮一次性批量调用 get_funding / get_market_breadth / get_fear_greed 自行获取
   （如资金费率极端可 load_skill 阅读 funding-extreme-handling）；不要为用工具而用工具，
   也不要每轮只查一个工具。
8. 不要输出不带工具调用的纯文字回复（会浪费一轮）；文字只用于简短说明查询意图，
   决策一律通过 submit_decision 提交。
9. 盈亏比铁律 ≥{rr_min}（无豁免）；止损宽度由结构位决定，不设固定价格百分比上限，不得以
   「止损过宽」为由 skip——宽止损由固定亏损法自动缩仓消化（止损越远仓位越小）；
   但止损**不得过近**：宽度须 ≥{sl_min_atr}×ATR（低于噪音级必被扫），也不得低于 0.3%；
   锚点落在该宽度内时把锚外扩到至少该宽度再报，否则直接 skip（程序按此下限复算打回）；
   仓位不要自己报，程序按固定亏损法计算，单笔止损预算={risk_label}：仓位 = 预算 ÷ 止损距离
   （止损越远仓位越小）。
10. 指标方向判定：{_RULE_VOTE_BRIEF}
    EMA 排列不构成方向限制（多头排列不禁止做空、空头排列不禁止做多）。
11. 止损锚定（提示词纪律，程序不再改写 stop 值——止损是否合理由你自己把关，
    程序只复算盈亏比与止损宽度下限）：结构位优先、金K极值兜底、两锚取更远者。做多优先锚定入场价下方
    最近摆动低点外 0.3%~0.5%，做空锚定上方最近摆动高点外同幅度；入场贴着结构位时
    止损还须越过金K极值——金K极值 = 金K窗口（最近 5 根已收盘K线，含信号K/突破K线，
    即事实包 K 线序列最后一根已收盘K线；最后一根未收盘K线不参与）的共同极值：
    做多取 5 根最低点、做空取 5 根最高点，
    做多取摆动低点与 5 根最低点中更低者、做空取摆动高点与 5 根最高点中更高者外 0.3%
    （该极值就是本笔信号的直接失效位；止损在近期波动区间内部必被扫损）；
    入场价与金K极值间无摆动结构时用金K锚——
    做多严格低于 5 根最低点（影线极值非收盘价）至少 0.3%，做空相反。
    距离分档（2026-10-12-5）：最近摆动结构位距入场超过 {far_atr}×ATR
    （=止盈一距离上限÷盈亏比铁律，超过此线 RR 数学上不可能达标）时，
    该位是更大级别趋势的失效位而非本笔信号失效位，不作为止损锚——
    直接降级金K锚：做多 stop_ref=signal_low、做空 stop_ref=signal_high
    （金K窗口极值外 0.3%）；降级后仍须满足盈亏比铁律与
    ≥{sl_min_atr}×ATR 宽度下限，不达标 → skip。
{near_stop_rule}
12. 止盈锚定（提示词纪律，程序不再改挂）：结构位=摆动高低点（事实包"近期摆动结构"）。
    距现价特别近（≤1×ATR）的位是价格正在
    测试/突破的"争夺位"，只回答"是否得到支撑/压力/突破"，
    不算止盈目标；做多止盈一必须挂在更远的第一档结构位（摆动高点）下方 0.2%~0.5%，
    做空挂在下方第一档结构位（摆动低点）上方 0.2%~0.5%
    ——不得贴死位或挂在越位一侧（价格常在位附近反弹，越位才触发的止盈大概率落空）。
    禁止把止盈一挂到更远结构位凑盈亏比：到第一结构位的盈亏比不足 {rr_min} 说明空间不足，
    应直接 skip（程序只复算盈亏比并按铁律打回）。
    另：第一档止盈须"够近"——距入场 ≤{tp1_max_atr}×ATR 且 ≤{tp1_max_r}×止损距离；
    更远的第一档结构位越过此上限时（强趋势/点位稀疏常见，会落到 3~4R）不要跳档硬挂，
    改用"入场 ± {rr_min}×止损距离"兜底（程序按距离上限复算打回；启用止盈二时
    该远端结构位保留为止盈二锚，见下条）。
    {_tp2_rule}
12.5 分析格式（reason 落库展示，前端按固定节标题分区）：按固定节标题两节输出
    （节标题逐字含冒号），每条独占一行（JSON 字符串内换行写 \n）：
    「公共检查：」（1. 4h方向 2. 指标多数决 3. 回踩触点 4. 形态与量能
    5. 风险点——含资金费率拥挤/BTC 大盘急跌等环境风险，无则写"无"）
    →「决策结论：」**按条分行**写满足/不满足什么——每条独占一行，以
    "1. 2. 3. …"序号逐条列出，最后以单独一行"结论：建议开单/不建议开单"收尾
    （**禁止**写成一行长句或不分条）。
    skip 时同样两节完整输出（决策结论节分条写不满足的主因）。
13. 方向纪律（不再程序强制，无 ±0.3% 贴近要求）：优先只在支撑结构做多（摆动低点/
    近期低点附近）、只在压力结构做空（摆动高点/近期高点附近）；现价已离开结构位时
    不再要求贴近，按动能与盈亏比自行判断入场（breakout 顺势追突破仍用 market 锚）。

{_RULE_STRUCT_BRIEF}

{skill_index}"""


# TP2 分批开关（2026-10-08）槽位变体：关闭时提示词整体剔除止盈二锚点语义，
# 与 P0 管线（ai_analyzer.compose_rule_tp）同目的——AI 不产出第二档止盈价。
# 含 {rr_min}/{tp1_max_atr}/{tp1_max_r} 的变体在渲染前须先显式 .format 再传入 AGENT_SYSTEM.format。
_PRICE_ANCHOR_ON = "（entry_ref/stop_ref/tp1_ref/tp2_ref + offset_pct）"
_PRICE_ANCHOR_OFF = "（entry_ref/stop_ref/tp1_ref + offset_pct）"

_TP_ANCHOR_DESC_ON = """止盈用线外锚点：做多止盈一 tp1_ref=resistance_zone_low（现价上方第一个距现价 >1×ATR
   的摆动高点，≤1×ATR 的争夺位不算；历史命名）、止盈二 tp2_ref=next_resistance_zone_low
   （其更远一档的摆动高点）；做空用 support_zone_high / next_support_zone_high
   （现价下方满足同条件的摆动低点及其更远一档）。止盈 offset 留余地：
   做多 -0.2~-0.5（位下方），做空 0.2~0.5（位上方）。"""

_TP_ANCHOR_DESC_OFF = """止盈用线外锚点：做多止盈一 tp1_ref=resistance_zone_low（现价上方第一个距现价 >1×ATR
   的摆动高点，≤1×ATR 的争夺位不算；历史命名）；做空用 support_zone_high
   （现价下方满足同条件的摆动低点）。止盈 offset 留余地：
   做多 -0.2~-0.5（位下方），做空 0.2~0.5（位上方）。"""

_TP2_RULE_ON = """止盈二取止盈一锚定档的下一档同类结构位（做多=更高一档摆动高点下方，做空=更低一档
    摆动低点上方）且必须比止盈一更远。价格创新高/新低、前方无摆动结构位时
    （如创新高突破），止盈一 = 入场 ± {rr_min}×止损距离、止盈二不设（仅一档）；
    若近端无合适结构位、第一档结构位距入场超过 {tp1_max_atr}×ATR / {tp1_max_r}×止损距离
    （结构位存在但过远），止盈一 = 入场 ± {rr_min}×止损距离，止盈二保留锚定该远端
    结构位（做多 tp2_ref=resistance_zone_low，做空 tp2_ref=support_zone_high，
    即"止盈一因过远弃用的那一档"），作为趋势跑手档。
    盈亏比 ≥{rr_min} 是开单硬性要求（程序复算，不足即打回）。"""

_TP2_RULE_OFF = """当前未启用止盈二（TP2 分批止盈已关闭）：不要提交 tp2_ref/tp2_offset_pct
    （提交也会被程序置 0），止盈一只挂第一档结构位。价格创新高/新低、前方无摆动
    结构位时，或第一档结构位距入场超过 {tp1_max_atr}×ATR / {tp1_max_r}×止损距离时，
    止盈一 = 入场 ± {rr_min}×止损距离。
    盈亏比 ≥{rr_min} 是开单硬性要求（程序复算，不足即打回）。"""

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
                               "structure_break 结构破位回踩（123·N字·2B 合并）/ range_edge 区间边缘反转",
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
            "recommendation": {"type": "integer", "description": "推荐程度 0-100，skip≤30，suggest≥50"},
            "tactic_name": {"type": "string",
                            "description": "战法命中名（仅借鉴标注）：用户消息出现【战法库判定】块且信号符合某战法时逐字输出该名，均不符合输出空字符串"},
            "tactic_reason": {"type": "string", "description": "战法判定依据 ≤60字；未命中输出空字符串"},
            "reason": {
                "type": "string",
                "description": (
                    "决策分析，≤300字中文，直接作为分析结果落库展示。按固定节标题两节输出、"
                    "每条一行（JSON 字符串内换行写\\n，节标题逐字含冒号）："
                    "公共检查：（1. 4h方向 2. 指标多数决 3. 回踩触点 4. 形态与量能 5. 风险点）→"
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
        rr_min=rr_min, risk_pct=risk_pct,
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
        )
    return False, f"unknown tool: {name}", None


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
) -> dict:
    """Agent 循环入口：返回与 Risk Guard 相同结构的决策 dict（可直接落库）。

    返回值额外带 "stage_trace" 键（工具循环 trace，docs/04 §5.9），落库时一并存储。
    progress_cb: 可选回调，接收 {"t": round/tool, ...} 进度事件供前端流式展示。
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
    渲染与页面配置同源；None=env/代码默认。"""
    client = get_client()

    # 布尔事实（技能 use_when 判定）
    ema = analyze_ema(klines)
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
        far_atr=round(settings.TP1_MAX_ATR / _rr, 2),
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
        _RULE_HTF_HARD=_RULE_HTF_HARD,
        _RULE_VOTE_BRIEF=_RULE_VOTE_BRIEF,
        _RULE_STRUCT_BRIEF=_RULE_STRUCT_BRIEF,
        near_stop_rule=near_stop_rule,
        _price_anchor_desc=_PRICE_ANCHOR_ON if _tp2_on else _PRICE_ANCHOR_OFF,
        _tp_anchor_desc=_TP_ANCHOR_DESC_ON if _tp2_on else _TP_ANCHOR_DESC_OFF,
        _tp2_rule=(_TP2_RULE_ON if _tp2_on else _TP2_RULE_OFF).format(rr_min=_rr),
        skill_index=skill_index,
    )
    if strategy_prompt:
        system += (
            "\n\n用户自定义交易策略（冲突时以自定义策略为准）：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    user_msg = _build_user_msg(signal, klines, ema, atr, user_input, htf_klines,
                               stop_cap_pct=stop_cap_pct, tactics_block=tactics_block,
                               flt=flt)
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
        "rr_min": rr_min, "risk_pct": risk_pct,
        # TP2 分批开关（2026-10-08）：透传 submit_decision 硬清零
        "tp2_enabled": _tp2_on,
    }
    fixed: Optional[dict] = None
    tool_rounds = 0
    trace: list[dict] = []
    t0 = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        rt = time.time()
        resp = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=messages,
            tools=_tools_schema(tp2_enabled=_tp2_on),
            temperature=0.3,
            max_tokens=32000,  # GLM 思考型：reasoning 计入配额，12000 被思考耗尽过（同 ai_analyzer 口径）
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        trace.append({
            "round": rnd,
            "llm_ms": int((time.time() - rt) * 1000),
            "tools": [tc.function.name for tc in tool_calls],
        })
        logger.info(
            "Agent %s round%d: tools=%d content=%dB",
            signal.get("symbol"), rnd, len(tool_calls), len(msg.content or ""),
        )

        if not tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "请调用 submit_decision 提交决策。"})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        })

        def _run_tool(tc):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tt = time.time()
            done, result, decision = _dispatch_tool(tc.function.name, args, ctx)
            return tc, args, done, result, decision, int((time.time() - tt) * 1000)

        # 轮内多工具并行执行（GLM 常在一轮里同时要数据工具+技能），结果按 tool_call 顺序回填
        with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as ex:
            outcomes = list(ex.map(_run_tool, tool_calls))

        for tc, args, done, result, decision, ms in outcomes:
            tool_rounds += 1
            trace[-1].setdefault("calls", []).append({
                "tool": tc.function.name,
                "args": {k: (str(v)[:80]) for k, v in args.items()},
                "result_len": len(result),
                "ms": ms,
            })
            if progress_cb:
                progress_cb({
                    "t": "tool", "tool": tc.function.name,
                    "args": "; ".join(f"{k}={v}" for k, v in args.items())[:60],
                })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result[:12000],
            })
            if done:
                fixed = decision
        if fixed:
            break

    if not fixed:
        fixed = build_forced_skip(f"Agent {MAX_ROUNDS} 轮内未提交有效决策")

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
    signal: dict, klines: list, ema: Optional[dict], atr: float,
    user_input: Optional[str], htf_klines: Optional[list] = None,
    stop_cap_pct: Optional[float] = None, tactics_block: str = "", flt=None,
) -> str:
    closed = klines[:-1] if len(klines) >= 2 else klines
    # 40 根与 P0 管线同口径（60→40，2026-09-28 缩 token 提速，见 ai_analyzer._build_messages）
    recent = closed[-40:]
    # 每根K线自带成交量与成交额（quote_vol，USDT），不再给汇总口径
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}" for k in recent
    )
    # 近期摆动结构（HH/LH/HL/LL，收盘价摆动点）——锚点解析的数据源
    sw = signal.get("recent_swings") or {}
    swings_section = ""
    if sw.get("highs") or sw.get("lows"):
        sw_lines = ["近期摆动结构(收盘价摆动点,新→旧):"]
        if sw.get("highs"):
            sw_lines.append(
                "  高点: " + ", ".join(
                    f"{h['label']} {h['price']:.6g}（{h['bars_ago']}根前）" for h in sw["highs"]
                )
            )
        if sw.get("lows"):
            sw_lines.append(
                "  低点: " + ", ".join(
                    f"{lv['label']} {lv['price']:.6g}（{lv['bars_ago']}根前）" for lv in sw["lows"]
                )
            )
        swings_section = "\n".join(sw_lines) + "\n"
    # 量能（放量突破判定依据，classify_volume 口径：相对近20根已收盘均量的倍数）
    vol_desc = signal.get("volume_type") or "—"
    if signal.get("volume"):
        vol_desc += f"（{float(signal['volume']):.1f}×20根均量）"
    msg = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {SIGNAL_TYPE_LABELS.get(signal['signal_type'], signal['signal_type'])}（{signal['signal_type']}）\n"
        f"当前价格: {signal['current_price']}\n"
        f"K线形态: {signal.get('pattern') or '无'}（位置: "
        f"{POSITION_LABEL_MAP.get(signal.get('position') or '', signal.get('position') or '未知')}）\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"{render_signal_gate_block(signal['signal_type'], stop_cap_pct=stop_cap_pct, flt=flt)}"
        f"信号强度: {signal.get('strength', '—')}\n"
        f"量能: {vol_desc}\n"
        f"ATR(14): {atr:.6g}\n"
        + (f"均线形态: {ema['state_label']}（{ema['detail']}）\n" if ema else "")
        + render_indicator_block(klines, htf_klines)
        + swings_section
        + tactics_block
        + f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol,quote_vol_USDT):\n{kline_summary}"
    )
    if user_input:
        msg += (
            "\n\n用户补充说明（请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )
    return msg
