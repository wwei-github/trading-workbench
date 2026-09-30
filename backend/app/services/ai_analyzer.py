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
from app.services.strategy.ema import analyze_ema
from app.services.strategy.technical import render_indicator_block
from app.services.strategy.trigger import TOUCH_LABELS
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

# ── 提示词公共片段：P0/P1 共享的纪律段（改一处两管线同步生效，防文本漂移）──
# 4h 方向硬约束前置（长文本首部是注意力高区；唯一"程序无法补救只能 skip"的规则）
_RULE_HTF_HARD = """0. **方向硬约束（最高优先级，先于一切规则）**：技术指标块出现「方向约束（硬性）」行时必须遵守——
4h强多头背景仅可开多（开空一律 skip），4h强空头背景仅可开空（开多一律 skip）；
即使 1h 指标多数决指向反方向也以约束为准（1h 逆强背景信号视为回调噪音）。"""

_RULE_VOTE = """指标多数决规则：用户消息"技术指标"块末尾的"指标方向"行为程序化统计
（MACD/布林带/RSI/SuperTrend/ADX/OBV/4h背景 各投多/空/中性票）。
多空票数谁多以谁为主——即使 1h K线形态或摆动结构与多数方向矛盾，也以多数方向为准，
并在 analysis 第一条写明"指标方向: 看多N项 vs 看空M项"及各指标依据；
多空票数平衡时自行结合 K线结构与量能判断。"""

_RULE_EMA = """EMA 排列不构成方向限制：多头排列不禁止做空、空头排列不禁止做多，
均线仅是趋势背景参考之一（金叉/死叉/拐头是动能转换信号）。"""

_RULE_SL = """止损纪律（结构位优先，影线极值兜底，两锚取更远者；程序不再改写止损价，合理性自行把关）：
- 优先：做多锚定入场价下方最近摆动低点外 0.3%（建议 0.3%~0.5%），做空锚定上方最近摆动高点外同幅度。
- 两锚取更远者（防止损落进近期波动区间内被扫损）：做多取摆动低点与最近 5 根最低点中更低者外 0.3%，做空取摆动高点与最近 5 根最高点中更高者外 0.3%。
- 兜底（入场价与极值间无摆动结构位时用极值锚）：做多严格低于最近 5 根最低点（影线极值即最低价，非收盘价）至少 0.3%，做空严格高于最高点至少 0.3%。
- 止损宽度不设固定百分比上限；盈亏比不达标先调入场价，仍不行则 skip。"""

_RULE_TP = """止盈锚定（多空方向不同，必须遵守；程序不再改挂止盈价）：
- 结构位=摆动高低点（用户消息中"近期摆动结构"）。距现价 ≤1×ATR 的位是正在
  测试的争夺位、不算止盈目标，止盈一必须挂更远的第一档结构位外侧：
  做多 = 上方摆动高点下方 0.2%~0.5%（不得越到位上方或贴死位）；
  做空 = 下方摆动低点上方 0.2%~0.5%（不得越到位下方或贴死位）。
- 禁止跳档凑盈亏比：到第一结构位不足 {rr_min}R 就是空间不足，应直接 skip
  （程序按盈亏比铁律复算打回）。
- 止盈二：止盈一锚定档的下一档结构位外侧（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方），留同幅余地，且必须比止盈一更远。
- 前方无任何结构位可锚（如创新高突破）：止盈一 = 入场价 ± {rr_min}×止损距离
  （盈亏比恰达标），止盈二设 0（仅设一档）。"""

_RULE_STRUCT = """结构破位回踩打法（structure_break）三子形态按行情阶段择一，选择理由写进 analysis：
1. 123法则（趋势反转）：破支撑(1) → 反弹不创新低(2) → 突破反弹高点时入场(3)；止损放转折点外侧。稳但滞后。
2. N字结构（趋势延续）：回调不破支撑 → 突破压力 → 回踩不破入场；止损放 N 字中间低点，破即失效。
3. 2B法则（假突破反手）：创阶段新高即收回位下方（或新低即收回位上方）→ 反向入场；止损放假突破极值外小缓冲。盈亏比最佳但胜率偏低，须严守止损。
共同：震荡市最差（假突破连发），震荡区间信号归 range_edge 不用本打法。"""

_RULE_DIRECTION = """方向纪律（不再程序强制，无 ±0.3% 贴近要求）：优先只在支撑结构做多、
只在压力结构做空；现价已离开结构位时不再要求贴近，可按动能顺势给入场价（顺势追势
入场贴近现价即可），入场是否合理由盈亏比与止损结构把关。"""

# 回踩触点语义注解（docs/22 触点体系，2026-09-28）：信号块"回踩触点"行的解读口径
_RULE_TOUCH = """回踩触点语义（信号块出现"回踩触点"行时程序已验证金K上一根触碰该位企稳）：
retest=突破回测位（破轨后横向验证，结构意义最强，止盈/止损参考被测轨位）、
swing=摆动结构位（HL/LH 防守位）、ema21=趋势动态价值区、ema55/ema144=大周期深回撤
价值区（三道门已程序验证）。有触点时止损优先锚定触点位：做多放触点位
下外侧、做空放上外侧（仍按止损纪律两锚取更远者结合影线极值）。"""

# 信号类型中文对照（结构分类）
SIGNAL_TYPE_LABELS = {
    "uptrend": "上涨趋势（HH+HL）",
    "downtrend": "下跌趋势（LH+LL）",
    "trend_reversal": "趋势反转（收盘价破前高/前低）",
    "range_bound": "震荡区间",
    "breakout": "放量突破（收盘破 Donchian 通道 + 放量）",
    # 指标驱动筛选（2026-09-17）：触发器类型（方向已由指标多数决确定）；
    # 括号内注明程序已验证的入场门槛（2026-09-25 三重收紧 + docs/19 两段式），AI 据此评估信号质量
    "trigger_breakout": "指标同向·突破前高触发（收盘越过未破的摆动结构前高）",
    "trigger_trend_breakout": "指标同向·趋势突破触发（收盘破 N 根 Donchian 滚动高低轨，强趋势连续新高/新低无未破结构位可越时的追势入场）",
    "trigger_candlestick": "指标同向·K线形态触发（旧类型，2026-09-29 纯金K下线，仅历史数据）",
    "trigger_pullback": "指标同向·回踩触发（旧类型，已被两段式金K确认取代，仅历史数据）",
    # 两段式回踩确认（2026-09-25，docs/19）：回踩触碰后等金K确认才入场；
    # 2026-09-28 docs/22 触点体系：触碰对象扩为 5 类触点（信号块"回踩触点"行注明具体位）；
    # 2026-09-29-2 复合确认：单影线锤/流星单根不算确认，须双锤或锤+趋势K
    "trigger_pullback_confirm": "指标同向·回踩触点后金K确认（上一根触碰触点位企稳、当根出现同向金K才入场；锤形线/流星线单根不算确认，reason 会标注『双锤组合』或『前根锤+当根趋势K』）",
    "unknown": "未分类",
    "manual_search": "手动搜索",
}


def render_signal_gate_block(signal_type: str, stop_cap_pct: Optional[float] = None) -> str:
    """触发器入场门槛说明（2026-09-28 动态化）：阈值读 scan_filters 生效值，
    消除与页面筛选配置的文本分叉（此前 0.2%/1.2× 硬编码，页面改配置后
    AI 看到的口径与程序实际闸门不一致）。按触发类型渲染对应门槛，
    非触发器类型输出空串。"""
    if signal_type not in ("trigger_breakout", "trigger_trend_breakout",
                           "trigger_pullback", "trigger_pullback_confirm"):
        return ""
    from app.services.scan_filters import resolve_scan_filters
    flt = resolve_scan_filters(None)  # 任务侧无 cfg 行时按 env/代码默认
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
            "- 突破K线形态: 长实体（实体 ≥75% 全长，影线合计 ≤25%）",
        ]
    elif signal_type == "trigger_trend_breakout":
        lines += [
            f"- 突破位: 收盘破 {flt.breakout_channel_bars} 根 Donchian 滚动高低轨"
            "（窗口不含信号K线；强趋势无未破结构位可越时的追势入场）",
            f"- 突破幅度: ≥{flt.breakout_threshold * 100:.1f}%（毛边不算）"
            + (f"，>{flt.breakout_max_pct:.1f}% 视为追高不触发" if flt.breakout_max_pct > 0 else "，追高上限未启用"),
            f"- 放量（加分项）: 信号K线量 ≥ 近20根中位量 {flt.breakout_vol_ratio:g} 倍 → 质量分+5（不拦截）",
            "- 突破K线形态: 长实体（实体 ≥75% 全长，影线合计 ≤25%）",
        ]
    else:
        if signal_type in ("trigger_pullback", "trigger_pullback_confirm"):
            # 2026-09-28 docs/22 触点体系：触碰对象由纯 EMA21 扩为 5 类触点，
            # 具体命中位看信号块"回踩触点"行（trigger 按优先级只标一个）
            lines += [
                "- 回踩触点: retest=突破回测位 / swing=摆动结构位 / ema21 / ema55 / ema144"
                "（优先级 retest>swing>ema21>ema55>ema144，命中位见信号块「回踩触点」行）",
                f"- 横向位触碰带宽: 影线可入带（{flt.structure_touch_band_pct:.1f}% 内）且收盘守住位；"
                "EMA 触点=下影触及且收盘收回线上（做空对称）",
                "- 两段式确认: 触碰后当根出现同向金K，或上根触碰+本根金K（次根确认）",
            ]
            if cap_line:
                lines.append(cap_line)
            return ("程序入场门槛（已程序化验证，评估信号质量时以此为基准而非自行估算）:\n"
                    + "\n".join(lines) + "\n")
    # 突破类公共段：止损锚降级链（docs/23，2026-09-29）——近锚只救
    # "万事俱备、只差盈亏比"的突破单，其余条件任一不满足时无近锚资格
    lines += [
        "- 止损锚（突破类专用）: 结构锚（摆动前低下方 0.3% 缓冲，做空=前高上方）优先；"
        "仅当按结构锚复算盈亏比 <1.5 且调整入场价仍无法达标时，才降级近锚——突破K线（本信号K线）"
        "极值外 0.3% 缓冲（做多=最低价下方，做空=最高价上方），止盈与盈亏比按新锚复算；"
        "近锚达标才可建议开单，并输出 stop_anchor=\"near\"（结构锚达标时输出 \"structure\"）；"
        "近锚仍不达标 → skip",
        "- 近锚适用前提（硬约束）: 仅当 skip 的唯一原因是盈亏比不达标——信号方向、4h 背景、"
        "趋势状态、量能特征、质量分等其余开单条件必须全部满足，否则照常 skip",
        "- 近锚资格附加线: 量比 <1 的缩量突破无近锚资格；近锚后盈亏比 >4 时重新评估或 skip；"
        "近锚止损宽度仍须 ≥0.3×ATR",
    ]
    if cap_line:
        lines.append(
            cap_line + "；结构锚宽度已超上限时无近锚降级资格，直接 skip"
        )
    return ("程序入场门槛（已程序化验证，评估信号质量时以此为基准而非自行估算）:\n"
            + "\n".join(lines) + "\n")


SYSTEM_PROMPT = """你是加密货币合约交易分析师。基于提供的信号与近期K线数据，给出是否开单的建议。
直接输出JSON，不要思考过程，不要markdown包裹。

【最高优先级硬约束】
{_RULE_HTF_HARD}

字段如下（skip 时为 0/空字符串的字段：direction/trade_type/entry_price/stop_loss/
take_profit_1/take_profit_2/risk_reward_ratio/position_pct；skip_reason 除外，见下）：
- trade_decision: 是否建议开单，"suggest"（建议开单）或 "skip"（不建议开单）
- skip_reason: trade_decision="skip" 时必填，用"1. 2. 3."序号逐条列出不建议开单的主要原因（如：1. 信号强度不足 2. 盈亏比不佳），最多3条，每条一行，JSON内换行写\\n。suggest 时输出空字符串。
- direction: 交易方向，"long"（做多）或 "short"（做空）。skip 时为空字符串。
- trade_type: 开单类型（结构打法归类），suggest 时必填其一：trend_follow（顺势，方向由 EMA 排列或指标共振确立，纯 EMA 排列不是硬门槛）/ structure_break（结构破位回踩，涵盖 123法则·N字结构·2B法则，见下方打法详解）/ range_edge（区间边缘反转）。skip 时为空字符串。
- stop_anchor: 止损锚类型，仅突破类信号需输出："structure"（结构锚）或 "near"（突破K极值近锚），资格与降级链以程序入场门槛块为准；其他信号类型输出空字符串。
- entry_price: 建议入场价。必须以"当前价格"（分析时刻最新价）为基准：顺势追势时入场贴近现价；
  计划等回踩时给回踩锚定价（摆动低/高点）并在 analysis 中写明"等回踩"（程序会据此标记
  "待回踩"，仅作展示、不会为其下单）；若现价已显著离开
  摆动结构位且无合理入场计划，应 skip。
- 方向纪律：{_RULE_DIRECTION}
- stop_loss: 止损价，做多时低于入场价，做空时高于入场价，基于结构位。
  三价硬约束（程序强制，违反直接打回浪费重试）：做多必须 SL < 入场 < TP1，做空必须
  TP1 < 入场 < SL。止损锚定只看入场价不利侧的摆动结构/影线极值——
  做空绝不允许锚定低点、做多绝不允许锚定高点。
- take_profit_1: 第一档止盈价，做多=上方摆动高点下方留 0.2%~0.5% 余地，
  做空=下方摆动低点上方留 0.2%~0.5%。盈亏比铁律（程序按三价复算打回，声称值无效）：
  RR = |TP1 − 入场| ÷ |入场 − 止损| 必须 ≥ {rr_min}，输出前先自行代入验算
  （近位争夺位不算止盈目标，细则见下方止盈锚定）。
- take_profit_2: 第二档止盈价，锚定更前方一档摆动结构位（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方，同样留余地）且必须比止盈一更远。
- risk_reward_ratio: take_profit_1 的盈亏比，2位小数。
- position_pct: 建议名义仓位占账户资金百分比，2位小数。程序会按固定亏损复算覆盖
  （单笔止损预算={risk_label}：仓位=预算÷止损距离），此处报估算值即可。
- recommendation: 推荐程度，0-100的整数。skip 时≤30，suggest 时≥50。锚点（评分需可区分强度）：
  50=仅勉强达标（单一条件满足，无共振）；70=多重共振（趋势+位置/量能两项以上同向）；
  85+=强信号（趋势+位置+盈亏比三重共振）。中间分数线性内插。
- analysis: ≤300字中文推理，用"1. 2. 3."序号逐条展示，每条独占一行（JSON内换行写\\n），格式如下：
  先逐条列出满足的条件（如：1. EMA多头排列，趋势向上 2. 回踩支撑位企稳，触及2次）；
  再列出不满足的条件或风险点；最后一条写明确结论（如：4. 结论：建议开单，盈亏比与趋势背景共振）。
  trade_decision="skip" 时 analysis 只写一句总括（如：信号强度不足且盈亏比不佳，不建议开单），
  具体理由已在 skip_reason 逐条列出，勿重复。

{_RULE_STRUCT}

{_RULE_SL}

{_RULE_TP}

{_RULE_TOUCH}

所有价格为数值，单位 USDT。
均线与指标纪律：
- {_RULE_EMA}
- {_RULE_VOTE}
- 入场/止损/止盈的结构位锚定规则不受指标方向影响，仍按上方规则执行。

资金费率与 BTC 状态以用户消息【市场环境】块为准（docs/24 A4）；持仓量/恐贪等其余
环境数据未提供，勿臆测：仅基于给定的 K 线、量能、均线、摆动结构、技术指标、
4h 背景与环境块数据进行分析，环境块缺失时按无环境数据处理。"""

def render_system_prompt(risk_label: Optional[str] = None) -> str:
    """渲染最终生效的 P0 系统提示词（SYSTEM_PROMPT 为带槽位模板）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，None 时回落
    百分比口径。页面预览与实际调用共用本函数保证同源。"""
    return SYSTEM_PROMPT.format(
        _RULE_HTF_HARD=_RULE_HTF_HARD,
        _RULE_DIRECTION=_RULE_DIRECTION.replace(chr(10), ' '),
        _RULE_STRUCT=_RULE_STRUCT,
        _RULE_SL=_RULE_SL,
        _RULE_TP=_RULE_TP.format(rr_min=settings.AI_RR_MIN),
        _RULE_TOUCH=_RULE_TOUCH,
        _RULE_EMA=_RULE_EMA.replace(chr(10), ' '),
        _RULE_VOTE=_RULE_VOTE.replace(chr(10), ' '),
        rr_min=settings.AI_RR_MIN,
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
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
    """
    client = _get_client()
    return _call_llm(
        client,
        _build_messages(signal, klines, strategy_prompt, user_input, htf_klines,
                        risk_label=risk_label, stop_cap_pct=stop_cap_pct,
                        interval=interval),
    )


def _build_messages(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    htf_klines: Optional[list] = None,
    risk_label: str = "",
    stop_cap_pct: Optional[float] = None,
    interval: str = "1h",
) -> list:
    """构造 LLM messages（analyze_coin / analyze_with_guard 共用）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，注入系统提示词
    仓位口径说明；空时用默认百分比口径。
    stop_cap_pct（2026-09-29）：止损宽度硬上限，注入门槛块（None=不限制不注入）。"""
    # 取最近 40 根已收盘 K 线摘要（klines[-1] 未收盘，用 klines[-41:-1]）。
    # 60→40（2026-09-28）：CSV 是 user 消息 token 大头（60 根 ≈5000+），思考型模型
    # reasoning 时长与输入量正相关；浓缩结论（指标/摆动块）已覆盖形态识别所需，
    # 窗口内 EMA/MACD 等指标块照常现算不受影响
    recent = klines[-41:-1] if len(klines) >= 41 else klines[:-1]

    # K线价格压缩到定点小数（docs/24 提示词瘦身）：%g 会把大价格变成
    # 科学计数法（64235→6.424e+04，反直觉且精度错觉），故按价位段动态小数位。
    # 各段最差相对误差 ≤0.05%（实测候选池 387 币分布，2026-09-30）：
    # ≥1000→1位(0.005%)、≥100→2位、≥1→3位、≥0.001→6位(0.05%)、
    # <0.001→8位——小价位币（NEIRO/MEME/MEW 等 6 个在池币）6 位截断误差
    # 可达 0.54%＞摆动锚 0.3% 缓冲，必须加档（8 位时 NEIRO 误差 0.005%）
    def _fp(v) -> str:
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

    # 每根K线自带成交量（quote_vol 删除：放量判定看 vol 相对倍数，绝对成交额
    # 单次管线无行为指向）；timestamp 删绝对时间——K线序列天然等间隔（1h），
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
    indicator_summary = render_indicator_block(klines, htf_klines)

    # 市场环境（docs/24 A4①）：本币资金费率 + BTC 状态摘要，紧跟指标块注入——
    # 系统提示词要求"勿臆测"，普跌日无环境感知 AI 只能凭本币指标判断；
    # 开关关闭或任一数据源失败静默降级为不注入（测试/无网路径不受影响）
    market_summary = ""
    if settings.MARKET_CONTEXT_ENABLED:
        try:
            ctx = build_market_context(get_kline_client(), signal["symbol"])
            if ctx:
                market_summary = f"【市场环境】\n{ctx}\n"
        except Exception as e:
            logger.debug("市场环境块构建失败（不注入）: %s", e)

    # 近期摆动结构（HH/LH/HL/LL，收盘价摆动点，与 Agent 管线同源）
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

    # 量能（放量突破判定依据，volume_ratio 口径：相对近20根已收盘均量的倍数）
    vol_desc = signal.get("volume_type") or "—"
    if signal.get("volume"):
        vol_desc += f"（{float(signal['volume']):.1f}×20根均量）"

    # 回踩触点（docs/22 触点体系）：程序已验证的触碰位类型，触点位价在信号理由中
    touch_ref = signal.get("touch_ref")
    touch_line = ""
    if touch_ref:
        touch_line = f"回踩触点: {TOUCH_LABELS.get(touch_ref, touch_ref)}（{touch_ref}）\n"

    # 市场环境块（market_summary，docs/24 A4①）已拼在指标块与摆动结构之间；
    # 单次调用管线无工具循环，模型仅凭给定数据分析，环境数据缺失时按无环境处理
    user_prompt = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {SIGNAL_TYPE_LABELS.get(signal['signal_type'], signal['signal_type'])}（{signal['signal_type']}）\n"
        f"当前价格: {signal['current_price']}\n"
        f"突破幅度: {signal['breakout_pct']:.2f}%\n"
        f"K线形态: {signal.get('pattern') or '无'}\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"{touch_line}"
        f"{render_signal_gate_block(signal['signal_type'], stop_cap_pct=stop_cap_pct)}"
        f"形态出现位置: {position_label or signal.get('position') or '未知'}\n"
        f"量能: {vol_desc}\n"
        f"{ema_summary}"
        f"{indicator_summary}"
        f"{market_summary}"
        f"{swings_summary}"
        f"近{len(recent)}根已收盘{interval} K线(open,high,low,close,vol，等间隔排列，价格按价位段保留小数):\n{kline_summary}"
    )

    if user_input:
        user_prompt += (
            "\n\n用户补充说明（用户基于自身判断提供的信息或要求，请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )

    system_prompt = render_system_prompt(risk_label or None)
    # 技能注入（P0 管线无工具循环，命中技能正文直接进系统提示词；与 Agent 管线共用同一事实判定）
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
    progress_id=None,
) -> dict:
    """带 Risk Guard 的分析入口：校验失败把违规明细反馈给模型重试（≤2 次），耗尽后强制 skip。

    返回值为校验修正后的决策 dict（rr/position_pct 为程序复算值），可直接落库。
    risk_*（2026-09-27 止损金额模式）：risk_label 注入提示词；fixed 模式下
    risk_amount 作为预算实额传入仓位复算（与开仓引擎同口径）。
    stop_cap_pct（2026-09-29）：止损宽度硬上限注入门槛块（None=不限制）。
    interval：K线周期标签（与取数口径一致，ai_tasks 传 cfg.kline_interval）。
    progress_id：scan_result_id，透传 _call_llm 推送 ai_progress 活进度事件（None=不推）。
    """
    prompt_messages = _build_messages(
        signal, klines, strategy_prompt=strategy_prompt, user_input=user_input,
        htf_klines=htf_klines, risk_label=risk_label, stop_cap_pct=stop_cap_pct,
        interval=interval,
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

        ok, violations, fixed = parse_and_validate(
            raw, signal, klines, risk_budget=_risk_budget,
        )
        if ok:
            logger.info(
                "AI 决策通过校验: %s %s rr=%s",
                signal.get("symbol"), fixed.get("trade_decision"),
                fixed.get("risk_reward_ratio"),
            )
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

    return build_forced_skip("AI 输出多次未通过风控校验", last_violations)
