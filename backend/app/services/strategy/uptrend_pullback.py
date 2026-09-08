"""策略三：上涨趋势回调

逻辑：
1. 识别上涨趋势：连续 HH（更高高点）+ HL（更高低点）
2. 用最近的摆动低点拟合上升趋势线（斜率 > 0）
3. 价格回调到趋势线附近但未跌破，或回调到近期 HL 附近
4. 重点关注：HL 序列保持（回调不破前低），回调幅度可控
"""
from __future__ import annotations

import numpy as np
from typing import Optional

from app.services.strategy.swing import (
    find_swing_points,
    merge_swings,
    classify_structure,
    linear_regression,
)
from app.services.strategy.types import UPTREND_PULLBACK


def detect(klines: list[list], config: dict) -> Optional[dict]:
    highs = np.array([float(k[2]) for k in klines], dtype=float)
    lows = np.array([float(k[3]) for k in klines], dtype=float)
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(highs)

    if n < config.get("min_klines", 30):
        return None

    order = config.get("swing_order", 3)
    high_idx, low_idx = find_swing_points(highs, lows, order)
    swings = merge_swings(high_idx, low_idx, highs, lows)

    if len(swings) < 4:
        return None

    struct = classify_structure(swings)
    highs_seq = struct["highs_seq"]
    lows_seq = struct["lows_seq"]

    if len(highs_seq) < 3 or len(lows_seq) < 3:
        return None

    close_last = float(closes[-1])

    # 条件1：近期为上涨趋势（HH + HL）
    recent_highs = highs_seq[-3:]
    is_hh_sequence = all(recent_highs[i][2] < recent_highs[i + 1][2] for i in range(len(recent_highs) - 1))

    recent_lows = lows_seq[-3:]
    is_hl_sequence = all(recent_lows[i][2] < recent_lows[i + 1][2] for i in range(len(recent_lows) - 1))

    if not (is_hh_sequence or is_hl_sequence):
        return None

    # 条件2：用最近的摆动低点拟合上升趋势线
    x = np.array([p[0] for p in recent_lows], dtype=float)
    y = np.array([p[2] for p in recent_lows], dtype=float)
    slope, intercept, r2 = linear_regression(x, y)

    if not np.isfinite(slope) or slope <= 0:
        return None

    r2_threshold = config.get("r_squared_threshold", 0.5)
    if r2 < r2_threshold:
        return None

    # 趋势线在当前位置的预测值
    trend_value = slope * (n - 1) + intercept
    if trend_value <= 0:
        return None

    # 条件3：回调判定
    # 方式A：价格回调到趋势线附近（距离趋势线一定范围内）
    pullback_tolerance = config.get("pullback_tolerance", 0.03)  # 3% 容差
    distance_to_trend = (close_last - trend_value) / trend_value

    near_uptrend_line = 0 <= distance_to_trend <= pullback_tolerance

    # 方式B：价格回调到最近摆动低点（HL）附近但未跌破
    last_low = recent_lows[-1][2]
    distance_to_hl = (close_last - last_low) / last_low
    near_hl = 0 <= distance_to_hl <= pullback_tolerance

    # 方式C：从近期高点回调幅度合理（3%~15%）
    recent_high = recent_highs[-1][2]
    pullback_depth = (recent_high - close_last) / recent_high
    reasonable_pullback = 0.02 <= pullback_depth <= 0.15

    if not (near_uptrend_line or near_hl or reasonable_pullback):
        return None

    # 回调不能跌破趋势线太多
    if distance_to_trend < -pullback_tolerance:
        return None

    breakout_pct = float(distance_to_trend * 100)

    strength = 0.0
    if near_uptrend_line and reasonable_pullback:
        strength = 1.0
    elif near_uptrend_line or near_hl:
        strength = 0.7
    else:
        strength = 0.5

    return {
        "signal_type": UPTREND_PULLBACK,
        "current_price": close_last,
        "breakout_pct": breakout_pct,
        "trend_slope": float(slope),
        "r_squared": float(r2),
        "pullback_depth": float(pullback_depth * 100),
        "distance_to_trend": float(distance_to_trend * 100),
        "strength": strength,
    }
