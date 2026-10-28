"""AI 分析服务：调用 OpenAI 兼容接口，为命中币种生成开单建议"""
import json
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.services import skill_library
from app.services.llm_client import get_client, parse_llm_json, json_mode_kwargs, stream_completion
from app.services.market_data import build_market_context, get_kline_client
from app.services.risk_guard import build_forced_skip, parse_and_validate
from app.services.score_thresholds import (
    derate_threshold,
    static_min_recommendation,
)
from app.services.strategy.candlestick import golden_whitelist_labels
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.technical import render_indicator_block
from app.services.strategy.trigger import TOUCH_LABELS
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

# ── 提示词公共片段：P0/P1 共享的纪律段（改一处两管线同步生效，防文本漂移）──
# 2026-10-21 用户拍板：4h 方向配对只保留在筛选层（scanner 前置门），
# 提示词与 AI 出结果后的程序层都不再注入方向硬约束——4h 状态仅作为数据提供，
# 多空思路由 AI 结合结构/量能/方向票自主判断。
_RULE_VOTE = """方向票规则：技术指标块"方向票"行由程序按 MACD/布林带/RSI 统计
（同向 ≥2 张即成立），是程序事实、不是方向硬约束；在 {field} 第一条写明
"方向票: 看多N项 vs 看空M项"及各指标依据。常规单以该行多数方向为基准，多空分歧或
结构与票数相反时结合 K 线结构、量能与 4h 背景自主判断，并在结论中写明依据。
SuperTrend/ADX/OBV/4h背景 不计票（分别作风险提示/趋势强度；4h 背景仅作环境数据参考，
不构成方向限制）。回踩确认单（trigger_pullback_confirm）：1h 方向票反向或同向不足
属回撤常态，仅作确认度参考（影响质量分），不得据此否决或降分；多空方向结合
4h 背景与结构自主判断。"""


def render_rule_vote(field: str = "analysis") -> str:
    """方向票纪律片段按落库字段名渲染（P0 field="analysis" / P1 field="reason"）。

    2026-10-16：由 P1 侧的 `.replace("analysis", "reason")` 改为显式参数化渲染
    ——replace 未命中会静默失配（文本漂移无告警），参数化后缺字段占位直接 KeyError。
    """
    return _RULE_VOTE.format(field=field)

_RULE_EMA = """EMA 排列不构成方向限制：多头排列不禁止做空、空头排列不禁止做多，
均线仅是趋势背景参考之一（金叉/死叉/拐头是动能转换信号）。"""

_RULE_SL = """止损纪律（结构位优先、金K极值兜底，两锚取更远；程序不改写）：
- 结构锚：做多锚入场下方最近摆动低点外 0.4%（0.4%~0.6%），做空锚上方摆动高点外同幅。
- 金K极值 = 金K窗口（最近 5 根已收盘K线，含信号K/突破K，未收盘根不参与）共同极值；
  做多取摆动低点与 5 根最低点中更低者外 0.4%，做空取更高者外 0.4%。该极值即失效位（影线极值）。
- 距离分档：最近结构位距入场 > {far_atr}×ATR（止损锚分档阈值，与止盈上限独立；超过即视为更大级别）
  时该位属更大级别、不作止损锚，直接降级金K锚（stop_anchor="near"）；突破类降级需量比≥1。
- 兜底（与金K极值间无摆动位）：同样用金K锚（多严格低于最低点、空严格高于最高点，≥0.4%）。
- 止损宽度无内部上限，不得以「止损过宽」为由 skip——宽止损由固定亏损法自动缩仓；
  门槛块给出「止损宽度硬上限」时以硬闸为准、超限 skip。
- 止损**不得过近**：宽度 ≥0.3×ATR 且 ≥0.3%；过近时外扩该宽度再报，否则 skip。
  盈亏比不达标先调入场价，仍不达标如实 skip。"""

def render_tp1_cap_desc(rr_min: Optional[float] = None) -> str:
    """第一档止盈距离上限的**程序口径**文案（2026-10-19）。

    与 risk_guard 复算严格一致：cap = max(min(TP1_MAX_ATR×ATR, TP1_MAX_R×止损距离),
    rr_min×止损距离)。此前提示词写成"≤TP1_MAX_ATR×ATR **且** ≤TP1_MAX_R×止损距离"的绝对
    硬上限，且 TP1_MAX_R=0（R 腿关闭）时渲染出无意义的"≤0×止损距离"——AI 据此把
    "入场 ± rr_min×止损距离"兜底价判为越 2×ATR 上限并 skip，与程序"兜底价永远可用、
    永不打回"相悖（2026-10-19 线上排查：当日高置信误 skip 9 笔）。TP1_MAX_R=0 时不渲染该腿。"""
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _atr = f"{settings.TP1_MAX_ATR:g}"
    _r = float(settings.TP1_MAX_R)
    if _r > 0:
        return f"max(min({_atr}×ATR, {_r:g}×止损距离), {_rr:g}×止损距离)"
    return f"max({_atr}×ATR, {_rr:g}×止损距离)"


# 质量分分项中文标签（P0/P1 共用；顺序即渲染顺序）
QUALITY_PART_LABELS = (
    ("base", "基础"),
    ("htf", "4h"),
    ("htf_macro", "逆宏观"),
    ("adx", "ADX"),
    ("volume", "量能"),
    ("shadow", "影线"),
    ("rsi", "RSI"),
    ("obv", "OBV"),
    ("risk_vote", "辅助票"),
    ("boll", "带宽"),
    ("zone", "结构密度"),
    ("stabilize", "企稳"),
    ("structure_gate", "结构门"),
    ("range", "震荡"),
    ("r2_low", "弱趋势"),
    ("box", "箱体"),
)


def render_quality_line(signal: dict) -> str:
    """质量分事实行（T0-3，2026-10-21）：P0/P1 共用，含分项拆解。

    只陈述程序排序结果，明确不替代 AI 判断——提示词多处要求「结合质量分」，
    此前事实包里没有该字段，模型只能凭感觉估。
    """
    q = signal.get("quality_score")
    if q is None:
        return ""
    try:
        total = f"{float(q):.0f}"
    except (TypeError, ValueError):
        return ""
    parts = signal.get("quality_parts") or {}
    detail = []
    for key, label in QUALITY_PART_LABELS:
        v = parts.get(key)
        if v is None:
            continue
        try:
            detail.append(f"{label} {float(v):+.0f}" if key != "base"
                          else f"{label} {float(v):.0f}")
        except (TypeError, ValueError):
            continue
    suffix = f"（{' / '.join(detail)}）" if detail else ""
    return f"质量分: {total}/100{suffix}（程序排序用，不替代你的判断）\n"


_RULE_TP_BASE = """止盈锚定（必须遵守；程序不改挂止盈价）：
- 结构位=摆动高低点；距现价 ≤1×ATR 的争夺位不算目标，止盈一挂更远第一档外侧
  （多=高点下方 0.3%~0.6%，空=低点上方 0.3%~0.6%）。
- 止盈一距入场上限 = {tp1_cap_desc}（程序复算口径）；其中 {rr_min}×止损距离 一项保证
  "入场价 ± {rr_min}×止损距离"兜底价永远合规、不因超 {tp1_max_atr}×ATR 被打回。
  更远结构位越过上限即近端无目标——不跳档硬挂远位，改用兜底价作止盈一
  （该远端位可留作止盈二）。**禁止以"兜底价超过上限"为由 skip。**
- 禁止跳档凑盈亏比：到第一结构位不足 {rr_min}R 应直接 skip。**唯一放宽**：盈亏比不达标是
  **唯一**不达标项（触发起效/量能/质量分等其余条件全部满足）时，改用
  "入场价 ± {rr_min}×止损距离" 作止盈一；若同时存在其他不达标项，一律 skip。"""

# TP2 分批开关（2026-10-08）：关闭时提示词整体剔除止盈二条目——AI 不再产出
# 第二档止盈价（落库 take_profit_2=0，与出场引擎 80% 口径一致）
_RULE_TP2_BULLET_ON = """- 止盈二：止盈一锚定档的下一档结构位外侧（同向更远一档，留 0.3%~0.6% 余地），须比止盈一更远；
  间距 ≥ max({tp2_gap_atr}×ATR, {tp2_gap_r}×止损距离)，不足直接输出 take_profit_2=0
  （贴档会几乎同时触发）；程序亦会对贴档/反向静默撤档。"""

_RULE_TP2_BULLET_OFF = """- 当前未启用止盈二（TP2 已关闭）：只规划止盈一，take_profit_2 一律输出 0。"""

_RULE_TP_TAIL_ON = """- 止盈一兜底，分三种情形：
  (a) 前方无任何结构位可锚（如创新高突破）：止盈一 = 兜底价，止盈二设 0（仅设一档）。
  (b) 更远结构位越过上限（上限 = {tp1_cap_desc}）：止盈一 = 兜底价，止盈二保留锚定该远端位
      （作趋势跑手档）。
  (c) 近端第一档太近、不足 {rr_min}R（原应 skip）：仅当盈亏比不达标为唯一不达标项时
      用兜底价，远位留作止盈二；否则止盈二设 0，其余 skip。"""

_RULE_TP_TAIL_OFF = """- 前方无结构位可锚（如创新高突破）、更远的第一档结构位已越过上限
  （上限 = {tp1_cap_desc}）、或近端第一档太近（到它不足 {rr_min}R 且盈亏比不达标为
  唯一不达标项）：止盈一 = 入场价 ± {rr_min}×止损距离（恰达标）；
  其余情形一律 skip。"""

# 兼容名（默认开启版）：外部仅 SYSTEM_PROMPT 槽位引用，经 compose_rule_tp 组装
_RULE_TP = "\n".join([_RULE_TP_BASE, _RULE_TP2_BULLET_ON, _RULE_TP_TAIL_ON])


def compose_rule_tp(tp2_enabled: bool) -> str:
    """止盈锚定规则块按 TP2 开关组装（tp2_enabled=False 剔除止盈二条目）。"""
    return "\n".join([
        _RULE_TP_BASE,
        _RULE_TP2_BULLET_ON if tp2_enabled else _RULE_TP2_BULLET_OFF,
        _RULE_TP_TAIL_ON if tp2_enabled else _RULE_TP_TAIL_OFF,
    ])


# 字段说明行按 TP2 开关二选一（SYSTEM_PROMPT 的 {_tp2_field} 槽位）
_TP2_FIELD_ON = """第二档止盈价，锚定更前方一档摆动结构位（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方，同样留 0.3%~0.6% 余地）且必须比止盈一更远；止盈一走兜底价
  （入场±rr_min×止损距离）时，直接锚定远端第一档结构位。"""

_TP2_FIELD_OFF = """当前未启用止盈二（TP2 分批关闭）：一律输出 0。"""

_RULE_STRUCT = """结构破位回踩打法（structure_break）子形态择一，理由写进 {field}：
1. 123法则：破支撑(1) → 反弹不创新低(2) → 突破反弹高点入场(3)；仅确认顺势方向的回踩结束，
   **不得当作抄底/逃顶的逆势反转**；止损放转折点外侧。
2. N字结构：回调不破支撑 → 突破压力 → 回踩不破入场；止损放 N 字中间低点。
共同：1h 结构反转作为入场确认，需结合 4h 背景判断延续性；方向不受 4h 排列限制；
震荡市最差（假突破连发），震荡区间已在筛选层剔除。"""


def render_rule_struct(field: str = "analysis") -> str:
    """结构破位回踩打法片段按落库字段名渲染（P0 field="analysis" / P1 field="reason"）。"""
    return _RULE_STRUCT.format(field=field)

_RULE_DIRECTION = """优先在支撑结构做多、压力结构做空；现价已离开结构位时按动能顺势
给入场价（贴近现价即可），入场是否合理由盈亏比与止损结构把关。"""

# 回踩触点语义注解（docs/22 触点体系，2026-09-28；2026-10-12 补 ema21_4h；
# 2026-10-16 ema144 复出并默认启用，门控改「1h EMA144 走平或顺 4h 方向」）
_RULE_TOUCH = """回踩触点语义（出现"回踩触点"行时程序已验证金K上一根触碰该位企稳）：
retest=突破回测位（结构意义最强）、swing=摆动结构位（HL/LH 防守位）、ema21=1h 动态价值区、
ema21_4h=4h 大周期价值区、ema55=中深度回撤价值区、ema144=深回撤价值区
（均已过程序门验证；启用类型由页面白名单控制）。有触点时止损优先锚定触点位
（做多下方外侧、做空上方外侧），仍按止损纪律两锚取更远者。"""

# ── 评分纪律（2026-10-21，P0/P1 共用）────────────────────────────
# 系统定位：规则引擎持否决权与仓位权，LLM 只出裁决与参数（docs/12 §一）。
# 原锚点「50=仅勉强达标…中间分数线性内插」把 recommendation 变成语气量表：
# AI 用 58 表达"保守"，程序却按 <60 门槛直接不分发开仓（ai_tasks
# ._dispatch_immediate_open），且仓位由固定亏损法复算、根本不读评分——语义错位。
# 改为档位化 + 注入生效门槛：分数 < 门槛即"程序不下单"，禁止用中间分表达犹豫。
# 评分档位（2026-10-21 T3-1）：按触发类型分表——回踩的 1h 反向是回撤常态，
# 不能沿用突破的「方向票同向≥2」门槛，否则健康回踩永远只能拿 60 档。
_SCORE_TIERS = {
    "breakout": """  60 = 触发有效 + 方向票同向 ≥1
  70 = 60 档 + 4h 同向（升档加分条件）+ 量能达标（量比 ≥1.2）+ 未过度延展（距触发位 ≤1.5×ATR）
  85 = 70 档 + 突破后首次回踩确认 或 结构位共振""",
    "pullback": """  60 = 触点有效 + 金K确认
  70 = 60 档 + 4h 同向（升档加分条件）+ 回撤深度合理（前一段 impulse 的 0.236~0.618）且量能未异常放大（量比 <1.5）
  85 = 70 档 + 回踩后 1h 收盘收回并创段内新高/新低""",
}
_PULLBACK_TRIGGERS = ("trigger_pullback_confirm", "trigger_pullback")


def _tier_table(trigger_type: Optional[str] = None) -> str:
    """按触发类型返回评分档位表；未指定时两张都列（预览页/无信号上下文）。"""
    if trigger_type in _PULLBACK_TRIGGERS:
        return _SCORE_TIERS["pullback"]
    if trigger_type:
        return _SCORE_TIERS["breakout"]
    return _SCORE_TIERS["breakout"] + "\n" + _SCORE_TIERS["pullback"]


_RULE_SCORE = """评分纪律（recommendation 只决定开/不开，不决定仓位）：
- 只取 60/70/85 三档，skip 一律 ≤30；禁止输出 58/62 这类中间分。
- 档位定义（按触发类型）：
{tier_table}
- 跨档须给依据（「决策结论」写明哪项共振成立/失效）。**减分项不单独跨档**：
  1h 反向、RR 恰达标只作该档位内的风险说明；环境逆风按「市场环境定位」最多降一档。
  若减分项本身足以否决，直接 skip 并写明依据（不得用中间分表达犹豫）。
- AI 分析阶段静态门槛 = {min_rec}：**低于该分数不分发开仓**，不是"减半仓"。{threshold_note}
- 连损软熔断按账号在开仓时单独校验，不等同于上述分析阶段静态门槛。
- 仓位恒由程序按固定亏损法计算（预算={risk_label} ÷ 止损距离），评分不参与；要保守
  只有两个动作：降档（仍 ≥{min_rec} 照常开单）或 skip。
- RR 恰达标（含"入场 ± {rr_min}×止损距离"兜底）不是减分项：其余满足时按 60 档。"""

# ── BTC 环境定位（2026-10-21，P0/P1 共用）────────────────────────
# BTC 状态行不是闸门（2026-10-13 起急跌仅提示词警示），不得用它做否决——
# 否则等于绕过已删除的 N-15 单边环境闸门。回踩场景下 BTC 同步 1h 走弱是共因，
# 与回踩单的 1h 反向豁免同源，最多降一档。
_RULE_BTC_ENV = """市场环境定位（BTC 状态行不是闸门，不拦截开单）：
- 未出现「急跌」：BTC 与信号不同向或 1h 近3根回落 → 只写风险点，不降分、不降档、不作 skip 理由；
- 出现「BTC 短线急跌」：最多降一档（85→70 / 70→60）；降档后低于门槛 → 直接 skip 并标 [环境逆风]。"""


def render_rule_score(
    min_rec: Optional[float] = None, rr_min: Optional[float] = None,
    risk_label: str = "", consecutive_losses: Optional[int] = None,
    trigger_type: Optional[str] = None,
) -> str:
    """评分纪律片段渲染（P0/P1 同源）：门槛/盈亏比/止损预算三处生效值注入。

    min_rec：system_config.min_recommendation 优先、回落
    TRADING_MIN_RECOMMENDATION。consecutive_losses 仅作跨账号风险提示，不能
    代表所有账号的门槛；真实连损门槛在每个账号开仓时单独复核。0 表示无已知连损。
    """
    _min = float(min_rec) if min_rec is not None else static_min_recommendation()
    _need = derate_threshold(consecutive_losses or 0)
    _note = ""
    if _need:
        _note = (
            "\n  连损动态门槛由执行层按账号在开仓时单独校验，不等同于分析阶段静态门槛；"
            "不要仅因连损推测交易结论或统一 skip。"
        )
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    return _RULE_SCORE.format(
        min_rec=f"{_min:g}", rr_min=f"{_rr:g}",
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
        threshold_note=_note,
        tier_table=_tier_table(trigger_type),
    )

# 信号类型中文对照（结构分类）
SIGNAL_TYPE_LABELS = {
    "uptrend": "上涨趋势（HH+HL）",
    "downtrend": "下跌趋势（LH+LL）",
    "trend_reversal": "趋势反转（收盘价破前高/前低）",
    "range_bound": "震荡区间",
    "breakout": "放量突破（收盘破 Donchian 通道 + 放量）",
    # 指标驱动筛选（2026-09-17）：触发器类型（方向已由方向票确定）；
    # 括号内注明程序已验证的入场门槛（2026-09-25 三重收紧 + docs/19 两段式），AI 据此评估信号质量
    "trigger_breakout": "指标同向·突破前高触发（收盘越过未破的摆动结构前高）",
    "trigger_trend_breakout": "指标同向·趋势突破触发（收盘破 N 根 Donchian 滚动高低轨，强趋势连续新高/新低无未破结构位可越时的追势入场）",
    # 旧类型（2026-09-29 纯金K下线后仅历史数据可见）：压缩标签省 token（docs/20 B5）
    "trigger_candlestick": "指标同向·K线形态触发（旧类型·仅历史数据）",
    "trigger_pullback": "指标同向·回踩触发（旧类型·仅历史数据）",
    # 两段式回踩确认（2026-09-25，docs/19）：回踩触碰后等金K确认才入场；
    # 2026-09-28 docs/22 触点体系：触碰对象扩为 5 类触点（信号块"回踩触点"行注明具体位）；
    # 2026-10-01 收窄：仅流星线单根不算确认（须双流星或前根流星+当根趋势K）；
    # 锤形线恢复单根确认；倒锤形线退出看涨白名单（2026-10-01-2）；
    # 2026-10-09：孕线突破/蜻蜓/墓碑移出白名单，金K清单 12→8；
    # B4（docs/20）：金K清单由 candlestick.GOLDEN_* 常量渲染，单一来源防漂移
    "trigger_pullback_confirm": (
        "指标同向·回踩触点后金K确认（上一根触碰触点位企稳、当根出现同向金K才入场；"
        f"金K白名单：{golden_whitelist_labels()}；"
        "仅流星线单根不算确认，reason 会标注『双流星组合』或『前根流星线+当根趋势K』；"
        "倒锤形线/上吊线/孕线突破/十字星（蜻蜓/墓碑/中性）不在白名单）"
    ),
    # 趋势回踩（2026-10-10 融合，docs/19）：普通回踩确认 + EMA 趋势门——
    # 4H EMA144 趋势闸门（斜率/缠绕/ADX 三重否决）+ 1H 三线排列/快中线发散度/
    # EMA144 方向全过，其余确认机制与 trigger_pullback_confirm 完全一致
    "trigger_trend_pullback": "指标同向·趋势回踩金K确认（4H EMA144 趋势闸门[斜率≥0.5%/20根+缠绕≤2次+ADX≥20 三重否决]与 1H EMA21>55>144 三线排列、快中线发散度达标、EMA144 方向顺信号方向全过后，回踩触点+同向金K入场；确认机制同回踩金K确认，趋势背景更强）",
    "unknown": "未分类",
    "manual_search": "手动搜索",
}


def render_signal_gate_block(
    signal_type: str, stop_cap_pct: Optional[float] = None, flt=None,
    direction: Optional[str] = None,
) -> str:
    """触发器入场门槛说明（2026-09-28 动态化；2026-10-12 接线真实配置）：
    阈值读 scan_filters 生效值，消除与页面筛选配置的文本分叉（此前 0.2%/1.2×
    硬编码，页面改配置后 AI 看到的口径与程序实际闸门不一致）。flt 由调用方
    （ai_tasks / api 手动分析）透传 resolve_scan_filters(cfg) 结果；None 时
    回落 env/代码默认（无 cfg 上下文的直接调用）。按触发类型渲染对应门槛，
    非触发器类型输出空串。"""
    if signal_type not in ("trigger_breakout", "trigger_trend_breakout",
                           "trigger_pullback", "trigger_pullback_confirm"):
        return ""
    from app.services.scan_filters import resolve_scan_filters
    flt = flt or resolve_scan_filters(None)  # 未透传时按 env/代码默认
    # 止损宽度硬上限（2026-09-29 透传 max_stop_pct 补丁参数）：AI 定止损前先自验
    # 不超限，避免"AI 建议开单→程序宽度闸弃单"的无效调用；None/0=未配置不注入
    cap_line = (
        f"- 止损宽度硬上限: 止损价与入场价距离不得超过 {stop_cap_pct * 100:g}%"
        "（程序硬闸，超限必弃单）"
        if stop_cap_pct is not None and stop_cap_pct > 0 else ""
    )
    lines: list[str] = []
    if signal_type == "trigger_breakout":
        lines += [
            "- 突破位: 收盘越过最近一个未被突破的摆动结构前高（做多；做空=跌破前低对称；窗口滚动新高不算）",
            f"- 突破幅度: ≥{flt.breakout_threshold * 100:.1f}%（毛边不算）"
            + (f"，>{flt.breakout_max_pct:.1f}% 视为追高不触发" if flt.breakout_max_pct > 0 else "，追高上限未启用"),
            f"- 放量（加分项）: 信号K线量 ≥ 近20根中位量 {flt.breakout_vol_ratio:g} 倍 → 质量分+5（不拦截）",
            "- 突破K线形态: 长实体（实体 ≥70% 全长，影线合计 ≤30%）",
        ]
    elif signal_type == "trigger_trend_breakout":
        lines += [
            f"- 突破位: 收盘破 {flt.breakout_channel_bars} 根 Donchian 滚动高低轨"
            "（窗口不含信号K线；强趋势无未破结构位可越时的追势入场）",
            f"- 突破幅度: ≥{flt.breakout_threshold * 100:.1f}%（毛边不算）"
            + (f"，>{flt.breakout_max_pct:.1f}% 视为追高不触发" if flt.breakout_max_pct > 0 else "，追高上限未启用"),
            f"- 放量（加分项）: 信号K线量 ≥ 近20根中位量 {flt.breakout_vol_ratio:g} 倍 → 质量分+5（不拦截）",
            "- 突破K线形态: 长实体（实体 ≥70% 全长，影线合计 ≤30%）",
        ]
    else:
        if signal_type in ("trigger_pullback", "trigger_pullback_confirm"):
            # 2026-09-28 docs/22 触点体系：具体命中位看信号块"回踩触点"行
            # （trigger 按优先级只标一个）；2026-10-12：语义补 ema21_4h
            # （2026-10-09 上线的 4h 大周期价值区）；2026-10-16 ema144 复出
            # （改判门控后默认启用），启用类型读页面白名单生效值。
            # 2026-10-27（docs/15 §16）：按方向渲染——多头/空头各有禁用表，
            # 把被禁的触点写进提示词会让模型按不存在的触点找依据
            _refs = flt.pullback_touch_refs
            if direction in ("long", "short"):
                _refs = flt.touch_refs_for(1 if direction == "long" else -1)
            enabled = "、".join(
                f"{TOUCH_LABELS.get(r, r)}（{r}）" for r in _refs)
            lines += [
                "- 回踩触点: retest=突破回测位 / swing=摆动结构位 / ema21=1h 趋势动态"
                "价值区 / ema21_4h=4h 大周期价值区 / ema55=中深度回撤/"
                "ema144=1h 深回撤（触发条件已由程序校验）",
                f"- 当前启用触点: {enabled}"
                "（优先级 retest>swing>ema21>ema21_4h>ema55>ema144，命中位见信号块「回踩触点」行）",
                f"- 横向位触碰带宽: 影线可入带（{flt.structure_touch_band_pct:.1f}% 内）且收盘守住位；"
                "EMA 触点=下影触及且收盘收回线上（做空对称）",
                "- 两段式确认: 当根触碰+金K 直接确认，或触碰后 N 根内出现同向金K（窗口内确认）",
            ]
            if cap_line:
                lines.append(cap_line)
            return ("程序入场门槛（已程序化验证，评估信号质量时以此为基准而非自行估算）:\n"
                    + "\n".join(lines) + "\n")
    # 突破类公共段：止损锚降级链（docs/23，2026-09-29）——近锚只救
    # "万事俱备、只差盈亏比"的突破单，其余条件任一不满足时无近锚资格
    lines += [
        "- 止损锚（突破类专用）: 结构锚（摆动前低下方 0.4% 缓冲，做空=前高上方）优先；"
        "仅当按结构锚复算盈亏比 <1.5 且调整入场价仍无法达标时，才降级近锚——金K窗口"
        "（最近 5 根已收盘K线共同极值，含本信号金K/突破K线）外 0.4% 缓冲"
        "（做多=5 根最低价下方，做空=5 根最高价上方），止盈与盈亏比按新锚复算；"
        "近锚达标才可建议开单，并输出 stop_anchor=\"near\"（结构锚达标时输出 \"structure\"）；"
        "近锚仍不达标 → skip",
        "- 近锚适用前提（硬约束）: 仅当 skip 的唯一原因是盈亏比不达标——信号方向、"
        "量能特征、质量分等其余开单条件必须全部满足，否则照常 skip"
        "（4h 背景不在此列，仅作数据参考）",
        "- 近锚资格附加线: 量比 <1 的缩量突破无近锚资格；近锚后盈亏比 >4 时重新评估或 skip；"
        "近锚止损宽度仍须 ≥0.3×ATR",
        "- 距离分档衔接（2026-10-12-5）: 结构锚距入场超过止损纪律的距离分档线时按止损纪律"
        "直接降级金K锚，不受\"盈亏比唯一不达标\"前提约束（量比 <1 无资格同样适用）；"
        "本条仅处理结构锚不远但盈亏比仍不达标的场景",
    ]
    if cap_line:
        lines.append(
            cap_line + "；结构锚宽度已超上限时无近锚降级资格，直接 skip"
        )
    return ("程序入场门槛（已程序化验证，评估信号质量时以此为基准而非自行估算）:\n"
            + "\n".join(lines) + "\n")


SYSTEM_PROMPT = """你是加密货币合约交易分析师。基于提供的信号与近期K线数据，给出是否开单的建议。
直接输出JSON，不要思考过程，不要markdown包裹。

4h 背景以技术指标块的数据行给出（state/macro），仅作趋势强弱与风险参考，
不限制多空方向；请结合结构、量能、方向票自主判断。

字段如下（skip 时 direction/trade_type/entry_price/stop_loss/take_profit_1/take_profit_2/
risk_reward_ratio/position_pct 为 0/空字符串；skip_reason 除外）：
- trade_decision: "suggest"（建议开单）或 "skip"（不建议开单）
- skip_reason: skip 时必填，类别前缀开头——[RR不足]/[止损过宽]/[环境逆风]/[形态质量]/[结构不清]/[波动极端]/[其他]；前缀后按 "1. 2. 3." 列主因（≤3条，JSON 内换行写 \n）。suggest 时为空。
- direction: "long"（做多）或 "short"（做空）。
- trade_type: 开单类型，suggest 必填其一：trend_follow（顺势，方向以方向票/结构为准，
  EMA 排列仅趋势背景、不是硬门槛）/ structure_break（结构破位回踩，含 123法则·N字结构，见下方打法）。
- stop_anchor: "structure"（结构锚）或 "near"（金K窗口极值锚）；采用金K极值锚时输出 "near"
  （距离分档或突破近锚降级），省略/null 按 structure（禁止空字符串）。
- entry_price: 建议入场价。**只允许现价入场**：必须贴近当前价，**禁止**放到现价回踩侧
  （多单低于现价/空单高于现价）——程序会标"待回踩"且不下单。现价入场 RR 不达标或已远离
  结构位 → 直接 skip，**不得给出回踩等待计划**。
- 方向纪律：{_RULE_DIRECTION}
- stop_loss: 止损价（做多低于入场、做空高于入场），基于结构位。三价硬约束（程序强制）：
  做多 SL < 入场 < TP1，做空 TP1 < 入场 < SL；止损只看入场价不利侧的摆动结构/影线极值。
- take_profit_1: 第一档止盈价（细则见下方止盈锚定）。RR = |TP1−入场|÷|入场−止损| ≥ {rr_min}，
  输出前自行验算；距入场上限 = {tp1_cap_desc}，超限改挂更近结构位或直接用兜底价。
- take_profit_2: {_tp2_field}
- risk_reward_ratio: take_profit_1 的盈亏比，2位小数（如实复算）。
- position_pct: 展示用估算值，2位小数；程序按固定亏损法复算覆盖（单笔止损预算={risk_label}）
  ——**该字段不承载保守/激进语义**，保留意见用评分档位或 skip 表达。
- recommendation: 0-100 整数，**分档不内插**（档位定义见下方「评分纪律」），skip 时 ≤30；
  禁止输出介于档位之间的分数（如 58、62）来表达"保守"或"犹豫"。
- tactic_name: 战法命中名；用户消息出现【战法库判定】且信号符合时逐字输出（禁止自创），否则空。
- tactic_reason: 战法判定依据，≤60字；未命中为空。
- analysis: ≤500字。**节标题逐字固定**：「公共检查：」逐条列（1. 4h背景（数据参考）2. 方向票 3. 回踩触点
  4. 形态与量能 5. 风险点——含资金费率拥挤/BTC 大盘急跌等环境风险，无则写"无"），随后
  「决策结论：」按条分行写满足/不满足什么（序号 1. 2. 3.），末行单独写
  "结论：建议开单/不建议开单"（禁止一行长句）；每条独占一行（JSON 内换行写 \n）。
  skip 时两节仍完整输出，skip_reason 不必重复 analysis 已有内容。

{_RULE_STRUCT}

{_RULE_SL}

{_RULE_TP}

{_RULE_TOUCH}

{_RULE_SCORE}

{_RULE_BTC_ENV}

所有价格为数值，单位 USDT。
均线与指标纪律：
- {_RULE_EMA}
- {_RULE_VOTE}
- 入场/止损/止盈的结构位锚定规则不受指标方向影响，仍按上方规则执行。

资金费率与 BTC 状态以用户消息【市场环境】块为准；其余环境数据未提供勿臆测，
环境块缺失时按无环境处理。"""

def render_system_prompt(
    risk_label: Optional[str] = None, rr_min: Optional[float] = None,
    tp2_enabled: bool = True, min_recommendation: Optional[float] = None,
    consecutive_losses: Optional[int] = None, trigger_type: Optional[str] = None,
) -> str:
    """渲染最终生效的 P0 系统提示词（SYSTEM_PROMPT 为带槽位模板）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，None 时回落
    百分比口径。页面预览与实际调用共用本函数保证同源。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，system_config.ai_rr_min
    覆盖时由调用方传入；None 回落 env 默认。
    min_recommendation（2026-10-21）：AI 分析阶段静态评分门槛，低于该值不分发开仓
    （system_config.min_recommendation 优先；None 回落 env）。执行层连损动态门槛
    是账号级复核，不是批量分析统一门槛。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时剔除止盈二规则与
    字段说明，AI 不再产出第二档止盈价（system_config.tp2_enabled，默认关）。
    consecutive_losses（2026-10-21 T0-2）：已知时只提示动态门槛可能存在，不替代账号级复核。"""
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _risk_label = risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%"
    return SYSTEM_PROMPT.format(
        _RULE_DIRECTION=_RULE_DIRECTION.replace(chr(10), ' '),
        _RULE_STRUCT=render_rule_struct("analysis"),
        # 止损锚分档阈值走独立键（2026-10-28，docs/16 §20）：原来用
        # TP1_MAX_ATR÷rr_min 现算，放宽止盈上限会顺带放宽止损锚
        _RULE_SL=_RULE_SL.format(far_atr=round(settings.STOP_ANCHOR_MAX_ATR, 2)),
        # _RULE_TP 值内含 {rr_min}/{tp1_max_atr}/{tp1_max_r}/{tp2_gap_atr}/{tp2_gap_r}
        # 字面占位（format 不递归替换值），须渲染前显式替换
        _RULE_TP=compose_rule_tp(tp2_enabled).format(
            rr_min=_rr,
            tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
            tp1_max_r=f"{settings.TP1_MAX_R:g}",
            tp1_cap_desc=render_tp1_cap_desc(_rr),
            far_atr=round(settings.TP1_MAX_ATR / _rr, 2),
            tp2_gap_atr=f"{settings.TP2_MIN_GAP_ATR:g}",
            tp2_gap_r=f"{settings.TP2_MIN_GAP_R:g}",
        ),
        _RULE_TOUCH=_RULE_TOUCH,
        _RULE_SCORE=render_rule_score(
            min_recommendation, _rr, _risk_label, consecutive_losses,
            trigger_type=trigger_type),
        _RULE_BTC_ENV=_RULE_BTC_ENV,
        _RULE_EMA=_RULE_EMA.replace(chr(10), ' '),
        _RULE_VOTE=render_rule_vote("analysis").replace(chr(10), ' '),
        _tp2_field=_TP2_FIELD_ON if tp2_enabled else _TP2_FIELD_OFF,
        rr_min=_rr,
        tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
        tp1_max_r=f"{settings.TP1_MAX_R:g}",
        tp1_cap_desc=render_tp1_cap_desc(_rr),
        risk_label=_risk_label,
    )  # 模板槽位均为纯名称：表达式在渲染前预处理（f-string 时代的 .format 内嵌已移除）


def _get_client() -> OpenAI:
    # 共享单例：复用连接池，统一超时与重试策略（见 llm_client）
    return get_client()


def analyze_coin(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    htf_klines: Optional[list] = None,
    risk_mode: str = "pct", risk_amount: float = 0.0, risk_label: str = "",
    stop_cap_pct: Optional[float] = None, interval: str = "1h",
    tactics_block: str = "", rr_min: Optional[float] = None,
    crash_pct: Optional[float] = None, tp2_enabled: bool = True, flt=None,
    min_recommendation: Optional[float] = None,
    consecutive_losses: int = 0,
) -> dict:
    """对单个币种进行 AI 分析（单次调用，返回原始 JSON dict）。

    signal: 包含 symbol/signal_type/current_price/breakout_pct/pattern/signal_reason/
            position/volume_type/volume/volume_24h（recent_swings 可选注入）
    klines: 原始 K 线数据 [[open_time, open, high, low, close, volume, ...], ...]
    strategy_prompt: 用户自定义交易策略（MD 格式），提供时附加到系统提示词供 AI 参考
    user_input: 本次分析的用户补充说明（要求/持仓计划/个人观点），附加到用户提示词
    htf_klines: 可选 4h 高周期 K 线（大方向背景；None 时注入块缺省 4h 行）
    risk_*: 止损金额模式口径（2026-09-27），仅 risk_label 注入提示词
    stop_cap_pct: 止损宽度硬上限（max_stop_pct 补丁参数，比例；None=不限制），
    注入门槛块供 AI 自验（2026-09-29）
    interval: K线周期标签（如 "1h"/"15m"），注入 CSV 表头让模型明确时间尺度
    （kline_interval 可在系统配置页修改，勿硬编码）
    tactics_block: 战法库判定块（2026-10-02，调用方预渲染；空=无战法不注入）
    rr_min: 盈亏比铁律下限（2026-10-02 上页面覆盖；None=env 默认）
    crash_pct: BTC 急跌阈值 %（2026-10-02 上页面覆盖；None=env 默认）
    tp2_enabled: TP2 分批止盈开关（2026-10-08，system_config.tp2_enabled）——
    关闭时提示词剔除止盈二条目且输出硬清零（AI 不产出第二档止盈价）
    flt: 筛选参数组（resolve_scan_filters(cfg)，2026-10-12）——门槛块阈值/
    触点启用列表读它渲染，None=env/代码默认
    min_recommendation（2026-10-21）：生效评分门槛（下方开仓分发门槛同源），
    注入「评分纪律」段；None=env 默认
    """
    client = _get_client()
    result = _call_llm(
        client,
        _build_messages(signal, klines, strategy_prompt, user_input, htf_klines,
                        risk_label=risk_label, stop_cap_pct=stop_cap_pct,
                        interval=interval, tactics_block=tactics_block,
                        rr_min=rr_min, crash_pct=crash_pct,
                        tp2_enabled=tp2_enabled, flt=flt,
                        min_recommendation=min_recommendation,
                        consecutive_losses=consecutive_losses),
    )
    if not tp2_enabled:
        # TP2 关闭兜底清零（提示词已要求输出 0；防止模型违规输出污染落库记录）
        result["take_profit_2"] = 0
    return result


def _fp(v) -> str:
    """K线价格压缩到定点小数（docs/24 提示词瘦身，P0/P1 共用）。

    %g 会把大价格变成科学计数法（64235→6.424e+04，反直觉且精度错觉），
    故按价位段动态小数位。各段最差相对误差 ≤0.05%（实测候选池 387 币分布，
    2026-09-30）：≥1000→1位(0.005%)、≥100→2位、≥1→3位、≥0.001→6位(0.05%)、
    <0.001→8位——小价位币（NEIRO/MEME/MEW 等 6 个在池币）6 位截断误差
    可达 0.54%＞摆动锚 0.4% 缓冲，必须加档（8 位时 NEIRO 误差 0.005%）。
    """
    v = float(v)
    if v >= 1000:
        return f"{v:.1f}"
    if v >= 100:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.3f}"
    if v >= 0.001:
        return f"{v:.6f}"
    return f"{v:.8f}"


def build_signal_facts(
    signal: dict, klines: list,
    htf_klines: Optional[list] = None,
    *, stop_cap_pct: Optional[float] = None, flt=None,
    min_recommendation: Optional[float] = None,
    consecutive_losses: Optional[int] = None,
    interval: str = "1h",
    market_summary: str = "",
    tactics_block: str = "",
) -> str:
    """P0/P1 共享的事实包正文（T3-3，2026-10-21）。

    两条管线此前各写一份事实渲染：字段顺序、量能口径、K线列、是否带 ATR 行
    全不一致，同一信号喂给模型的"事实"不同——4h 定向 / 1h 入场体系下这是
    不可接受的偏差。现收敛为单一渲染器，P1 仅在尾部追加工具能力段。

    market_summary 由 P0 传入（单次调用无工具，环境必须预取）；P1 走工具循环
    按需取，传空串。其余字段两管线完全一致。
    """
    # 取最近 40 根已收盘 K 线（klines[-1] 未收盘，用 klines[-41:-1]）。
    # 60→40（2026-09-28）：CSV 是 user 消息 token 大头（60 根 ≈5000+），思考型模型
    # reasoning 时长与输入量正相关；浓缩结论（指标/摆动块）已覆盖形态识别所需，
    # 窗口内 EMA/MACD 等指标块照常现算不受影响
    closed = klines[:-1] if len(klines) >= 2 else klines
    recent = closed[-40:]
    # 每根K线自带成交量（quote_vol 删除：放量判定看 vol 相对倍数，绝对成交额
    # 单次管线无行为指向）；timestamp 删绝对时间——K线序列天然等间隔，
    # 模型只需顺序不需钟点，省 10 字符/根
    kline_summary = "\n".join(
        f"{_fp(k[1])},{_fp(k[2])},{_fp(k[3])},{_fp(k[4])},{_fp(k[5])}" for k in recent
    )

    # 信号形态出现位置标签
    position_label = POSITION_LABEL_MAP.get(signal.get("position") or "", "")

    # 均线形态（EMA21/55/144 状态判定，基于已收盘 K 线实时计算）
    ema = analyze_ema(klines)
    ema_summary = ""
    if ema:
        ema_summary = f"均线形态: {ema['state_label']}（{ema['detail']}）\n"

    # 技术指标（MACD/布林带/RSI/SuperTrend/ADX/OBV + 4h背景 + 程序化共振结论）
    # 标题按触发类型渲染（2026-10-21）：4h 只作环境数据，回踩标注「方向票=确认度参考」
    indicator_summary = render_indicator_block(
        klines, htf_klines, trigger_type=signal.get("signal_type"))

    # 近期摆动结构（HH/LH/HL/LL，收盘价摆动点）
    sw = signal.get("recent_swings") or {}
    swings_summary = ""
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
        swings_summary = "\n".join(sw_lines) + "\n"

    # 量能（T0-1 修正）：`signal["volume"]` 是原始成交量（币/张数），不是量比；
    # 量比口径 = 近 20 根已收盘 K 线中位量的倍数（indicators.volume_ratio）。
    # 此前把原始量渲染成「×20根均量」，与门槛块的「中位量 1.2 倍」自相矛盾。
    vr = signal.get("volume_ratio")
    try:
        vr = float(vr) if vr is not None else float(volume_ratio(klines))
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        vr = None

    vol_desc = signal.get("volume_type") or "—"
    if vr is not None:
        vol_desc += f"（量比 {vr:.2f}×近20根中位量）"
    raw_vol = signal.get("volume")
    vol_line = f"量能: {vol_desc}\n"
    if raw_vol:
        vol_line += f"原始量: {float(raw_vol):,.0f}（币/张数，仅记录，不参与判断）\n"

    # 质量分（T0-3）：含分项拆解
    quality_line = render_quality_line(signal)

    # 分析阶段静态门槛；账号级连损动态门槛由执行层单独复核
    _eff_min = (
        float(min_recommendation) if min_recommendation is not None
        else static_min_recommendation()
    )
    threshold_line = f"AI 分析阶段静态门槛: {_eff_min:g}——低于此分不分发开仓\n"
    if consecutive_losses is not None and derate_threshold(consecutive_losses):
        threshold_line += (
            "连损动态门槛: 部分账号可能提高；实际按账号在开仓时单独复核，"
            "不要据此统一 skip\n"
        )

    # 回踩触点（docs/22 触点体系）：程序已验证的触碰位类型，触点位价在信号理由中
    touch_ref = signal.get("touch_ref")
    touch_line = ""
    if touch_ref:
        touch_line = f"回踩触点: {TOUCH_LABELS.get(touch_ref, touch_ref)}（{touch_ref}）\n"

    breakout = signal.get("breakout_pct")
    breakout_line = (
        f"突破幅度: {float(breakout):.2f}%\n" if breakout is not None else ""
    )

    return (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {SIGNAL_TYPE_LABELS.get(signal['signal_type'], signal['signal_type'])}（{signal['signal_type']}）\n"
        f"当前价格: {signal['current_price']}\n"
        f"{breakout_line}"
        f"K线形态: {signal.get('pattern') or '无'}\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"{touch_line}"
        f"{render_signal_gate_block(signal['signal_type'], stop_cap_pct=stop_cap_pct, flt=flt, direction=signal.get('direction'))}"
        f"形态出现位置: {position_label or signal.get('position') or '未知'}\n"
        f"{vol_line}"
        f"{quality_line}"
        f"{threshold_line}"
        f"{ema_summary}"
        f"{indicator_summary}"
        f"{market_summary}"
        f"{swings_summary}"
        f"{tactics_block}"
        f"近{len(recent)}根已收盘{interval} K线(open,high,low,close,vol，等间隔排列，价格按价位段保留小数):\n{kline_summary}"
    )


def _build_messages(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    htf_klines: Optional[list] = None,
    risk_label: str = "",
    stop_cap_pct: Optional[float] = None,
    interval: str = "1h",
    tactics_block: str = "",
    rr_min: Optional[float] = None,
    crash_pct: Optional[float] = None,
    tp2_enabled: bool = True,
    flt=None,
    min_recommendation: Optional[float] = None,
    consecutive_losses: Optional[int] = None,
    diag: Optional[dict] = None,
) -> list:
    """构造 LLM messages（analyze_coin / analyze_with_guard 共用）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，注入系统提示词
    仓位口径说明；空时用默认百分比口径。
    stop_cap_pct（2026-09-29）：止损宽度硬上限，注入门槛块（None=不限制不注入）。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，注入系统提示词（None=env 默认）。
    crash_pct（2026-10-02 上页面）：BTC 急跌阈值 %，透传市场环境块（None=env 默认）。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关，注入系统提示词（None 不适用，
    默认 True）。
    flt（2026-10-12）：筛选参数组，透传门槛块渲染（None=env/代码默认）。
    min_recommendation（2026-10-21）：生效评分门槛，注入「评分纪律」段（None=env 默认）。

    2026-10-21（T3-3）：事实包正文由 build_signal_facts 统一渲染，P1 复用同一
    函数，仅追加工具能力段；本函数只负责 P0 独有的市场环境预取与系统提示词。
    """
    # 市场环境（docs/24 A4①）：本币资金费率 + BTC 状态摘要，紧跟指标块注入——
    # 系统提示词要求"勿臆测"，普跌日无环境感知 AI 只能凭本币指标判断；
    # 开关关闭或任一数据源失败静默降级为不注入（测试/无网路径不受影响）
    # T0-5：缺数据不再按「无环境」隐式放行，显式标注未知，禁止据此加分
    market_summary = ""
    # Disabled means deliberately not requested, not a data-source failure.
    _env_missing = False
    if settings.MARKET_CONTEXT_ENABLED:
        _env_missing = True
        try:
            ctx = build_market_context(get_kline_client(), signal["symbol"],
                                       crash_pct=crash_pct)
            if ctx:
                market_summary = f"【市场环境】\n{ctx}\n"
                _env_missing = False
            else:
                market_summary = "【市场环境】\nBTC 环境: 未知（数据缺失，不得据此加分或降分）\n"
        except Exception as e:
            logger.debug("市场环境块构建失败（标注未知）: %s", e)
            market_summary = "【市场环境】\nBTC 环境: 未知（数据缺失，不得据此加分或降分）\n"
    if diag is not None:
        # 仅已启用但取数失败时标缺失；关闭功能不计入数据故障。
        diag["btc_env_missing"] = _env_missing

    user_prompt = build_signal_facts(
        signal, klines, htf_klines,
        stop_cap_pct=stop_cap_pct, flt=flt,
        min_recommendation=min_recommendation,
        consecutive_losses=consecutive_losses,
        interval=interval, market_summary=market_summary,
        tactics_block=tactics_block,
    )

    if user_input:
        user_prompt += (
            "\n\n用户补充说明（用户基于自身判断提供的信息或要求，请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )

    system_prompt = render_system_prompt(risk_label or None, rr_min=rr_min,
                                         tp2_enabled=tp2_enabled,
                                         min_recommendation=min_recommendation,
                                         consecutive_losses=consecutive_losses,
                                         trigger_type=signal.get("signal_type"))
    # 技能注入（命中技能正文直接进系统提示词；事实判定由 skill_library 统一产出）
    skill_block = skill_library.render_matched(skill_library.build_facts(signal, klines))
    if skill_block:
        system_prompt += (
            "\n\n以下技能与当前信号命中相关（use_when 条件已程序判定），请遵循其规则"
            "（优先级：用户自定义策略 > 技能规则 > 默认纪律，冲突时取高优先级）：\n"
            f"{skill_block}"
        )
    if strategy_prompt:
        system_prompt += (
            "\n\n以下是用户自定义交易策略，请优先遵循其规则进行分析，"
            "与默认规则冲突时以自定义策略为准：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return messages


class _LLMOutputError(ValueError):
    """LLM 输出不可用（空内容/解析失败），携带原始 content 供 Reflection 反馈。

    解析失败重试时模型需要看到自己上一次的输出才能针对性修正（docs/24 D2）；
    异常抛出点在 _call_llm 内部，content 只有这里拿得到，附在异常上传递。
    """

    def __init__(self, msg: str, content: str = ""):
        super().__init__(msg)
        self.content = content


def _call_llm(client: OpenAI, messages: list, progress_id=None, attempt: int = 0) -> dict:
    """单次 LLM 调用（流式）→ 原始 JSON dict；空内容/解析失败抛 _LLMOutputError（携带原文）

    流式（2026-09-30 网关挂死治理，见 llm_client.stream_completion）：GLM 推理模型
    非流式长推理零字节返回会被读超时误杀（线上 4 连挂实测），流式下 reasoning
    增量持续到达保持连接活跃；顺带获得首 token / 总耗时延迟观测。
    progress_id（2026-09-30 方案 B 轻量进度）：传入 scan_result_id 时把流式增量
    计数节流推送为 ai_progress "llm" 事件（≥2s 一条），前端展示「推理中」活进度；
    push 内部静默失败，进度展示不影响主流程。
    """
    # GLM 系列为思考型模型，reasoning tokens 也计入 max_tokens，
    # 配额太小会导致正文为空或被截断（finish_reason=length）：
    # 12000 曾被思考过程整体耗尽（2026-09-28 线上：3 次重试全空内容强制 skip），放大至 32000
    on_progress = None
    if progress_id is not None:
        from app.services import ai_progress

        def on_progress(m: dict) -> None:
            ai_progress.push(progress_id, "llm", attempt=attempt, **m)

    r = stream_completion(
        client, messages,
        temperature=0.3,
        max_tokens=32000,
        on_progress=on_progress,
        **json_mode_kwargs(),  # LLM_JSON_MODE 开启时带 response_format（docs/24 D2）
    )
    finish = r.finish_reason
    content = r.content
    logger.info(
        "AI 返回原始内容(finish=%s 首token=%.1fs 总耗时=%.1fs): %s",
        finish, r.first_token_s, r.total_s, content[:300],
    )
    if not content:
        raise _LLMOutputError(f"AI 返回空内容 (finish_reason={finish})")
    # 容错处理：去掉可能的 markdown 包裹
    if content.startswith("```"):
        lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    # 标准解析失败降级 json_repair（未转义引号/尾逗号等 LLM 常见语法瑕疵）
    try:
        return parse_llm_json(content)
    except (ValueError, json.JSONDecodeError) as e:
        raise _LLMOutputError(str(e)[:150], content) from e


def analyze_with_guard(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    htf_klines: Optional[list] = None,
    risk_mode: str = "pct", risk_amount: float = 0.0, risk_label: str = "",
    stop_cap_pct: Optional[float] = None, interval: str = "1h",
    progress_id=None, tactics_block: str = "",
    rr_min: Optional[float] = None, risk_pct: Optional[float] = None,
    crash_pct: Optional[float] = None, tp2_enabled: bool = True, flt=None,
    rr_tol: Optional[float] = None, min_recommendation: Optional[float] = None,
    consecutive_losses: int = 0,
) -> dict:
    """带 Risk Guard 的分析入口：校验失败把违规明细反馈给模型重试（≤2 次），耗尽后强制 skip。

    返回值为校验修正后的决策 dict（rr/position_pct 为程序复算值），可直接落库。
    risk_*（2026-09-27 止损金额模式）：risk_label 注入提示词；fixed 模式下
    risk_amount 作为预算实额传入仓位复算（与开仓引擎同口径）。
    stop_cap_pct（2026-09-29）：止损宽度硬上限注入门槛块（None=不限制）。
    interval：K线周期标签（与取数口径一致，ai_tasks 传 cfg.kline_interval）。
    progress_id：scan_result_id，透传 _call_llm 推送 ai_progress 活进度事件（None=不推）。
    tactics_block：战法库判定块（2026-10-02，调用方预渲染；空=无战法不注入）。
    rr_min / risk_pct / crash_pct（2026-10-02 上页面）：盈亏比铁律下限、单笔
    风险比例与 BTC 急跌阈值，注入提示词并透传 Risk Guard 校验（None=env 默认）。
    rr_tol（2026-10-14）：RR 铁律/TP1 上限容差（cfg.ai_rr_tol 透传，None=env 默认）。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时提示词剔除止盈二条目，
    且在风控校验前硬清零 take_profit_2（AI 不产出第二档止盈价）。
    flt（2026-10-12）：筛选参数组，透传门槛块渲染（None=env/代码默认）。
    min_recommendation（2026-10-21）：生效评分门槛，注入「评分纪律」段（None=env 默认）。"""
    _diag: dict = {}
    prompt_messages = _build_messages(
        signal, klines, strategy_prompt=strategy_prompt, user_input=user_input,
        htf_klines=htf_klines, risk_label=risk_label, stop_cap_pct=stop_cap_pct,
        interval=interval, tactics_block=tactics_block, rr_min=rr_min,
        crash_pct=crash_pct, tp2_enabled=tp2_enabled, flt=flt,
        min_recommendation=min_recommendation,
        consecutive_losses=consecutive_losses,
        diag=_diag,
    )
    client = _get_client()
    _risk_budget = risk_amount if risk_mode == "fixed" and risk_amount > 0 else None

    last_violations: list[str] = []
    for attempt in range(3):  # 1 次初始 + 2 次反馈重试
        try:
            raw = _call_llm(client, prompt_messages, progress_id=progress_id, attempt=attempt)
        except _LLMOutputError as e:
            last_violations = [f"输出无法解析: {str(e)[:150]}"]
            logger.warning("AI 第%d次输出解析失败: %s", attempt + 1, e)
            # 解析失败同样走 Reflection 反馈（docs/24 D2）：此前原样重发 messages，
            # 模型不知道上次错在哪，同一毛病会反复犯到重试耗尽。错误摘要截断至
            # 200 字符附进上下文；上次输出非空时以 assistant 消息回放进上下文，
            # 模型看着自己的原文改错（空内容场景无原文，仅附错误说明）。
            # 覆盖两条失败路径——直接解析抛异常（空内容/JSONDecodeError）与
            # json-repair 修复后仍非法（parse_llm_json 重抛）
            if e.content:
                prompt_messages.append({"role": "assistant", "content": e.content})
            prompt_messages.append({
                "role": "user",
                "content": (
                    f"上一次输出 JSON 解析失败：{str(e)[:200]}。"
                    "请严格只输出合法 JSON 对象，不要任何其他文本"
                ),
            })
            continue

        # TP2 关闭硬清零（校验前生效：Risk Guard 的 TP2 排序检查对 0 值自动跳过）
        if not tp2_enabled and isinstance(raw, dict):
            raw["take_profit_2"] = 0

        ok, violations, fixed = parse_and_validate(
            raw, signal, klines, risk_budget=_risk_budget,
            rr_min=rr_min, risk_pct=risk_pct, rr_tol=rr_tol,
        )
        if ok:
            logger.info(
                "AI 决策通过校验: %s %s rr=%s",
                signal.get("symbol"), fixed.get("trade_decision"),
                fixed.get("risk_reward_ratio"),
            )
            fixed["btc_env_missing"] = bool(_diag.get("btc_env_missing", True))
            return fixed
        last_violations = violations
        logger.warning(
            "AI 决策未过校验(第%d次): %s", attempt + 1, violations,
        )
        # 违规明细作为反馈消息进入上下文，模型看着错处修改（Reflection）
        prompt_messages.append({
            "role": "assistant",
            "content": json.dumps(raw, ensure_ascii=False),
        })
        prompt_messages.append({
            "role": "user",
            "content": (
                "你的决策未通过程序风控校验，违规项如下：\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\n请逐条修正后，重新输出完整 JSON（字段要求与最初一致）。"
            ),
        })

    _forced = build_forced_skip("AI 输出多次未通过风控校验", last_violations)
    _forced["btc_env_missing"] = bool(_diag.get("btc_env_missing", True))
    return _forced
