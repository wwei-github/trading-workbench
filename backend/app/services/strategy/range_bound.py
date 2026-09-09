"""策略二：区间震荡

优化逻辑：
1. 用摆动点识别震荡结构（水平/收敛），历史趋势仍需 order 确认
2. 只在最后一根已收盘 K 线上检测形态
3. 必须在区间顶部或底部出现形态才命中：
   A. 接近下沿 + 看涨形态（锤形线、吞没等）
   B. 接近上沿 + 看涨形态（突破上沿的吞没/阳线等）
"""
from __future__ import annotations

import numpy as np
from typing import Optional

from app.services.strategy.swing import (
    find_swing_points,
    merge_swings,
    linear_regression,
)
from app.services.strategy.candlestick import detect_all_patterns
from app.services.strategy.types import RANGE_BOUND


def detect(klines: list[list], config: dict) -> Optional[dict]:
    highs = np.array([float(k[2]) for k in klines], dtype=float)
    lows = np.array([float(k[3]) for k in klines], dtype=float)
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(highs)

    if n < config.get("min_klines", 30):
        return None

    # 摆动点用收盘价计算，过滤影线毛刺
    order = config.get("swing_order", 3)
    high_idx, low_idx = find_swing_points(closes, closes, order)
    swings = merge_swings(high_idx, low_idx, closes, closes)

    if len(swings) < 5:
        return None

    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]

    if len(highs_seq) < 3 or len(lows_seq) < 3:
        return None

    # 用最后一根已收盘 K 线（klines[-2]）
    close_last = float(closes[-2])
    n_closed = n - 1

    # 取最近若干摆动高/低点拟合边界
    recent_n = min(5, len(highs_seq))
    recent_highs = highs_seq[-recent_n:]
    recent_lows = lows_seq[-recent_n:]

    xh = np.array([p[0] for p in recent_highs], dtype=float)
    yh = np.array([p[2] for p in recent_highs], dtype=float)
    slope_h, intercept_h, r2_h = linear_regression(xh, yh)

    xl = np.array([p[0] for p in recent_lows], dtype=float)
    yl = np.array([p[2] for p in recent_lows], dtype=float)
    slope_l, intercept_l, r2_l = linear_regression(xl, yl)

    if not (np.isfinite(slope_h) and np.isfinite(slope_l)):
        return None

    # 判断震荡结构
    price_range = float(highs.max() - lows.min())
    if price_range <= 0:
        return None

    rel_slope_h = abs(slope_h) / price_range * n
    rel_slope_l = abs(slope_l) / price_range * n

    max_slope = config.get("max_trend_slope", 0.005)
    is_horizontal = rel_slope_h < max_slope and rel_slope_l < max_slope
    is_converging = slope_h < 0 and slope_l > 0

    if not (is_horizontal or is_converging):
        return None

    # 计算当前上下边界
    upper_now = slope_h * n_closed + intercept_h
    lower_now = slope_l * n_closed + intercept_l
    if upper_now <= lower_now or lower_now <= 0:
        return None

    band_width = upper_now - lower_now
    if band_width / lower_now < 0.005:
        return None

    position = (close_last - lower_now) / band_width  # 0=下沿, 1=上沿

    # === 位置判定 ===
    near_lower = position <= 0.3   # 接近下沿
    near_upper = position >= 0.7   # 接近上沿

    if not (near_lower or near_upper):
        return None

    # === 位置 + 形态过滤 ===
    # 不同位置只接受有意义的形态
    # 下沿：锤形线（长下影拒回）、看涨吞没（多头反攻）、启明星（三根反转）—— 这些是底部反转形态
    # 上沿/突破：看涨吞没（突破时多头强势）、启明星（蓄势突破）
    # 倒锤形线、刺透线在区间底部意义不大，不计入
    LOWER_PATTERNS = {"hammer", "bullish_engulfing", "morning_star", "piercing_line"}
    UPPER_PATTERNS = {"bullish_engulfing", "morning_star"}

    patterns = detect_all_patterns(klines, idx=-2)
    pattern = None
    for p in patterns:
        if p["direction"] != "bullish":
            continue
        pname = p["pattern"]
        if near_lower and pname in LOWER_PATTERNS:
            pattern = p
            break
        if near_upper and pname in UPPER_PATTERNS:
            pattern = p
            break

    # 无形态 → 不命中
    if not pattern:
        return None

    # 波动率收敛
    mid = n // 2
    early_range = float(highs[:mid].max() - lows[:mid].min())
    late_range = float(highs[mid:].max() - lows[mid:].min())
    volatility_ratio = late_range / early_range if early_range > 0 else 1.0

    # === 强度评分 ===
    breakout_threshold = config.get("breakout_threshold", 0.005)
    broke_upper = (close_last - upper_now) / upper_now >= breakout_threshold

    strength = 0.0
    signal_reason = ""

    if near_lower:
        strength = pattern["strength"] * 0.9
        signal_reason = f"区间下沿{pattern['pattern']}"
        if volatility_ratio < 1.0:
            strength = min(strength + 0.1, 1.0)
    elif near_upper:
        if broke_upper:
            strength = min(pattern["strength"] + 0.2, 1.0)
            signal_reason = f"突破上沿+{pattern['pattern']}"
        else:
            strength = pattern["strength"] * 0.8
            signal_reason = f"区间上沿{pattern['pattern']}"

    return {
        "signal_type": RANGE_BOUND,
        "current_price": close_last,
        "breakout_pct": float(((close_last - lower_now) / lower_now) * 100),
        "trend_slope": float(slope_h),
        "r_squared": float(max(r2_h, r2_l)),
        "upper_band": float(upper_now),
        "lower_band": float(lower_now),
        "band_position": float(position),
        "volatility_ratio": float(volatility_ratio),
        "is_converging": is_converging,
        "pattern": pattern["pattern"],
        "signal_reason": signal_reason,
        "strength": strength,
    }
