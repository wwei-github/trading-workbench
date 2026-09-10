"""市场结构分类模块

基于摆动点序列（收盘价计算）判定当前市场结构：
- uptrend 上涨趋势: 高点递增(HH) + 低点递增(HL)
- downtrend 下跌趋势: 高点递减(LH) + 低点递减(LL)
- trend_reversal 趋势反转: 趋势结构中收盘价破前高/前低（123法则第②步信号）
- range_bound 震荡区间: 水平/收敛结构，或高低点方向冲突（HH+LL / LH+HL）
- unknown 未分类: 摆动点不足

分类只做"打标签"，不构成过滤门槛。
"""
from __future__ import annotations

import numpy as np

from app.services.strategy.swing import linear_regression
from app.services.strategy.types import (
    DOWNTREND,
    RANGE_BOUND,
    TREND_REVERSAL,
    UPTREND,
    UNKNOWN,
)


def classify_structure(
    swings: list[tuple],
    closes: np.ndarray,
    n_closed: int,
    config: dict,
) -> dict:
    """判定市场结构。

    swings: merge_swings 输出的摆动点 [(idx, "H"|"L", price), ...]
    closes: 收盘价数组（含未收盘最后一根）
    n_closed: 已收盘 K 线数量（n - 1）
    返回: {signal_type, reversal, highs_seq, lows_seq}
        reversal: None | "up"（下跌转涨） | "down"（上涨转跌）
    """
    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]
    base = {"highs_seq": highs_seq, "lows_seq": lows_seq, "reversal": None}

    if len(highs_seq) < 2 or len(lows_seq) < 2:
        return {**base, "signal_type": UNKNOWN}

    close_last = float(closes[-2])
    recent_highs = highs_seq[-3:]
    recent_lows = lows_seq[-3:]

    hh = all(
        recent_highs[i][2] < recent_highs[i + 1][2]
        for i in range(len(recent_highs) - 1)
    )
    lh = all(
        recent_highs[i][2] > recent_highs[i + 1][2]
        for i in range(len(recent_highs) - 1)
    )
    hl = all(
        recent_lows[i][2] < recent_lows[i + 1][2]
        for i in range(len(recent_lows) - 1)
    )
    ll = all(
        recent_lows[i][2] > recent_lows[i + 1][2]
        for i in range(len(recent_lows) - 1)
    )

    prev_high = highs_seq[-1][2]
    prev_low = lows_seq[-1][2]

    # 反转优先：趋势结构 + 收盘价破前高/前低（123法则第②步）
    if (lh or ll) and close_last > prev_high:
        return {**base, "signal_type": TREND_REVERSAL, "reversal": "up"}
    if (hh or hl) and close_last < prev_low:
        return {**base, "signal_type": TREND_REVERSAL, "reversal": "down"}

    # 水平结构 → 震荡（高点低点回归斜率相对价格区间都足够小）
    max_slope = config.get("max_trend_slope", 0.005)
    price_range = float(closes.max() - closes.min())
    if price_range > 0 and len(recent_highs) >= 2 and len(recent_lows) >= 2:
        xh = np.array([p[0] for p in recent_highs], dtype=float)
        yh = np.array([p[2] for p in recent_highs], dtype=float)
        xl = np.array([p[0] for p in recent_lows], dtype=float)
        yl = np.array([p[2] for p in recent_lows], dtype=float)
        slope_h, _, _ = linear_regression(xh, yh)
        slope_l, _, _ = linear_regression(xl, yl)
        if np.isfinite(slope_h) and np.isfinite(slope_l):
            n = len(closes)
            rel_h = abs(slope_h) / price_range * n
            rel_l = abs(slope_l) / price_range * n
            if rel_h < max_slope and rel_l < max_slope:
                return {**base, "signal_type": RANGE_BOUND}

    # 方向一致的趋势
    if hh and hl:
        return {**base, "signal_type": UPTREND}
    if lh and ll:
        return {**base, "signal_type": DOWNTREND}

    # 高低点方向冲突（HH+LL 发散 / LH+HL 收敛）→ 震荡
    return {**base, "signal_type": RANGE_BOUND}
