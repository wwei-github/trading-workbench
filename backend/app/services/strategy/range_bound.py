"""策略二：区间震荡

逻辑：
1. 识别震荡结构：HH/HL 与 LH/LL 交替出现，价格在上下边界之间
2. 上下边界斜率接近水平（趋势线斜率绝对值小）
3. 当前价格接近区间下沿（潜在做多机会）或突破上沿
4. 重点关注：更低的高点 + 更高的低点（收敛三角形）或水平震荡
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
from app.services.strategy.types import RANGE_BOUND


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

    if len(swings) < 5:
        return None

    struct = classify_structure(swings)
    highs_seq = struct["highs_seq"]
    lows_seq = struct["lows_seq"]

    if len(highs_seq) < 3 or len(lows_seq) < 3:
        return None

    close_last = float(closes[-1])

    # 取最近若干摆动高/低点
    recent_n = min(5, len(highs_seq))
    recent_highs = highs_seq[-recent_n:]
    recent_lows = lows_seq[-recent_n:]

    # 上边界：拟合摆动高点
    xh = np.array([p[0] for p in recent_highs], dtype=float)
    yh = np.array([p[2] for p in recent_highs], dtype=float)
    slope_h, intercept_h, r2_h = linear_regression(xh, yh)

    # 下边界：拟合摆动低点
    xl = np.array([p[0] for p in recent_lows], dtype=float)
    yl = np.array([p[2] for p in recent_lows], dtype=float)
    slope_l, intercept_l, r2_l = linear_regression(xl, yl)

    if not (np.isfinite(slope_h) and np.isfinite(slope_l)):
        return None

    # 条件1：上下边界斜率接近水平（震荡），或形成收敛三角形
    # 水平震荡：两斜率绝对值都很小
    # 收敛三角形：上边界下行 + 下边界上行
    max_slope = config.get("max_trend_slope", 0.005)  # 相对斜率阈值
    price_range = float(highs.max() - lows.min())
    if price_range <= 0:
        return None

    rel_slope_h = abs(slope_h) / price_range * n
    rel_slope_l = abs(slope_l) / price_range * n

    is_horizontal = rel_slope_h < max_slope and rel_slope_l < max_slope
    is_converging = slope_h < 0 and slope_l > 0  # 收敛三角形

    if not (is_horizontal or is_converging):
        return None

    # 条件2：计算当前上下边界
    upper_now = slope_h * (n - 1) + intercept_h
    lower_now = slope_l * (n - 1) + intercept_l
    if upper_now <= lower_now or lower_now <= 0:
        return None

    band_width = upper_now - lower_now
    if band_width / lower_now < 0.005:  # 区间过窄
        return None

    # 条件3：当前价格在区间内，且接近下沿（潜在买入）或突破上沿
    position = (close_last - lower_now) / band_width  # 0=下沿, 1=上沿

    breakout_threshold = config.get("breakout_threshold", 0.005)
    near_lower = position <= 0.3  # 接近下沿
    broke_upper = (close_last - upper_now) / upper_now >= breakout_threshold  # 突破上沿

    if not (near_lower or broke_upper):
        return None

    # 条件4：波动率收敛（近期振幅小于前期）
    mid = n // 2
    early_range = float(highs[:mid].max() - lows[:mid].min())
    late_range = float(highs[mid:].max() - lows[mid:].min())
    if early_range <= 0:
        return None
    volatility_ratio = late_range / early_range

    strength = 0.0
    if broke_upper and is_horizontal:
        strength = 1.0
    elif near_lower and volatility_ratio < 1.0:
        strength = 0.8
    elif near_lower:
        strength = 0.5
    else:
        strength = 0.6

    return {
        "signal_type": RANGE_BOUND,
        "current_price": close_last,
        "breakout_pct": float(((close_last - lower_now) / lower_now) * 100),
        "upper_band": float(upper_now),
        "lower_band": float(lower_now),
        "band_position": float(position),
        "volatility_ratio": float(volatility_ratio),
        "is_converging": is_converging,
        "strength": strength,
    }
