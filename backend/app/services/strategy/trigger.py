"""入场触发器检测（指标驱动筛选管线，2026-09-17；两段式回踩确认 2026-09-25 docs/19；
回踩触点体系 2026-09-28 docs/22；双突破分类+纯金K下线 2026-09-29）

方向由方向票（MACD/布林带/RSI）多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1a trigger_breakout（结构突破）：收盘越过最近未破的摆动结构前高（做多；
  做空=前低对称）——N 字/趋势延续的结构性破位
- T1b trigger_trend_breakout（趋势突破）：收盘破 N 根 Donchian 滚动高低轨
  （BREAKOUT_CHANNEL_BARS，窗口不含信号K线与未收盘根）——强趋势连续新高/新低
  无未破结构位可越时的追势入场；与 T1a 同轮互斥（结构优先，单币单候选），
  两类共享三重收紧：最小幅度 + 放量 + 长实体短影线 + 追高上限（各自按突破位衡量）
- T3 trigger_pullback_confirm：回踩触点企稳 + 同向金K 两段式确认——
  当根同时触碰+金K（当根确认），或触碰根后 1..N 根已收盘K线内出现同向金K
  （次根/N 根内确认，N=pending_confirm_window_bars 默认 4）；
  仅触碰无金K 不再当根触发，转入 pending 待确认池（scanner 持久化）
- 纯金K触发（trigger_candlestick）2026-09-29 下线：金K必须与触点配合，无触碰
  无有效 pending 的金K 一律不触发（正式网复盘 28 笔 6 胜负期望）

回踩触点（docs/22 §2，启用白名单见 ScanFilters.pullback_touch_refs）：
- retest  突破回测位：前高/前低破位价。双来源（2026-10-16）——①事件派生：scanner
  维护 breakout_events（T1 突破触发时记录）；②结构推导：新 HH 收盘越位后，被它越过的
  前一个 HH 即回测位（空头镜像 LL），不依赖先手记录。影线可入带、收盘须守住
  ——回踩不破 = 突破有效性确认
- swing   摆动结构位：最近的 HL（多）/LH（空）防守位，只认结构延续标签；
  2026-10-16 起取最近 3 个同向结构位候选（最近 1 个可能因 merge_swings 合并
  消失而不稳、第 2 个常规、第 3 个兜底），任一满足「影线入带+收盘守位」即命中
- ema21   趋势动态价值区（原口径，不加触点门）；2026-10-15 起叠加 1H 结构门
  软校验（三线排列+快中线发散史+EMA144 方向）——不过门不再否决，改 quality
  重罚（structure_gate=False → -15，docs/21 O9）
- ema55 中回撤：数据门（ema_touch_min_bars）+ 排列门（EMA21 在 EMA55 同侧）
  + 斜率门（近 5 根同向）——走平/错排的慢线是震荡不是回踩
- ema144 深回撤（2026-10-16 复出并默认启用）：数据门 + 走平/顺势门（1h EMA144
  近 SLOPE_BARS 根变化 ≥0 当 4h 为多，做空镜像；|变化|/EMA 落在 SLOPE_FLAT_PCT
  容差带内一律视为走平放行）——不叠加排列门；4h 方向由 scanner
  htf_context.direction 经 htf_dir 传入，4h 数据降级（htf_dir=None）时不出该触点
同根多触点命中按优先级标注（横向结构 > 均线；均线取最快）：
retest > swing > ema21 > ema21_4h > ema55 > ema144

优先级（决策矩阵，docs/19 §4.1）：T1a/T1b 突破 > 当根确认 > 次根/N 根内确认 >
触碰转 pending；pending 在触碰根后 N 根窗口内有效（N = ScanFilters.
pending_confirm_window_bars，默认 4；N=1 即旧「恰好下一根」口径）。

position 语义（docs/09 方向派生口径，2026-09-28 关键位移除后回归）：
看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance。
"""
from datetime import datetime
from typing import Optional, Tuple

from app.services.scan_filters import (
    TOUCH_EMA144,
    TOUCH_EMA21,
    TOUCH_EMA21_4H,
    TOUCH_EMA55,
    TOUCH_RETEST,
    TOUCH_SWING,
    ScanFilters,
    resolve_scan_filters,
)
from app.services.strategy import recent_swings
from app.services.strategy.candlestick import (
    PATTERN_LABEL_MAP,
    detect_all_patterns,
    resolve_golden_evidence,
)
from app.services.strategy.ema import calc_ema
from app.services.strategy.ema_gate import SLOPE_BARS, SLOPE_FLAT_PCT, structure_1h
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.technical import atr_series, pullback_stabilize_bonus
import numpy as np

# 触发类型 = signal_type（AI 提示词 SIGNAL_TYPE_LABELS 已注册同名标签）
BREAKOUT = "trigger_breakout"                  # 结构突破：越过未破摆动前高/前低
TREND_BREAKOUT = "trigger_trend_breakout"      # 趋势突破：破 N 根 Donchian 滚动轨
PULLBACK = "trigger_pullback"          # 历史类型：两段式上线后不再产生新记录
PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩触点后金K确认（当根/次根）

# 回踩触点标注优先级与中文标签（docs/22 §2；与 scan_filters.ALL_TOUCH_REFS 同源顺序；
# 2026-10-09 加入 4h EMA21——均线取最快原则按更新频率排序：1h EMA21 > 4h EMA21 > 1h EMA55）
TOUCH_ORDER = (
    TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA21_4H, TOUCH_EMA55, TOUCH_EMA144,
)
TOUCH_LABELS = {
    TOUCH_RETEST: "突破回测位",
    TOUCH_SWING: "摆动结构位",
    TOUCH_EMA21: "EMA21",
    TOUCH_EMA21_4H: "EMA21(4h)",
    TOUCH_EMA55: "EMA55",
    TOUCH_EMA144: "EMA144",
}


def _touch_ref_label(touch_ref: str, direction: int) -> str:
    """触点方向化措辞（做多=支撑侧语义/做空=压力侧，与前端 touchLabel 同口径）。
    retest 用「前高/前低」（position_ref 拼接「回踩」前缀后语义通顺，不重复「回测」）"""
    if touch_ref == TOUCH_RETEST:
        return "前高" if direction == 1 else "前低"
    if touch_ref == TOUCH_SWING:
        return "摆动低点" if direction == 1 else "摆动高点"
    return TOUCH_LABELS.get(touch_ref, touch_ref)


def _position_ref_of(touch_ref: Optional[str], direction: int) -> Optional[str]:
    """回踩确认的命中位置文案（scan_results.position_ref，前端「位置」列展示）"""
    return f"回踩{_touch_ref_label(touch_ref, direction)}" if touch_ref else None


# 摆动结构位时效门：防守点太老（>40 根前确认）防守意义衰减
SWING_MAX_BARS_AGO = 40

# 摆动结构位候选个数（2026-10-16 用户拍板需求②）：取最近 3 个同向结构位（多=HL/空=LH）——
# 第 1 个（最近）可能因当前窗口内 merge_swings 把摆动点合并吞掉而"随时消失"（不稳），
# 第 2 个是常规防守位，第 3 个兜底；按新→旧顺序取最先满足「影线入带 + 收盘守位」者。
# 取样个数 SWING_TOUCH_SCAN_N 放大到覆盖 3 个同向位（各标签混合，故须多取几个）。
SWING_TOUCH_CANDIDATES = 3
SWING_TOUCH_SCAN_N = 10

# 结构推导回测位（2026-10-16 用户拍板需求①）：新 HH 形成后，被它越过的前一个 HH 即为
# 突破回测位——不依赖 scanner 破轨事件先手记录（breakout_events 只在 T1 结构突破信号
# 产生时才 upsert，而 T1a 要求突破根为长实体顺向K线，非长实体突破的结构位永远记不上）。
# 与事件派生来源共用同一「影线入带 + 收盘守位」触碰口径；新 HH 时效门复用
# ScanFilters.retest_window_bars（默认 24 根；0 仍表示整体关闭 retest 触点）。
RETEST_STRUCT_LOOKBACK_N = 10   # 取摆动点个数（与突破路径同源）

# 突破K线形态门槛（2026-09-30-2 口径，用户拍板）：长实体占据K线主体，影线可有但要短——
# 实体 ≥70% 全长即影线合计 ≤30%（原 75%/25% 放宽，HUMA 个案：影线略长但结构强的突破
# 不再被拒）；防长影毛刺仍由主体占比承担，不做单侧影线额外限制。
# 口径注记（2026-10-10-4）：candlestick.TREND_CANDLE_BODY_RANGE_MIN（趋势K线，
# 复合确认证据层）已放宽至 0.60 并解除同步约定——突破K线是触发层最高置信门，
# 维持 0.70；两常量自即日起各自独立改版
BREAKOUT_BODY_RANGE_MIN = 0.70   # 实体/K线全长 ≥ 0.70

# 回踩前置反向腿门（2026-10-22）：腿终点只取信号K线之前的 N 根已收盘K线
# （不含信号K线本身）——反转K线的自身极值属于反转动作，不算反向腿终点，
# 否则单根反抽 + 吞没/锤子自身振幅即可凑满门槛（APR 类伪回踩漏网）。
# N=2 覆盖启明星/黄昏星的星线在信号K线前一根的情形
LEG_END_WINDOW_BARS = 2

# 腿单调性容差（2026-10-22-2，ARIA 个案）：腿区间内部不得出现比腿终点更深的反向
# 极值（做多=更低低点、做空=更高高点）——否则只是宽幅震荡里的「陈旧腿」：起点之后
# 价格已先反向走得更远再收回，终点并非这段腿的真实极值（ARIA 腿内出现比终点低约
# 2.6 ATR 的低点仍被当作下跌腿，导致上涨后的 retest 看涨吞没被命中）。0.5 ATR 容差
# 吸收毛刺与近似双底/双顶。
LEG_OVERSHOOT_ATR = 0.5


def _is_solid_breakout_candle(klines: list[list], direction: int) -> bool:
    """突破K线必须为顺向长实体，影线可有但要短（2026-09-30-2 口径）。

    量化（2026-09-25 突破策略收紧，2026-09-28 放宽影线口径，2026-09-30-2 实体门
    0.75→0.70 再放宽）：
    - 顺向实体：做多要求阳线（close>open），做空要求阴线（close<open）
    - 长实体：|close-open| / (high-low) ≥ 0.70——实体占据 K 线主体，
      影线合计 ≤30% 即天然短影线，不做单侧影线额外限制
    """
    o = float(klines[-2][1])
    h = float(klines[-2][2])
    l = float(klines[-2][3])
    c = float(klines[-2][4])
    body = abs(c - o)
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    if direction == 1 and c <= o:
        return False
    if direction == -1 and c >= o:
        return False
    return body / rng >= BREAKOUT_BODY_RANGE_MIN


def _diag_note(diag: Optional[dict], text: str) -> None:
    """未触发原因采集（2026-10-13 漏斗「无触发」判定说明）：向 diag["reasons"]
    追加原因文本段，scanner 收口落 funnel_json.details、前端判定说明列按分号
    拆行展示；diag=None 静默（watchlist 等不开采集的调用方零开销）。"""
    if diag is not None:
        diag.setdefault("reasons", []).append(text)


def _pending_touch_label(pending: dict, direction: int) -> str:
    """pending 触点方向化标签（失效原因文案用；缺省「触点」）"""
    ref = pending.get("touch_ref")
    if not ref:
        return "触点"
    try:
        d = int(pending.get("direction") or direction)
    except (TypeError, ValueError):
        d = direction
    return _touch_ref_label(ref, d)


def _expire_note(pending: dict, direction: int, flip: bool) -> str:
    """待确认失效原因文本（direction_flip=方向失守 / window_expired=确认窗口内无金K）"""
    if flip:
        return "待确认失效：待确认方向与当前多数决相反（方向失守）"
    return (f"待确认失效：回踩{_pending_touch_label(pending, direction)}后"
            "确认窗口内未出现同向金K（窗口关闭）")


def _breakout_fail_notes(
    klines: list[list], direction: int, solid: bool, ref: Optional[float],
    flt: ScanFilters,
) -> list[str]:
    """突破路径未命中原因（诊断文本，2026-10-13）：镜像上方 T1a/T1b 闸门顺序
    （实体门 → 结构位 → 追高上限 → 最小幅度 → Donchian 回落口径），逐项给出
    具体卡点；仅在两类突破均未命中时调用，纯诊断、不参与判定。"""
    o = float(klines[-2][1])
    h = float(klines[-2][2])
    low = float(klines[-2][3])
    c = float(klines[-2][4])
    body, rng = abs(c - o), h - low
    up = direction == 1
    thr_pct = flt.breakout_threshold * 100
    max_brk = flt.breakout_max_pct
    notes: list[str] = []
    if not solid:
        if (up and c <= o) or (not up and c >= o):
            notes.append("突破路径未过：信号K非顺向实体（看多需阳线/看空需阴线）")
        elif rng <= 0 or body <= 0:
            notes.append("突破路径未过：信号K无实体或零振幅")
        else:
            notes.append(
                f"突破路径未过：信号K实体占比{body / rng * 100:.1f}%"
                f"<{BREAKOUT_BODY_RANGE_MIN:.0%}（长实体门）")
        return notes
    side = "前高" if up else "前低"
    if ref is None:
        notes.append("突破路径未过：无未破结构位（前高/前低已被连续破位）")
    else:
        over = (c - ref) / ref * 100 if up else (ref - c) / ref * 100
        if max_brk > 0 and over > max_brk:
            notes.append(f"突破路径未过：越{side}{ref:.6g}达{over:.2f}%，"
                         f"超追高上限{max_brk:g}%")
        elif over < 0:
            notes.append(f"突破路径未过：收盘距{side}{ref:.6g}还差{abs(over):.2f}%")
        else:
            notes.append(f"突破路径未过：收盘仅越{side}{ref:.6g}的{over:.2f}%"
                         f"<最小幅度{thr_pct:.2f}%")
    chan_n = flt.breakout_channel_bars
    if len(klines) < chan_n + 2:
        notes.append(f"趋势突破未过：K线不足{chan_n + 2}根")
        return notes
    window = klines[-2 - chan_n:-2]
    chan = max(float(k[2]) for k in window) if up else min(float(k[3]) for k in window)
    rail = "上轨" if up else "下轨"
    over = (c - chan) / chan * 100 if up else (chan - c) / chan * 100
    if max_brk > 0 and over > max_brk:
        notes.append(f"趋势突破未过：越{chan_n}根Donchian{rail}{chan:.6g}"
                     f"达{over:.2f}%，超追高上限{max_brk:g}%")
    elif over < 0:
        notes.append(f"趋势突破未过：收盘距{chan_n}根Donchian{rail}{chan:.6g}"
                     f"还差{abs(over):.2f}%")
    else:
        notes.append(f"趋势突破未过：仅越{chan_n}根Donchian{rail}{chan:.6g}的"
                     f"{over:.2f}%<最小幅度{thr_pct:.2f}%")
    return notes


def _pullback_volume_note(klines: list[list]) -> str:
    """回踩金K量能注记（docs/25 §3.1）：补齐「大量」要素，双口径——

    - 量比 = 信号K线量 ÷ 近 20 根中位量（程序唯一口径，与突破路径一致）
    - 较前根 = 信号K线量 vs 前一根（知识库口径「第二根量超前一根为加分项」）

    注入 reason 后随信号落库 → AI 事实包 / 前端 / 复盘快照全链路可见；
    数据不足（volume_ratio 返回 0）或前根量为 0 时静默省略对应段。
    """
    vr = volume_ratio(klines)
    if vr <= 0:
        return ""
    note = f"·量比{vr:.1f}(vs中位)"
    try:
        prev_v = float(klines[-3][5])
        cur_v = float(klines[-2][5])
        if prev_v > 0:
            note += f"·较前根×{cur_v / prev_v:.1f}"
    except (IndexError, TypeError, ValueError):
        pass
    return note


def _reanchor_stale_leg(
    highs: list[float], lows: list[float], start_idx: int, end_idx: int,
    direction: int, atr: float,
) -> Optional[tuple[int, int, float, float, float]]:
    """陈旧腿重锚（2026-10-22-3，MAGMA 个案）。

    腿区间内部出现比终点更深的反向极值，说明当前起点之前还夹着上一段行情；
    以该内部极值为分界，从其后重新取反向极值作为最近一段腿的起点（只重锚
    一轮；重锚后腿内若仍有更早的陈旧极值，按非单调拒绝，避免级联放宽）。
    返回 (新起点, bars, 起点价, 腿幅, 新 overshoot)；内部极值紧邻腿终点、
    无法重锚时返回 None。
    """
    if direction == 1:
        ext_idx = min(range(start_idx + 1, end_idx), key=lambda i: lows[i])
        if ext_idx + 1 >= end_idx:
            return None
        new_start = max(range(ext_idx + 1, end_idx), key=lambda i: highs[i])
        new_amp = highs[new_start] - lows[end_idx]
        new_over = (
            lows[end_idx]
            - min(lows[new_start + 1:end_idx], default=lows[end_idx])
        ) / atr
        return new_start, end_idx - new_start, highs[new_start], new_amp, new_over

    ext_idx = max(range(start_idx + 1, end_idx), key=lambda i: highs[i])
    if ext_idx + 1 >= end_idx:
        return None
    new_start = min(range(ext_idx + 1, end_idx), key=lambda i: lows[i])
    new_amp = highs[end_idx] - lows[new_start]
    new_over = (
        max(highs[new_start + 1:end_idx], default=highs[end_idx])
        - highs[end_idx]
    ) / atr
    return new_start, end_idx - new_start, lows[new_start], new_amp, new_over


def _pullback_leg_gate(
    klines: list[list], direction: int, flt: ScanFilters,
) -> tuple[bool, dict]:
    """回踩前置反向腿门（2026-10-22）。

    做多要求触点前存在一段下跌腿（前高 → 回踩低点），做空要求一段反弹腿
    （前低 → 回踩高点）；只用已收盘K线（klines[:-1]），按 ATR 归一化，避免
    不同币种波动率差异导致门槛失真。触碰根 = 最后一根已收盘K线（klines[-2]）。

    腿终点在 [t-LEG_END_WINDOW_BARS, t-1] 内取极值（严格在信号K线之前——
    反转K线自身极值不算腿终点，覆盖启明星/黄昏星星线在 t-1 的情形）；腿起点
    默认取终点之前、lookback 窗口内的反向极值。腿区间内部若出现比终点更深的
    反向极值（超出 LEG_OVERSHOOT_ATR），先尝试以该极值为分界重锚最近一段腿
    （MAGMA 个案：更早的高点不属于本次回踩）；重锚后仍不满足单边/门槛则拒绝。
    返回 (是否通过, 证据字典)；ATR 不可用时 fail-closed。
    """
    if direction not in (1, -1):
        return True, {"bars": 0, "atr_mult": 0.0, "reason": "direction_invalid"}
    if flt.pullback_leg_min_bars <= 0 and flt.pullback_leg_min_atr <= 0:
        return True, {"bars": 0, "atr_mult": 0.0, "reason": "disabled"}

    n_closed = len(klines) - 1
    t = n_closed - 1
    lookback = max(2, int(flt.pullback_leg_lookback_bars))
    window_start = max(0, t - lookback + 1)
    end_start = max(window_start, t - LEG_END_WINDOW_BARS)
    empty = {
        "bars": 0, "atr_mult": 0.0, "leg_start_idx": -1, "leg_end_idx": -1,
        "leg_start_price": 0.0, "leg_end_price": 0.0, "atr": 0.0,
    }

    atrs = atr_series(klines, 14)
    if atrs is None or len(atrs) != n_closed:
        return False, {**empty, "reason": "atr_unavailable"}

    highs = [float(k[2]) for k in klines[:n_closed]]
    lows = [float(k[3]) for k in klines[:n_closed]]

    if end_start >= t:
        return False, {**empty, "reason": "no_leg_start"}

    if direction == 1:
        end_idx = min(range(end_start, t), key=lambda i: lows[i])
        if end_idx <= window_start:
            return False, {**empty, "reason": "no_leg_start"}
        start_idx = max(range(window_start, end_idx), key=lambda i: highs[i])
        bars = end_idx - start_idx
        leg_start_price = highs[start_idx]
        leg_end_price = lows[end_idx]
        amplitude = leg_start_price - leg_end_price
    else:
        end_idx = max(range(end_start, t), key=lambda i: highs[i])
        if end_idx <= window_start:
            return False, {**empty, "reason": "no_leg_start"}
        start_idx = min(range(window_start, end_idx), key=lambda i: lows[i])
        bars = end_idx - start_idx
        leg_start_price = lows[start_idx]
        leg_end_price = highs[end_idx]
        amplitude = leg_end_price - leg_start_price

    atr = float(atrs[end_idx])
    if not np.isfinite(atr) or atr <= 0:
        return False, {**empty, "reason": "atr_unavailable"}

    # 非单调/陈旧腿校验（2026-10-22-2）：腿区间内部（不含起点与终点）不得出现比
    # 终点更深的反向极值，否则终点不是这段腿的真实极值（宽幅震荡里的半截腿）。
    if direction == 1:
        interior_low = min(lows[start_idx + 1:end_idx], default=leg_end_price)
        overshoot_atr = (leg_end_price - interior_low) / atr
    else:
        interior_high = max(highs[start_idx + 1:end_idx], default=leg_end_price)
        overshoot_atr = (interior_high - leg_end_price) / atr

    atr_mult = amplitude / atr
    evidence = {
        "bars": bars, "atr_mult": atr_mult,
        "leg_start_idx": start_idx, "leg_end_idx": end_idx,
        "leg_start_price": leg_start_price, "leg_end_price": leg_end_price,
        "atr": atr, "overshoot_atr": overshoot_atr,
    }
    if overshoot_atr > LEG_OVERSHOOT_ATR:
        # 2026-10-22-3（MAGMA 个案）：腿内更深极值若发生在更早，说明默认起点
        # 之前还夹着上一段行情；以该极值为分界重锚最近一段腿，重锚后满足
        # 单边+门槛才放行，否则维持非单调拒绝。
        anchor = _reanchor_stale_leg(highs, lows, start_idx, end_idx, direction, atr)
        if anchor is not None:
            ns, nb, nsp, namp, nover = anchor
            if (
                nover <= LEG_OVERSHOOT_ATR
                and nb >= max(0, int(flt.pullback_leg_min_bars))
                and namp / atr >= max(0.0, float(flt.pullback_leg_min_atr))
            ):
                return True, {
                    "bars": nb, "atr_mult": namp / atr,
                    "leg_start_idx": ns, "leg_end_idx": end_idx,
                    "leg_start_price": nsp, "leg_end_price": leg_end_price,
                    "atr": atr, "overshoot_atr": nover,
                }
        return False, {**evidence, "reason": "non_monotonic"}
    ok = (
        bars >= max(0, int(flt.pullback_leg_min_bars))
        and atr_mult >= max(0.0, float(flt.pullback_leg_min_atr))
    )
    return ok, evidence


def _leg_reject_detail(leg_ev: dict, direction: int, flt: ScanFilters) -> str:
    """腿门拒绝原因文案（行2/行5 触点侧与行3 确认侧共用；leg_word 随方向化）。"""
    leg_word = "下跌腿" if direction == 1 else "反弹腿"
    if leg_ev.get("reason") == "atr_unavailable":
        return f"无有效{leg_word}：ATR 不可用，无法验证"
    if leg_ev.get("reason") == "non_monotonic":
        extreme_word = "更低低点" if direction == 1 else "更高高点"
        return (
            f"无有效{leg_word}：腿内出现{extreme_word}"
            f"（超出 {leg_ev.get('overshoot_atr', 0.0):.2f}ATR > 容差 "
            f"{LEG_OVERSHOOT_ATR:.2f}ATR，非单边腿）"
        )
    return (
        f"无有效{leg_word}：{leg_ev.get('bars', 0)}根/"
        f"{leg_ev.get('atr_mult', 0.0):.2f}ATR < 门槛 "
        f"{flt.pullback_leg_min_bars}根/"
        f"{flt.pullback_leg_min_atr:.2f}ATR"
    )


def _detect_ema_touch(
    klines: list[list], direction: int, period: int, flt: ScanFilters,
    htf_dir: Optional[str] = None,
) -> Optional[float]:
    """EMA 触碰检测（docs/22 §3.3）。

    多头=下影触及且收盘收回线上（low ≤ EMA ≤ close），空头对称
    （close ≤ EMA ≤ high）。命中返回触碰时的 EMA 值，未命中 None。

    ema21：原口径（docs/19），不加任何门，行为完全不变；
    ema55：三道门防"把震荡当回踩"——
    ① 数据门：已收盘根数 < max(ema_touch_min_bars, 周期×1.7) 静默跳过
      （EMA 收敛需要窗口）
    ② 排列门：多头要求快线在慢线上方（EMA21>EMA55），空头对称
       ——慢线之下还有快线托着才是趋势回踩
    ③ 斜率门：被触碰 EMA 近 5 根同向（走平的慢线是震荡盘整）
    ema144（2026-10-16 用户拍板，深回撤，默认启用）：
    ① 数据门同上（周期×1.7≈245 根，扫描链 limit=max(kline_window,300) 已满足）；
    ② 走平/顺势门：1h EMA144 近 SLOPE_BARS 根变化 ≥0（走平或向上）当 4h 方向为多，
    做空镜像——|变化|/EMA < SLOPE_FLAT_PCT 的容差带内一律视为走平放行；不叠加
    排列门（EMA55>EMA144 在深回撤时先杀）；
    ③ 4h 上下文门：4h 方向由 scanner 的 4h 合并入口 htf_context.direction 经
    htf_dir 传入，None（4h 数据不可用/gate 降级）时 fail-closed 不出该触点
    （防用 1h 方向冒充 4h 方向）

    A3 深回撤语义（2026-10-17 docs/23）：本函数只负责「几何命中」，深回撤
    （ema55/ema144）能否真正产出信号还取决于 scanner 上游的方向票关（③）与方向
    冲突关（⑤）——价格深跌到中期/长期均线通常已打翻 1h 方向票，使定向与 4h 多头
    闸门相反而被 ⑤ 挡下；故「慢速深回撤」实际几乎不产生信号，仅「插针式触及」
    （单根影线插到均线、收盘仍强、1h 票未转向）可穿过。此处命中率低≠缺陷。
    """
    closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
    if period != 21 and len(closes) < max(flt.ema_touch_min_bars, int(period * 1.7)):
        return None
    e = calc_ema(closes, period)
    if e is None:
        return None
    ema = float(e[-1])
    if ema <= 0 or not np.isfinite(ema):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and not (low_last <= ema <= close_last):
        return None
    if direction == -1 and not (close_last <= ema <= high_last):
        return None
    if period == 21:
        return ema

    if period == 144:
        # 深回撤 144 门（2026-10-16 用户拍板）：4h 趋势明确时，1h EMA144 必须走平
        # 或顺 4h 方向（近 SLOPE_BARS=20 根变化 ≥0，含走平；|变化|/EMA 落在
        # SLOPE_FLAT_PCT 容差带内一律视为走平），做空镜像。4h 方向由 scanner
        # 的 htf_context.direction 传入；htf_dir 为 None
        # 表示 4h 数据不可用/gate 降级——「4h 多时」前提不成立，fail-closed 不出该
        # 触点（2026-10-16 加固：不再用 1h 方向冒充 4h 方向）。不叠加排列门
        # （EMA55>EMA144 在深回撤时先杀，正是 2026-10-10 退役主因）与 5 根严格
        # 斜率门（走平不再被拒）。
        if htf_dir is None:
            return None
        if len(e) < SLOPE_BARS + 1:
            return None
        slope144 = float(e[-1]) - float(e[-SLOPE_BARS])
        if not np.isfinite(slope144):
            return None
        # 走平容差带（2026-10-16 拍板）：|SLOPE_BARS 根变化|/EMA144 < SLOPE_FLAT_PCT
        # 视为走平 → 多空两侧均放行（真走平时斜率在 ±数值噪声间抖动，无带宽会把一侧
        # 随机误判为反向）；超出带宽才按方向严格判据否决。常量与 4h 闸门/1h 结构门同源
        if abs(slope144) / ema >= SLOPE_FLAT_PCT:
            if htf_dir == "long" and slope144 < 0:
                return None
            if htf_dir == "short" and slope144 > 0:
                return None
        return ema

    # 排列门（ema55）：快线（EMA21）须在慢线上方——慢线之下还有快线托着才是趋势回踩
    e_fast = calc_ema(closes, 21)
    if e_fast is not None and np.isfinite(float(e_fast[-1])) and float(e_fast[-1]) > 0:
        fast = float(e_fast[-1])
        if direction == 1 and not fast > ema:
            return None
        if direction == -1 and not fast < ema:
            return None

    # 斜率门：近 5 根同向
    if direction == 1 and not float(e[-1]) > float(e[-6]):
        return None
    if direction == -1 and not float(e[-1]) < float(e[-6]):
        return None
    return ema


def _detect_htf_ema21_touch(
    klines: list[list], htf_klines: list[list], direction: int,
) -> Optional[float]:
    """4h EMA21 跨周期触碰检测（2026-10-09）。

    语义：1h 信号K线（klines[-2]）影线/收盘触及 4h EMA21 水平——大周期动态
    价值区回踩，是 1h EMA21 的跨周期放大版。触碰带语义与 _detect_ema_touch
    完全一致（多头 low ≤ EMA ≤ close，空头对称），不带 band（同 ema21 原口径）。

    不加排列/斜率门：4h 震荡（ranging）与方向冲突已被扫描端闸门（震荡过滤 +
    方向一致门）前置过滤，此处只做触碰判定。4h EMA 取最新已收盘根
    （htf_klines[-1] 为未收盘根，与 analyze_htf_background 同口径剔除）；
    尾根差 ≤4h 对 21 周期 EMA 的影响可忽略，且同一 4h 根内的 1h K线共享
    同一 4h EMA21 快照，口径自洽。
    """
    if len(htf_klines) < 22:
        return None
    closes = np.array([float(k[4]) for k in htf_klines[:-1]], dtype=float)
    e = calc_ema(closes, 21)
    if e is None:
        return None
    ema = float(e[-1])
    if ema <= 0 or not np.isfinite(ema):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and low_last <= ema <= close_last:
        return ema
    if direction == -1 and close_last <= ema <= high_last:
        return ema
    return None


def _structural_retest_level(
    klines: list[list], direction: int, order: int, window_bars: int,
) -> Optional[float]:
    """结构推导突破回测位（需求①，2026-10-16）。

    多头：在 recent_swings(klines[:-1], order, n=RETEST_STRUCT_LOOKBACK_N)["highs"]
    （新→旧）中取最近一个 label=="HH" 且 bars_ago ≤ window_bars 的摆动高点 A
    （= 新 HH），再取比 A 更旧、label 为 "HH" 或序列末位 "H" 且 price < A.price 的
    最近一个 B（= 被 A 越过的前一个 HH）→ 回测位 = B["price"]。
    空头镜像：lows 中 label=="LL" 的 A（新 LL）+ 更旧的 "LL"/末位 "L" 且
    price > A.price 的 B。

    保证项：
    - 摆动取材用 klines[:-1]（与突破路径同口径）——信号K线因右侧根数不足不会成为
      pivot，避免「破位者自己杀掉邻近 pivot」。
    - A/B 均为收盘价口径的摆动极值，A 标签为 HH/LL 已蕴含「高于/低于前一摆动极值」，
      再叠加 price 比较即等价于「已收盘越位」。
    - 两个 HH 之间若夹着 LH（新 HH 相对前一摆动高点更高、但并未高于更旧的那个 HH）时，
      price 比较会把它排除，继续向更旧找，不会误判为「已突破」。
    - B 接受序列末位 "H"/"L"（2026-10-16 边界放宽）：该点无前参照、标签不足以否定其
      「前高/前低」身份，仅因回看窗内摆动点太少而拿不到「更高/更低」对照；此前要求
      B 必须严格为 HH/LL，导致「整个回看窗只有 2 个摆动高点（老=H、新=HH）」时推导
      不出 B。放宽只影响 B，A 仍须是 HH/LL（末位点无前参照，谈不上「破前高」）。

    数据不足 / 无满足条件的 A 或 B → None。
    """
    if window_bars <= 0:
        return None
    sw = recent_swings(klines[:-1], order=order, n=RETEST_STRUCT_LOOKBACK_N)
    seq = sw["highs"] if direction == 1 else sw["lows"]
    want = "HH" if direction == 1 else "LL"
    base = "H" if direction == 1 else "L"
    b_labels = (want, base)
    a_price: Optional[float] = None
    for p in seq:
        label = p["label"]
        price = float(p["price"])
        if a_price is None:
            # A = 最近且未过时效的新 HH / LL（末位 H/L 不算「新」）
            if label == want and p["bars_ago"] <= window_bars:
                a_price = price
            continue
        if label not in b_labels:
            continue
        lower_than_a = price < a_price if direction == 1 else price > a_price
        if lower_than_a:
            return price             # B = 被 A 越过的前一个 HH / LL（或末位 H / L）
    return None


def _detect_retest(
    klines: list[list], direction: int, retest: Optional[dict],
    window_bars: int, band_pct: float, order: int = 3,
) -> Optional[float]:
    """突破回测位触碰（docs/22 §3.1；2026-10-16 双来源）。

    来源①事件派生（优先）：retest = scanner 侧活跃破轨事件快照
    {price, direction, candle_open}（naive UTC，与 klines 时间戳同源换算）。
    来源②结构推导（需求①）：`_structural_retest_level`——新 HH 收盘越位后，被它
    越过的前一个 HH 即回测位（空头镜像 LL）；事件缺失 / 方向不符 / 窗口过期 /
    未触碰时回落到本来源，故线上「突破根非长实体、事件从没登记上」的行情也能命中。

    判定：
    - 方向一致（反向破轨事件对当前候选无效）
    - 窗口：`window_bars <= 0` 关闭该触点；事件K线开盘须落在最近 window_bars 根
      已收盘K线内（越久回测语义越弱），结构推导则要求新 HH 的 bars_ago ≤ window_bars
    - 带宽触碰：影线可下插入带（l ≤ lvl×(1+band)），收盘须守住位上（c ≥ lvl），
      空头对称——收盘破位 = 突破失败，不构成回测
    命中返回位价，未命中 None。
    """
    if window_bars <= 0:
        return None
    band = max(band_pct, 0.0) / 100.0
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])

    def _touch(lvl: float) -> bool:
        if lvl <= 0 or not np.isfinite(lvl):
            return False
        if direction == 1:
            return low_last <= lvl * (1 + band) and close_last >= lvl
        return high_last >= lvl * (1 - band) and close_last <= lvl

    # 来源①：破轨事件（T1 突破事实源）
    if retest and retest.get("direction") == direction:
        ev_lvl = float(retest.get("price") or 0)
        ev_open = retest.get("candle_open")
        if isinstance(ev_open, datetime):
            try:
                oldest = datetime.utcfromtimestamp(float(klines[-2 - int(window_bars)][0]) / 1000)
                anchor = datetime.utcfromtimestamp(float(klines[-2][0]) / 1000)
            except (IndexError, TypeError, ValueError, OSError):
                ev_lvl = 0.0      # 换算失败 → 该来源作废，继续结构推导
            else:
                if not (oldest <= ev_open <= anchor):
                    ev_lvl = 0.0  # 事件过期 → 该来源作废，继续结构推导
        if ev_lvl and _touch(ev_lvl):
            return ev_lvl

    # 来源②：结构推导（需求①）——事件缺失 / 过期 / 未触碰时的补充口径
    lvl = _structural_retest_level(klines, direction, order, window_bars)
    return lvl if lvl is not None and _touch(lvl) else None


def _detect_swing_touch(
    klines: list[list], direction: int, band_pct: float, order: int,
) -> Optional[float]:
    """摆动结构位触碰（docs/22 §3.2）。

    复用 recent_swings（收盘价摆动点，两侧各 order 根确认、无前视；order 跟随
    配置 flt.swing_order，2026-10-12 起与 AI 事实包/K线标注同源）：
    多头只认 HL（更高低点，结构延续防守位），空头只认 LH——
    LL/HH 不构成回踩结构（是接刀/追高）。

    候选口径（2026-10-16 用户拍板需求②）：取最近 SWING_TOUCH_CANDIDATES=3 个
    同向结构位——最近 1 个可能因当前窗口 merge_swings 合并而消失（不稳），
    第 2 个是常规防守位，第 3 个兜底；按新→旧顺序返回最先满足「影线入带 +
    收盘守位」者的位价。时效门 bars_ago ≤ SWING_MAX_BARS_AGO。命中返回位价。
    """
    sw = recent_swings(klines, order=order, n=SWING_TOUCH_SCAN_N)
    seq = sw["lows"] if direction == 1 else sw["highs"]
    want = "HL" if direction == 1 else "LH"
    levels = [
        float(p["price"]) for p in seq
        if p["label"] == want and p["bars_ago"] <= SWING_MAX_BARS_AGO
    ][:SWING_TOUCH_CANDIDATES]
    if not levels:
        return None
    band = max(band_pct, 0.0) / 100.0
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    for lvl in levels:
        if lvl <= 0:
            continue
        if direction == 1:
            if low_last <= lvl * (1 + band) and close_last >= lvl:
                return lvl
        elif high_last >= lvl * (1 - band) and close_last <= lvl:
            return lvl
    return None


def _unbroken_pivot_level(swings: list[dict], klines: list[list], high: bool) -> Optional[float]:
    """最近一个自形成以来未被收盘价突破的摆动极值位（新→旧扫描，2026-09-28）。

    swings 为 recent_swings(klines[:-1]) 的 highs/lows（按 bars_ago 新→旧，相对
    最后一根已收盘根）；摆动点为收盘价口径、两侧各 flt.swing_order 根确认
    （2026-10-12 起跟随配置）。判定区间 =
    摆动K线后一根 ~ 信号K线前一根（klines[idx+1:-2]）：做高向任一收盘 > 该位
    即前高已破，做空向任一收盘 < 该位即前低已破（收盘恰等于该位是触碰不是破位），
    继续向更旧一个找。全部已破（强趋势连续新高）返回 None——
    无结构前高可破，不触发突破信号。"""
    n_bars = len(klines)
    for s in swings:
        idx = n_bars - 2 - int(s["bars_ago"])
        level = float(s["price"])
        if idx < 0 or idx >= n_bars - 2:
            continue
        seg = [float(k[4]) for k in klines[idx + 1:n_bars - 2]]
        broken = any(c > level for c in seg) if high else any(c < level for c in seg)
        if not broken:
            return level
    return None


def _close_op(pending: Optional[dict], direction: int, supersede_reason: str) -> Optional[dict]:
    """pending 终态处置（行1/行2 路径）：方向反转一律 expire(direction_flip)——
    死因是票崩而非被抢先，审计语义优先；同方向被更强触发（T1 突破/当根确认）
    抢先 → supersede。"""
    if not pending:
        return None
    if pending.get("direction") != direction:
        return {"op": "expire", "reason": "direction_flip"}
    return {"op": "supersede", "reason": supersede_reason}


def detect_trigger(
    klines: list[list], direction: int,
    pending: Optional[dict] = None,
    flt: Optional[ScanFilters] = None,
    retest: Optional[dict] = None,
    htf: Optional[list] = None,
    htf_dir: Optional[str] = None,
    diag: Optional[dict] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """检测与候选方向一致的入场触发（两段式决策矩阵，docs/19 §4.1 + docs/22 触点）。

    direction: 1 多 / -1 空（方向票多数决的符号）
    pending: 该币活跃 pending 快照（{id, direction, stage1_candle_open, touch_ref}）或 None
    flt（可选）：筛选参数组（2026-09-28 筛选配置上页面）；None=按 settings
    解析（env/代码默认），策略函数不读 DB。
    retest（可选）：scanner 侧活跃破轨事件快照 {price, direction, candle_open}
    （docs/22 §3.1；T1 突破触发时由 scanner 记录 breakout_events）。
    htf（可选）：4h 原始K线（尾根未收盘，与 analyze_htf_background 同源序列）；
    None/过短 → ema21_4h 触点静默跳过（4h 数据缺失不阻断 1h 触点体系）。
    htf_dir（可选）：scanner htf_context.direction 给出的 4h 方向（"long"/"short"），或 None
    （4h 拉取失败/gate 异常降级）——ema144 深回撤触点据此判 4h 趋势方向，
    None 时 fail-closed 不出该触点（不用 1h 方向冒充 4h）。
    diag（可选，2026-10-13 漏斗「无触发」判定说明）：未触发路径的可选出参——
    向 diag["reasons"] 追加原因文本段（突破卡点/触点缺失/待确认失效/无金K），
    scanner 收口落 funnel_json.details 前端展示；触发命中时内容不保证完整，
    调用方仅在无触发拒绝时读取。diag=None 全程静默（旧调用方零改动）。
    返回 (trigger, pending_op) 二元组：
    - trigger: 命中的触发 dict（type/direction/position/touch_ref/pattern/
      breakout_pct/reason）或 None（无触发 → 观察池）；touch_ref 仅回踩确认
      携带（当根确认=本次命中触点，次根确认=pending 的 Stage-1 触点）
    - pending_op: 本轮对活跃 pending 的处置，None 或
      {"op": "supersede"|"confirm"|"expire"|"upsert", ...}
      ——upsert/expire 后续在 scanner 侧结合 DB 行补全（方向反转拆两步、
      quality/votes 快照由 scanner 填充），trigger.py 保持无 DB 依赖。

    信号判定全部基于已收盘K线（klines[-2]）；pending 有效期 = 其 stage1_candle_open
    落在信号K线之前 1..N 根已收盘K线内（N=pending_confirm_window_bars），免疫扫描
    时刻漂移与轮次缺失；窗口未满且无同向金K 时保持等待（不下 pending 处置）。
    """
    # 筛选参数组（None=按 settings 解析，env/代码默认兜底）
    flt = flt or resolve_scan_filters(None)
    n = len(klines)
    if n < 30 or direction not in (1, -1):
        _diag_note(diag, "触发判定未跑：K线不足30根")
        return None, None
    close_last = float(klines[-2][4])
    # position 按方向派生（2026-09-28 关键位移除后回到 docs/09 原始口径）：
    # 看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance
    pos_fb = "support" if direction == 1 else "resistance"
    wanted = "bullish" if direction == 1 else "bearish"

    # pending 有效期判定：Stage-1 触碰根的已收盘K线须落在信号K线之前 1..N 根内
    # （N=ScanFilters.pending_confirm_window_bars；N=1 即旧「恰好下一根」口径）
    win = max(1, int(flt.pending_confirm_window_bars))
    pending_elapsed = None  # 触碰根距信号K线的已收盘根数（1..N）；None=不在窗口内
    if pending:
        anchor = pending.get("stage1_candle_open")
        for k in range(1, win + 1):
            idx = -2 - k
            if -idx > n:
                break
            try:
                if anchor == datetime.utcfromtimestamp(float(klines[idx][0]) / 1000):
                    pending_elapsed = k
                    break
            except (IndexError, TypeError, ValueError, OSError):
                break
    pending_valid = pending_elapsed is not None

    # T1 双突破分类（2026-09-29）：同一根信号K线按突破位口径互斥归类，结构优先——
    # ① T1a 结构突破：突破位 = 最近一个自形成以来未被收盘价突破的摆动结构前高
    #    （做多）/前低（做空），收盘越过它才算 N 字/趋势延续的结构性破位；
    # ② T1b 趋势突破：结构突破未命中时回落 20 根 Donchian 滚动轨口径——强趋势
    #    连续新高/新低时摆动位全破（ref=None）无结构可越，恢复 8373eb6 之前的
    #    强趋势追势入场，作为独立类型与结构突破并存（复盘分组对比胜率）。
    # 两类共享闸门：① 最小突破幅度（毛边不算）② 长实体短影线
    # （实体≥70%全长，2026-09-30-2 放宽）；放量为加分项（2026-09-29：≥量比门槛 +5 质量分，
    # 不卡死——缩量突破放行但排序自然落后）；追高上限各自按突破位衡量
    # （越位 >1.5% 视为追高，0=关闭）。
    # 突破返回携带 breakout_level（前高/前低/轨价）与 candle_open（信号K线开盘），
    # scanner 据此维护 breakout_events（回测触点=破位价回测的事实源，docs/22 §4）。
    if n >= 30:
        # 摆动结构取信号K线之前的已收盘K线（klines[:-1]）——信号K线是潜在破位者，
        # 不参与结构检测，否则其极值收盘会杀死邻近 pivot（如破位阴线压掉前低）。
        # 阶数跟随配置 flt.swing_order（2026-10-12，与 AI 事实包/K线标注同源）
        swings = recent_swings(klines[:-1], order=flt.swing_order, n=10)
        vr = volume_ratio(klines)
        max_brk = flt.breakout_max_pct
        thr = flt.breakout_threshold
        sig_open = datetime.utcfromtimestamp(float(klines[-2][0]) / 1000)
        solid = _is_solid_breakout_candle(klines, direction)
        vol_desc = f"放量{vr:.1f}倍·" if vr >= flt.breakout_vol_ratio else ""
        ref = None
        if solid:
            ref = _unbroken_pivot_level(
                swings["highs"] if direction == 1 else swings["lows"],
                klines, high=(direction == 1),
            )
        if ref is not None:
            over_max = (
                (direction == 1 and close_last > ref * (1 + max_brk / 100))
                or (direction == -1 and close_last < ref * (1 - max_brk / 100))
            ) if max_brk > 0 else False
            if not over_max:
                if direction == 1 and close_last >= ref * (1 + thr):
                    return {
                        "type": BREAKOUT, "direction": direction,
                        "position": "resistance",
                        "breakout_level": ref, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破前高 {ref:.6g}",
                        "breakout_pct": (close_last - ref) / ref * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘突破前高({ref:.6g})",
                    }, _close_op(pending, direction, "breakout")
                if direction == -1 and close_last <= ref * (1 - thr):
                    return {
                        "type": BREAKOUT, "direction": direction,
                        "position": "support",
                        "breakout_level": ref, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破前低 {ref:.6g}",
                        "breakout_pct": (close_last - ref) / ref * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘跌破前低({ref:.6g})",
                    }, _close_op(pending, direction, "breakout")
        # T1b 趋势突破（Donchian 滚动轨，窗口不含信号K线自身与未收盘根，
        # 防"自己破自己"）：结构突破未命中（无未破结构位 / 未达幅度 / 追高拦截）
        # 时的回落口径，2026-09-27 追高复盘的"跳过 T1 判定落回金K分支"改为独立类型
        chan_n = flt.breakout_channel_bars
        if solid and n >= chan_n + 2:
            window = klines[-2 - chan_n:-2]
            chan_high = max(float(k[2]) for k in window)
            chan_low = min(float(k[3]) for k in window)
            over_max = (
                (direction == 1 and close_last > chan_high * (1 + max_brk / 100))
                or (direction == -1 and close_last < chan_low * (1 - max_brk / 100))
            ) if max_brk > 0 else False
            if not over_max:
                if direction == 1 and close_last >= chan_high * (1 + thr):
                    return {
                        "type": TREND_BREAKOUT, "direction": direction,
                        "position": "resistance",
                        "breakout_level": chan_high, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破轨高 {chan_high:.6g}",
                        "breakout_pct": (close_last - chan_high) / chan_high * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘突破{chan_n}根"
                                  f"Donchian上轨({chan_high:.6g})",
                    }, _close_op(pending, direction, "breakout")
                if direction == -1 and close_last <= chan_low * (1 - thr):
                    return {
                        "type": TREND_BREAKOUT, "direction": direction,
                        "position": "support",
                        "breakout_level": chan_low, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破轨低 {chan_low:.6g}",
                        "breakout_pct": (close_last - chan_low) / chan_low * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘跌破{chan_n}根"
                                  f"Donchian下轨({chan_low:.6g})",
                    }, _close_op(pending, direction, "breakout")
        # 两类突破均未命中才会执行至此 → 采集卡点明细（2026-10-13 诊断用，
        # 不参与判定；diag=None 时整块跳过）
        if diag is not None:
            diag.setdefault("reasons", []).extend(
                _breakout_fail_notes(klines, direction, solid, ref, flt))

    # 当根金K证据统一裁决（2026-09-29 复合确认；2026-10-01 收窄：仅流星线维持
    # 复合确认（双流星/流星+趋势K），锤形线恢复单根直接确认；2026-10-01-2 倒锤
    # 形线退出看涨白名单——见 candlestick.resolve_golden_evidence；触点框架要求不变）
    patterns = detect_all_patterns(klines, idx=-2)
    golden, golden_note = resolve_golden_evidence(klines, wanted, patterns)

    # 1H 结构门（2026-10-12 硬门；2026-10-15「降级不否决」，docs/21 O9）：
    # 三线排列 + 快中线发散史 + EMA144 方向。仅当回踩触点为 1h EMA21 时评估；
    # 不过门不再否决该次回踩——照常产出信号并带 structure_gate=False 证据，
    # 由质量分重罚（technical.STRUCTURE_GATE_PENALTY=-15）体现排序差异；
    # 其余触点（retest/swing/4h EMA21/EMA55）豁免本门（structure_gate=True）。
    # 4H 全局闸门由 scanner 前置强制（htf_context 闸门 + 状态机前置门 + 方向一致性），
    # 此处不重复。
    try:
        struct = structure_1h(klines[:-1], "long" if direction == 1 else "short")
    except Exception:
        struct = None  # fail-closed：结构门计算异常按不过处理（仍不否决，仅重罚）

    # 并行探测启用触点（docs/22 §3）：命中收集为 {ref: 位价}，标注取最高优先
    # （横向结构语义强于均线；均线取最快——最早确认、止损最紧）
    touches: dict[str, float] = {}
    refs = flt.pullback_touch_refs
    if TOUCH_RETEST in refs:
        p = _detect_retest(klines, direction, retest,
                           flt.retest_window_bars, flt.structure_touch_band_pct,
                           order=flt.swing_order)
        if p is not None:
            touches[TOUCH_RETEST] = p
    if TOUCH_SWING in refs:
        p = _detect_swing_touch(
            klines, direction, flt.structure_touch_band_pct, flt.swing_order)
        if p is not None:
            touches[TOUCH_SWING] = p
    for ref, period in ((TOUCH_EMA21, 21), (TOUCH_EMA55, 55), (TOUCH_EMA144, 144)):
        if ref in refs:
            p = _detect_ema_touch(klines, direction, period, flt, htf_dir=htf_dir)
            if p is not None:
                touches[ref] = p
    if TOUCH_EMA21_4H in refs and htf:
        p = _detect_htf_ema21_touch(klines, htf, direction)
        if p is not None:
            touches[TOUCH_EMA21_4H] = p
    touch_ref = next((r for r in TOUCH_ORDER if r in touches), None)
    touch_price = touches.get(touch_ref) if touch_ref else None
    touch_label = TOUCH_LABELS.get(touch_ref, "") if touch_ref else None
    # 未触发原因：本根未命中任何启用触点（诊断采集，见 diag 参数说明）
    if diag is not None and touch_ref is None:
        enabled = "、".join(TOUCH_LABELS.get(r, r) for r in flt.pullback_touch_refs)
        _diag_note(diag, "回踩路径未过：未命中任何启用触点"
                   + (f"（{enabled}）" if enabled else ""))
    # 1H 结构门结果（2026-10-15 降级不否决）：仅 ema21 触点可能为 False，
    # 其余触点豁免（True）。False 时照常出信号，由质量分重罚体现（-15）。
    structure_gate = True
    if touch_ref == TOUCH_EMA21:
        structure_gate = struct is not None
    # 结构门未过注记：随信号 reason 落库，AI/前端/复盘可见（截断前优先给出）
    struct_note = "" if structure_gate else "·1H结构未过(重罚-15)"

    # 量能注记（docs/25 §3.1）：回踩金K路径补「大量」要素，双口径见 helper
    vol_note = _pullback_volume_note(klines)

    # 回踩前置反向腿门（2026-10-22）：Stage-1 触点前须存在一段反向腿
    # （做多=下跌腿、做空=反弹腿）；不满足则不建 pending、不当根确认。
    # 2026-10-22-2：retest 不再豁免——ARIA 个案证明「上涨后的回测位看涨吞没」
    # 同样会被误命中；突破回测不天然等价于回踩，必须同样存在前置反向腿。
    # 门不通过时把 touch_ref 置空，让流程自然落回「无当前触点」分支。
    # 行3 确认侧另以当前K线复校腿门（2026-10-22-2，ARIA 个案：pending 建立后价格
    # 已连涨远离触点，旧口径仍按 Stage-1 语境确认 → 上涨末端的金K被误判为回踩）。
    leg_rejected = False
    if touch_ref is not None and flt.pullback_leg_gate_enabled:
        leg_ok, leg_ev = _pullback_leg_gate(klines, direction, flt)
        if not leg_ok:
            _diag_note(
                diag,
                f"回踩{_touch_ref_label(touch_ref, direction)}前"
                f"{_leg_reject_detail(leg_ev, direction, flt)}",
            )
            touch_ref = None
            touch_price = None
            leg_rejected = True

    # 行2：当根触碰+金K → 当根确认（比纯形态更强，pending 一律被抢先）。
    # ema21 触点未过 1H 结构门不再否决，改带 structure_gate=False 证据（2026-10-15）
    if touch_ref is not None and golden:
        _label = PATTERN_LABEL_MAP.get(golden["pattern"], golden["pattern"])
        # 证据描述：单形态="当根X确认"；组合=note 原文+确认（如
        # "双流星组合确认"/"前根锤形线+当根趋势K确认"）
        _ev = f"当根{_label}确认" if not golden_note else f"{golden_note}确认"
        # 企稳加分项（docs/19 §3.3 条件2/3，2026-10-10 拍板：加分不拦截）：
        # 当根确认时触碰根即金K根，量能已由放量加分评估，volume_semantics=False
        _stab = pullback_stabilize_bonus(klines, direction, -2, volume_semantics=False)
        # 组合确认口径（2026-10-26，docs/11）：pattern 落在信号根之前时，把形态真实
        # 所在K线与确认滞后一起送出去；形态在当根（offset=-2）时 pattern_candle_open
        # 就是信号根，滞后无意义置 None
        _pat_off = golden.get("offset", -2)
        _pattern_open = datetime.utcfromtimestamp(float(klines[_pat_off][0]) / 1000)
        _lag = None
        if _pat_off == -3:
            _lag = round(
                direction * (float(klines[-2][4]) / float(klines[_pat_off][4]) - 1) * 100, 4)
        return {
            "type": PULLBACK_CONFIRM,
            "direction": direction,
            "position": pos_fb, "touch_ref": touch_ref,
            "position_ref": _position_ref_of(touch_ref, direction),
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "stabilize": _stab,
            # 1H 结构门结果（2026-10-15 降级不否决）：scanner 据此算质量分重罚
            "structure_gate": structure_gate,
            # 组合金K标志（2026-10-13-6）：供质量分算「金K组合价格带结构点密度」
            # 时决定价格带是否并入前根（True=组合两根包络，False=单根）
            "golden_combo": bool(golden_note),
            # 形态真实所在K线开盘时间 + 确认滞后（docs/11，仅 offset=-3 非空）
            "pattern_candle_open": _pattern_open, "entry_lag_pct": _lag,
            "reason": f"指标同向·回踩{touch_label}({touch_price:.6g})企稳且{_ev}"
                      f"{vol_note}{struct_note}",
        }, _close_op(pending, direction, "same_candle_confirm")

    # 行3/行4：未触碰但有金K → 有效 pending 则次根确认（有回踩语境）。
    # 纯金K独立触发 2026-09-29 下线：正式网复盘纯金K 28 笔 6 胜（-52.15 USDT），
    # 无回踩语境的形态追入负期望——金K必须与触点配合（当根/次根确认），
    # 无有效 pending 的金K 一律不触发。悬空金K门槛（candle_max_ema21_dist_pct，
    # 2026-09-27）随之整体失效，配置键保留向后兼容；活跃 pending 照常终态处置。
    if touch_ref is None and golden:
        if pending_valid and pending and pending.get("direction") == direction:
            # 行3 确认侧腿门复校（2026-10-22-2）：以确认根之前的K线重新度量反向腿——
            # Stage-1 建池时过门不代表确认时语境仍有效（ARIA：pending 建立后价格
            # 连涨 4 根远离触点，上涨末端的金K按旧口径仍被当作回踩确认）。
            # 不通过则本根不确认、pending 保持等待（确认窗口耗尽后自然过期）。
            if flt.pullback_leg_gate_enabled:
                leg_ok, leg_ev = _pullback_leg_gate(klines, direction, flt)
                if not leg_ok:
                    _diag_note(
                        diag,
                        "待确认未确认：确认根前"
                        + _leg_reject_detail(leg_ev, direction, flt),
                    )
                    return None, None
            p_ref = pending.get("touch_ref")
            # ema21 触点同样受 1H 结构门约束，但 2026-10-15 起复校只重罚不失效
            # （降级不否决，docs/21 O9）：结构不过照常确认，带 structure_gate=False
            p_structure_gate = True
            if p_ref == TOUCH_EMA21:
                p_structure_gate = struct is not None
            p_struct_note = "" if p_structure_gate else "·1H结构未过(重罚-15)"
            p_label = TOUCH_LABELS.get(p_ref, "触点") if p_ref else "触点"
            _label = PATTERN_LABEL_MAP.get(golden["pattern"], golden["pattern"])
            _ev = f"本根{_label}确认" if not golden_note else f"{golden_note}确认"
            # 企稳加分项：触碰根 = 金K 之前 pending_elapsed 根（klines[-2-elapsed]，
            # 即 pending 的 Stage-1 根），量能语义照常评估
            _stab = pullback_stabilize_bonus(klines, direction, -2 - pending_elapsed)
            # 组合确认口径（2026-10-26，docs/11）：pattern 落在信号根之前时，把形态真实
            # 所在K线与确认滞后一起送出去；形态在当根（offset=-2）时 pattern_candle_open
            # 就是信号根，滞后无意义置 None
            _pat_off = golden.get("offset", -2)
            _pattern_open = datetime.utcfromtimestamp(float(klines[_pat_off][0]) / 1000)
            _lag = None
            if _pat_off == -3:
                _lag = round(
                    direction * (float(klines[-2][4]) / float(klines[_pat_off][4]) - 1) * 100, 4)
            return {
                "type": PULLBACK_CONFIRM,
                "direction": direction,
                "position": pos_fb, "touch_ref": p_ref,
                "position_ref": _position_ref_of(p_ref, direction),
                "pattern": golden["pattern"], "breakout_pct": 0.0,
                "stabilize": _stab,
                # 1H 结构门结果（2026-10-15 降级不否决）：scanner 据此算质量分重罚
                "structure_gate": p_structure_gate,
                # 组合金K标志（2026-10-13-6）：质量分算金K组合价格带结构点密度用
                "golden_combo": bool(golden_note),
                # 形态真实所在K线开盘时间 + 确认滞后（docs/11，仅 offset=-3 非空）
                "pattern_candle_open": _pattern_open, "entry_lag_pct": _lag,
                "reason": f"指标同向·回踩{p_label}企稳·{_ev}{vol_note}{p_struct_note}",
            }, {"op": "confirm"}
        if pending:
            flip = pending.get("direction") != direction
            _diag_note(diag, _expire_note(pending, direction, flip))
            return None, {"op": "expire",
                          "reason": "direction_flip" if flip else "window_expired"}
        if not leg_rejected:
            _diag_note(diag, "金K未触发：本根出现同向金K但无回踩触点/待确认语境"
                       "（纯金K不触发）")
        return None, None

    # 行5：触碰但无金K → 不触发，转 pending 待确认（scanner 侧 upsert）。
    # ema21 键存 Stage-1 触碰锚点价（命中触点的位价），touch_ref 记触点类型。
    # 1H 结构门未过（structure_gate=False）不再挡进池，随 op 透传给 scanner 记重罚
    if touch_ref is not None:
        return None, {
            "op": "upsert", "direction": direction, "touch_ref": touch_ref,
            "stage1_candle_open": datetime.utcfromtimestamp(float(klines[-2][0]) / 1000),
            "ema21": touch_price,
            "structure_gate": structure_gate,
        }

    # 行6/行7：未触碰无金K → 窗口未满保持等待；窗口耗尽/无效 pending 才终态
    # （错过即过期）。1H 结构门 2026-10-15 起不再作为死因（降级为质量分重罚）
    if pending:
        if pending.get("direction") != direction:
            _diag_note(diag, _expire_note(pending, direction, True))
            return None, {"op": "expire", "reason": "direction_flip"}
        if pending_valid and pending_elapsed < win:
            # 窗口未满：保持等待，本轮不对 pending 下任何处置（待确认池继续显示）
            _diag_note(diag, f"待确认等待中：本根无同向金K，确认窗口还剩 {win - pending_elapsed} 根")
            return None, None
        _diag_note(diag, _expire_note(pending, direction, False))
        return None, {"op": "expire", "reason": "window_expired"}
    _diag_note(diag, "本根无同向金K")
    return None, None
