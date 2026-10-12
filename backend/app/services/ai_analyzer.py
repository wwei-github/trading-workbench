"""AI 分析服务：调用 OpenAI 兼容接口，为命中币种生成开单建议"""
import json
import logging
from typing import Optional

import numpy as np
from openai import OpenAI

from app.config import settings
from app.services import skill_library
from app.services.llm_client import get_client, parse_llm_json, json_mode_kwargs, stream_completion
from app.services.market_data import build_market_context, get_kline_client
from app.services.risk_guard import build_forced_skip, parse_and_validate
from app.services.strategy.candlestick import golden_whitelist_labels
from app.services.strategy.ema import analyze_ema
from app.services.strategy.technical import atr_series, render_indicator_block
from app.services.strategy.trigger import TOUCH_LABELS
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

# ── 提示词公共片段：P0/P1 共享的纪律段（改一处两管线同步生效，防文本漂移）──
# 4h 方向硬约束前置（长文本首部是注意力高区；唯一"程序无法补救只能 skip"的规则）。
# （2026-10-11 再重构 EMA 状态机 11 态：「过热约束」行改「状态约束」（缠绕/
# 拐头双向禁开），锁逆势集合扩为宏观交叉/恢复/排列六态，见 docs/17）
_RULE_HTF_HARD = """0. **方向硬约束（最高优先级，先于一切规则）**：技术指标块出现「状态约束（硬性）」或「方向约束（硬性）」行时必须遵守——
4h缠绕（无趋势）/多头拐头/空头拐头（排列内 EMA21 连续反向=趋势衰竭警戒）背景双向禁开新仓（long/short 一律 skip，程序会强制改判）；
4h宏观金叉/死叉初期、多头/空头恢复初期、多头/空头排列（宏观同侧态）仅可顺势（逆势一律 skip，程序会强制改判）；
4h牛内回调/熊内反弹（宏观逆侧过渡态）无此约束，但仍应结合质量分与打法合理性自评；
即使 1h 指标多数决指向反方向也以约束为准（1h 逆宏观同侧态背景信号视为回调噪音）。"""

_RULE_VOTE = """指标多数决规则：用户消息"技术指标"块末尾的"指标方向"行为程序化统计
（MACD/布林带/RSI/SuperTrend/ADX/OBV/4h背景 各投多/空/中性票）。
多空票数谁多以谁为主——即使 1h K线形态或摆动结构与多数方向矛盾，也以多数方向为准，
并在 analysis 第一条写明"指标方向: 看多N项 vs 看空M项"及各指标依据；
多空票数平衡时自行结合 K线结构与量能判断。"""

_RULE_EMA = """EMA 排列不构成方向限制：多头排列不禁止做空、空头排列不禁止做多，
均线仅是趋势背景参考之一（金叉/死叉/拐头是动能转换信号）。"""

_RULE_SL = """止损纪律（结构位优先，金K极值兜底，两锚取更远者；程序不再改写止损价，合理性自行把关）：
- 优先：做多锚定入场价下方最近摆动低点外 0.3%（建议 0.3%~0.5%），做空锚定上方最近摆动高点外同幅度。
- 两锚取更远者（防止损落进近期波动区间内被扫损）：金K极值 = 金K窗口（最近 5 根已收盘K线，含信号K/突破K线=事实包 K 线序列最后一根已收盘K线；最后一根未收盘K线不参与）的共同极值——做多取 5 根最低点、做空取 5 根最高点；做多取摆动低点与 5 根最低点中更低者外 0.3%，做空取摆动高点与 5 根最高点中更高者外 0.3%。该极值就是本笔信号的直接失效位（影线极值即最低/最高价，非收盘价）。
- 距离分档（2026-10-12-5）：最近摆动结构位距入场超过 {far_atr}×ATR（=止盈一距离上限÷盈亏比铁律，超过此线盈亏比数学上不可能达标）时，该位是更大级别趋势的失效位而非本笔信号失效位，不作止损锚——直接降级金K锚：做多严格低于金K窗口最低点至少 0.3%，做空严格高于金K窗口最高点至少 0.3%（此时输出 stop_anchor="near"）；突破类信号按本条降级时，量比 <1 的缩量突破无资格。
- 兜底（入场价与金K极值间无摆动结构位时用金K锚）：做多严格低于金K窗口最低点至少 0.3%，做空严格高于金K窗口最高点至少 0.3%。
- 止损宽度不设固定百分比上限；不得以「止损过宽」为由 skip——宽止损由固定亏损法
  自动缩仓消化（止损越远仓位越小，BTWUSDT 等插针极值锚场景本就该宽），
  pin-bar/极端影线场景按技能减半仓位即可。
- 但止损**不得过近**：宽度须 ≥ 0.3×ATR（低于噪音级必被扫），也不得低于 0.3%；
  锚点落在 0.3×ATR 内时把锚外扩到该宽度再报，否则直接 skip（程序按此下限复算打回）。
  盈亏比不达标先调入场价，仍不达标如实 skip。"""

_RULE_TP_BASE = """止盈锚定（多空方向不同，必须遵守；程序不再改挂止盈价）：
- 结构位=摆动高低点（用户消息中"近期摆动结构"）。距现价 ≤1×ATR 的位是正在
  测试的争夺位、不算止盈目标，止盈一必须挂更远的第一档结构位外侧：
  做多 = 上方摆动高点下方 0.2%~0.5%（不得越到位上方或贴死位）；
  做空 = 下方摆动低点上方 0.2%~0.5%（不得越到位下方或贴死位）。
- 第一档止盈须"够近"：距入场价不得超过 {tp1_max_atr}×ATR，也不得超过
  {tp1_max_r}×止损距离。若更远的第一档结构位已越过此上限（强趋势/点位稀疏时
  常见，会落到 3~4R），说明近端无合适目标——不要跳档硬挂远位凑盈亏比，改用
  "入场价 ± {rr_min}×止损距离"作止盈一（程序按此上限复算打回；启用止盈二时
  该远端结构位可保留为止盈二，见下方兜底条）。
- 禁止跳档凑盈亏比：到第一结构位不足 {rr_min}R 就是空间不足，应直接 skip
  （程序按盈亏比铁律复算打回）。"""

# TP2 分批开关（2026-10-08）：关闭时提示词整体剔除止盈二条目——AI 不再产出
# 第二档止盈价（落库 take_profit_2=0，与出场引擎 80% 口径一致）
_RULE_TP2_BULLET_ON = """- 止盈二：止盈一锚定档的下一档结构位外侧（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方），留同幅余地，且必须比止盈一更远。"""

_RULE_TP2_BULLET_OFF = """- 当前未启用止盈二（TP2 分批止盈已关闭）：只规划止盈一，
  take_profit_2 一律输出 0，即使前方有更远结构位也不要给出第二档止盈价。"""

_RULE_TP_TAIL_ON = """- 止盈一兜底，分两种情形：
  (a) 前方无任何结构位可锚（如创新高突破）：止盈一 = 入场价 ± {rr_min}×止损距离
      （盈亏比恰达标），止盈二设 0（仅设一档）。
  (b) 近端无合适结构位、更远的第一档结构位已越过距离上限（{tp1_max_atr}×ATR 或
      {tp1_max_r}×止损距离）：止盈一 = 入场价 ± {rr_min}×止损距离，止盈二保留
      锚定该远端结构位外侧（留 0.2%~0.5% 余地），作为趋势跑手档。"""

_RULE_TP_TAIL_OFF = """- 前方无任何结构位可锚（如创新高突破）、或更远的第一档结构位已越过距离上限
  （{tp1_max_atr}×ATR 或 {tp1_max_r}×止损距离）：止盈一 = 入场价 ± {rr_min}×止损距离
  （盈亏比恰达标）。"""

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
  做空=更低一档摆动低点上方，同样留余地）且必须比止盈一更远；止盈一走兜底价
  （入场±rr_min×止损距离）时，直接锚定远端第一档结构位。"""

_TP2_FIELD_OFF = """当前未启用止盈二（TP2 分批关闭）：一律输出 0。"""

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
下外侧、做空放上外侧（仍按止损纪律两锚取更远者结合金K极值）。"""

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
        "仅流星线单根不算确认，reason 会标注『双流星组合』或『前根倒锤形线（流星）+当根趋势K』；"
        "倒锤形线/上吊线/孕线突破/十字星（蜻蜓/墓碑/中性）不在白名单）"
    ),
    # 趋势回踩（2026-10-10 融合，docs/19）：普通回踩确认 + EMA 趋势门——
    # 4H EMA144 趋势闸门（斜率/缠绕/ADX 三重否决）+ 1H 三线排列/快中线发散度/
    # EMA144 方向全过，其余确认机制与 trigger_pullback_confirm 完全一致
    "trigger_trend_pullback": "指标同向·趋势回踩金K确认（4H EMA144 趋势闸门[斜率≥0.5%/20根+缠绕≤2次+ADX≥20 三重否决]与 1H EMA21>55>144 三线排列、快中线发散度达标、EMA144 方向顺信号方向全过后，回踩触点+同向金K入场；确认机制同回踩金K确认，趋势背景更强）",
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
        "仅当按结构锚复算盈亏比 <1.5 且调整入场价仍无法达标时，才降级近锚——金K窗口"
        "（最近 5 根已收盘K线共同极值，含本信号金K/突破K线）外 0.3% 缓冲"
        "（做多=5 根最低价下方，做空=5 根最高价上方），止盈与盈亏比按新锚复算；"
        "近锚达标才可建议开单，并输出 stop_anchor=\"near\"（结构锚达标时输出 \"structure\"）；"
        "近锚仍不达标 → skip",
        "- 近锚适用前提（硬约束）: 仅当 skip 的唯一原因是盈亏比不达标——信号方向、4h 背景、"
        "趋势状态、量能特征、质量分等其余开单条件必须全部满足，否则照常 skip",
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

【最高优先级硬约束】
{_RULE_HTF_HARD}

字段如下（skip 时为 0/空字符串的字段：direction/trade_type/entry_price/stop_loss/
take_profit_1/take_profit_2/risk_reward_ratio/position_pct；skip_reason 除外，见下）：
- trade_decision: 是否建议开单，"suggest"（建议开单）或 "skip"（不建议开单）
- skip_reason: trade_decision="skip" 时必填，必须以类别前缀开头——[RR不足]/[止损过宽]/[环境逆风]/[形态质量]/[结构不清]/[波动极端]/[其他] 之一；前缀后用"1. 2. 3."序号逐条列出不建议开单的主要原因（如：[RR不足]1. 到第一结构位仅0.5R 2. 上方空间不足），最多3条，每条一行，JSON内换行写\\n。suggest 时输出空字符串。
- direction: 交易方向，"long"（做多）或 "short"（做空）。skip 时为空字符串。
- trade_type: 开单类型（结构打法归类），suggest 时必填其一：trend_follow（顺势，方向由 EMA 排列或指标共振确立，纯 EMA 排列不是硬门槛）/ structure_break（结构破位回踩，涵盖 123法则·N字结构·2B法则，见下方打法详解）/ range_edge（区间边缘反转）。skip 时为空字符串。
- stop_anchor: 止损锚类型："structure"（结构锚）或 "near"（金K窗口极值金K锚）；任何信号类型采用金K极值锚时输出 "near"（距离分档降级或突破类近锚降级链，以止损纪律与程序入场门槛块为准），结构锚输出 "structure"；省略该字段或输出 null 按 structure 理解（禁止输出空字符串）。
- entry_price: 建议入场价。**只允许现价入场（2026-09-30 二次拍板）**：entry 必须贴近
  当前价格（顺势追势），**禁止**把入场价放到现价回踩侧（多单低于现价/空单高于现价）——
  程序对"入场价在现价回踩侧"的建议标记"待回踩"且**不会为其下单**（等于放弃该笔）。
  若按现价入场 RR 无法达标（追入止损过远、到结构位空间不足等），直接 skip，
  **不得给出回踩等待计划**（等回踩建议一律被程序丢弃）；现价已显著离开摆动结构位
  且无合理入场计划时，同样直接 skip。
- 方向纪律：{_RULE_DIRECTION}
- stop_loss: 止损价，做多时低于入场价，做空时高于入场价，基于结构位。
  三价硬约束（程序强制，违反直接打回浪费重试）：做多必须 SL < 入场 < TP1，做空必须
  TP1 < 入场 < SL。止损锚定只看入场价不利侧的摆动结构/影线极值——
  做空绝不允许锚定低点、做多绝不允许锚定高点。
- take_profit_1: 第一档止盈价，做多=上方摆动高点下方留 0.2%~0.5% 余地，
  做空=下方摆动低点上方留 0.2%~0.5%。盈亏比铁律（程序按三价复算打回，声称值无效）：
  RR = |TP1 − 入场| ÷ |入场 − 止损| 必须 ≥ {rr_min}，输出前先自行代入验算
  （近位争夺位不算止盈目标，细则见下方止盈锚定）。距离上限（程序复算打回）：
  距入场 ≤ {tp1_max_atr}×ATR 且 ≤ {tp1_max_r}×止损距离，超限请改挂更近结构位或
  用"入场 ± {rr_min}×止损距离"兜底。
- take_profit_2: {_tp2_field}
- risk_reward_ratio: take_profit_1 的盈亏比，2位小数（按报价如实复算）。
- position_pct: 建议名义仓位占账户资金百分比，2位小数。程序会按固定亏损复算覆盖
  （单笔止损预算={risk_label}：仓位=预算÷止损距离），此处报估算值即可。
- recommendation: 推荐程度，0-100的整数。skip 时≤30，suggest 时≥50。锚点（评分需可区分强度）：
  50=仅勉强达标（单一条件满足，无共振）；70=多重共振（趋势+位置/量能两项以上同向）；
  85+=强信号（趋势+位置+盈亏比三重共振）。中间分数线性内插。
- tactic_name: 战法命中名（2026-10-02，仅借鉴标注不影响开单结论）。用户消息出现
  【战法库判定】块且信号符合其中某个战法时，逐字输出该战法名（禁止改写/自创）；
  均不符合或无战法清单时输出空字符串。
- tactic_reason: 战法判定依据，≤60字（引用战法条件逐条对照结论）；未命中输出空字符串。
- analysis: ≤500字中文推理。**节标题逐字固定**（前端按标题分区展示，改标题即破坏契约）：
  以「公共检查：」开头逐条列公共项判定（1. 4h方向 2. 指标多数决 3. 回踩触点
  4. 形态与量能 5. 风险点——含资金费率拥挤/BTC 大盘急跌等环境风险，无则写"无"），
  随后「决策结论：」节写满足/不满足什么，节内以"结论：建议开单/不建议开单"收尾。
  每条独占一行（JSON内换行写\\n）。
  trade_decision="skip" 时 analysis 仍按上述两节完整输出（决策结论节写不满足的
  主因），skip_reason 不必重复 analysis 已有内容。

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

def render_system_prompt(
    risk_label: Optional[str] = None, rr_min: Optional[float] = None,
    tp2_enabled: bool = True,
) -> str:
    """渲染最终生效的 P0 系统提示词（SYSTEM_PROMPT 为带槽位模板）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，None 时回落
    百分比口径。页面预览与实际调用共用本函数保证同源。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，system_config.ai_rr_min
    覆盖时由调用方传入；None 回落 env 默认。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时剔除止盈二规则与
    字段说明，AI 不再产出第二档止盈价（system_config.tp2_enabled，默认关）。"""
    _rr = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    return SYSTEM_PROMPT.format(
        _RULE_HTF_HARD=_RULE_HTF_HARD,
        _RULE_DIRECTION=_RULE_DIRECTION.replace(chr(10), ' '),
        _RULE_STRUCT=_RULE_STRUCT,
        _RULE_SL=_RULE_SL.format(far_atr=round(settings.TP1_MAX_ATR / _rr, 2)),
        # _RULE_TP 值内含 {rr_min}/{tp1_max_atr}/{tp1_max_r} 字面占位
        # （format 不递归替换值），须渲染前显式替换
        _RULE_TP=compose_rule_tp(tp2_enabled).format(
            rr_min=_rr,
            tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
            tp1_max_r=f"{settings.TP1_MAX_R:g}",
        ),
        _RULE_TOUCH=_RULE_TOUCH,
        _RULE_EMA=_RULE_EMA.replace(chr(10), ' '),
        _RULE_VOTE=_RULE_VOTE.replace(chr(10), ' '),
        _tp2_field=_TP2_FIELD_ON if tp2_enabled else _TP2_FIELD_OFF,
        rr_min=_rr,
        tp1_max_atr=f"{settings.TP1_MAX_ATR:g}",
        tp1_max_r=f"{settings.TP1_MAX_R:g}",
        risk_label=risk_label or f"账户风险基数 × {settings.RISK_BUDGET_PCT:g}%",
    )  # 模板槽位均为纯名称：表达式在渲染前预处理（f-string 时代的 .format 内嵌已移除）


def _get_client() -> OpenAI:
    # 共享单例：复用连接池，统一超时与重试策略（见 llm_client）
    return get_client()


# 程序止损参考缓冲（×ATR14，1h 口径）：锚与提示词止损纪律同语义——候选 =
# 前一同侧摆动点（多头「前一个低点」/空头「前一个高点」）与金K窗口极值
# （最近 5 根已收盘K线的共同最低/最高点，含确认扫描时的金K/突破K线），取更远者
# （更保守），缓冲由 AI 执行的 0.3% 换成 0.5×ATR——AI 拿不到可靠 ATR 做
# 跨品种归一的算术，由程序侧计算
STOP_REF_ATR_MULT = 0.5


def calc_stop_reference(
    signal: dict, klines: list, direction: Optional[str],
) -> Optional[float]:
    """程序止损参考价（2026-10-11）：仅前端对比展示，不改写 AI 报价、不参与下单。

    与 _RULE_SL 锚纪律同语义：锚 = 前一同侧摆动点与金K窗口极值（2026-10-12-8
    恢复窗口口径：最近 5 根已收盘K线的共同最低/最高点，含确认扫描时的金K/
    突破K线 klines[-2]；2026-10-11 单根口径下前一根更低/更高时止损会落进
    近期波动区间内部、必被扫损——C98USDT.P 案例）中更远者（摆动点用现价
    同侧过滤近似 AI 的「入场价下方/上方」——程序不知道 AI 将定的入场价），外扩
    STOP_REF_ATR_MULT×ATR14（Wilder，自剔尾）。方向缺失 / ATR 不可算 /
    K线不足返回 None（旧行与 skip 行为 NULL，前端不显示参考卡）。"""
    if direction not in ("long", "short"):
        return None
    atr_arr = atr_series(klines, period=14)
    if atr_arr is None or not np.isfinite(atr_arr[-1]):
        return None
    closed = klines[:-1] if len(klines) >= 2 else klines
    if not closed:
        return None
    price = float(signal.get("current_price") or closed[-1][4])
    sw = signal.get("recent_swings") or {}
    # 金K窗口（2026-10-12-8 恢复窗口口径）：最近 5 根已收盘K线的共同极值——
    # 做多取 5 根最低点、做空取 5 根最高点（与提示词止损纪律、
    # ai_agent._resolve_price 的 signal_low/signal_high 解析同口径）
    window = closed[-5:]
    if direction == "long":
        anchor = min(float(c[3]) for c in window)  # 5 根最低点
        for s in sw.get("lows", []):
            p = float(s["price"])
            if p < price:
                anchor = min(anchor, p)
                break  # 新→旧，第一个现价下方摆动低点即「前一个低点」
        return anchor - STOP_REF_ATR_MULT * float(atr_arr[-1])
    anchor = max(float(c[2]) for c in window)  # 5 根最高点
    for s in sw.get("highs", []):
        p = float(s["price"])
        if p > price:
            anchor = max(anchor, p)
            break
    return anchor + STOP_REF_ATR_MULT * float(atr_arr[-1])


def analyze_coin(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    htf_klines: Optional[list] = None,
    risk_mode: str = "pct", risk_amount: float = 0.0, risk_label: str = "",
    stop_cap_pct: Optional[float] = None, interval: str = "1h",
    tactics_block: str = "", rr_min: Optional[float] = None,
    crash_pct: Optional[float] = None, tp2_enabled: bool = True,
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
    """
    client = _get_client()
    result = _call_llm(
        client,
        _build_messages(signal, klines, strategy_prompt, user_input, htf_klines,
                        risk_label=risk_label, stop_cap_pct=stop_cap_pct,
                        interval=interval, tactics_block=tactics_block,
                        rr_min=rr_min, crash_pct=crash_pct,
                        tp2_enabled=tp2_enabled),
    )
    if not tp2_enabled:
        # TP2 关闭兜底清零（提示词已要求输出 0；防止模型违规输出污染落库记录）
        result["take_profit_2"] = 0
    # 程序止损参考价（0.5×ATR 外扩）：仅 suggest 落值，前端对比展示用，
    # 不改写 AI 报价；批量管线的注入在编排层统一做（ai_tasks，覆盖双管线）
    if result.get("trade_decision") == "suggest":
        result["calc_stop_loss"] = calc_stop_reference(signal, klines, result.get("direction"))
    return result


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
) -> list:
    """构造 LLM messages（analyze_coin / analyze_with_guard 共用）。

    risk_label（2026-09-27 止损金额模式）：单笔止损预算文案，注入系统提示词
    仓位口径说明；空时用默认百分比口径。
    stop_cap_pct（2026-09-29）：止损宽度硬上限，注入门槛块（None=不限制不注入）。
    rr_min（2026-10-02 上页面）：盈亏比铁律下限，注入系统提示词（None=env 默认）。
    crash_pct（2026-10-02 上页面）：BTC 急跌阈值 %，透传市场环境块（None=env 默认）。
    tp2_enabled（2026-10-08）：TP2 分批止盈开关，注入系统提示词（None 不适用，
    默认 True）。"""
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
            ctx = build_market_context(get_kline_client(), signal["symbol"],
                                       crash_pct=crash_pct)
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
        f"{tactics_block}"
        f"近{len(recent)}根已收盘{interval} K线(open,high,low,close,vol，等间隔排列，价格按价位段保留小数):\n{kline_summary}"
    )

    if user_input:
        user_prompt += (
            "\n\n用户补充说明（用户基于自身判断提供的信息或要求，请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )

    system_prompt = render_system_prompt(risk_label or None, rr_min=rr_min,
                                         tp2_enabled=tp2_enabled)
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
    progress_id=None, tactics_block: str = "",
    rr_min: Optional[float] = None, risk_pct: Optional[float] = None,
    crash_pct: Optional[float] = None, tp2_enabled: bool = True,
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
    tp2_enabled（2026-10-08）：TP2 分批止盈开关——关闭时提示词剔除止盈二条目，
    且在风控校验前硬清零 take_profit_2（AI 不产出第二档止盈价）。"""
    prompt_messages = _build_messages(
        signal, klines, strategy_prompt=strategy_prompt, user_input=user_input,
        htf_klines=htf_klines, risk_label=risk_label, stop_cap_pct=stop_cap_pct,
        interval=interval, tactics_block=tactics_block, rr_min=rr_min,
        crash_pct=crash_pct, tp2_enabled=tp2_enabled,
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
            rr_min=rr_min, risk_pct=risk_pct,
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
