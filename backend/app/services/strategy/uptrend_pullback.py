"""策略三：上涨趋势回调

优化逻辑：
1. 用摆动点识别历史上涨趋势（HH+HL），这部分仍需 order 确认
2. 只在最后一根已收盘 K 线上检测看涨形态（回调中首次出现的形态）
3. 如果前面已经连续上涨 N 根，说明已经涨了，不再算回调第一根
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
from app.services.strategy.types import UPTREND_PULLBACK


def _is_bullish_candle(k: list) -> bool:
    """判断是否为阳线"""
    return float(k[4]) > float(k[1])


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

    if len(swings) < 4:
        return None

    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]

    if len(highs_seq) < 2 or len(lows_seq) < 2:
        return None

    # 用最后一根已收盘 K 线（klines[-2]），不用未收盘的 klines[-1]
    close_last = float(closes[-2])
    n_closed = n - 1

    # === 条件1：确认历史上涨趋势 ===
    recent_highs = highs_seq[-3:] if len(highs_seq) >= 3 else highs_seq[-2:]
    is_hh_sequence = all(
        recent_highs[i][2] < recent_highs[i + 1][2]
        for i in range(len(recent_highs) - 1)
    )

    recent_lows = lows_seq[-3:] if len(lows_seq) >= 3 else lows_seq[-2:]
    is_hl_sequence = all(
        recent_lows[i][2] < recent_lows[i + 1][2]
        for i in range(len(recent_lows) - 1)
    )

    if not (is_hh_sequence or is_hl_sequence):
        return None

    # === 条件2：拟合上升趋势线 ===
    x = np.array([p[0] for p in recent_lows], dtype=float)
    y = np.array([p[2] for p in recent_lows], dtype=float)
    slope, intercept, r2 = linear_regression(x, y)

    if not np.isfinite(slope) or slope <= 0:
        return None

    trend_value = slope * n_closed + intercept
    if trend_value <= 0:
        return None

    # === 条件3：回调位置判定 ===
    pullback_tolerance = config.get("pullback_tolerance", 0.03)

    distance_to_trend = (close_last - trend_value) / trend_value
    near_uptrend_line = 0 <= distance_to_trend <= pullback_tolerance

    last_low = recent_lows[-1][2]
    distance_to_hl = (close_last - last_low) / last_low
    near_hl = 0 <= distance_to_hl <= pullback_tolerance

    recent_high = recent_highs[-1][2]
    pullback_depth = (recent_high - close_last) / recent_high
    reasonable_pullback = 0.02 <= pullback_depth <= 0.15

    position_ok = near_uptrend_line or near_hl or reasonable_pullback
    if not position_ok:
        return None

    # 未跌破趋势线太多
    if distance_to_trend < -pullback_tolerance:
        return None

    # === 条件4：只在最后一根已收盘 K 线上检测形态 ===
    # 回调低点有效形态：锤形线、看涨吞没、启明星、刺透线
    # 倒锤形线在回调位意义不大（上方有抛压），不计入
    VALID_PATTERNS = {"hammer", "bullish_engulfing", "morning_star", "piercing_line"}
    patterns = detect_all_patterns(klines, idx=-2)
    pattern = None
    for p in patterns:
        if p["direction"] == "bullish" and p["pattern"] in VALID_PATTERNS:
            pattern = p
            break

    # 无形态 → 不命中
    if not pattern:
        return None

    # === 条件5：排除已经涨了几根的情况 ===
    # 检查形态 K 线（klines[-2]）之前 max_consecutive_bull 根是否连续阳线
    # 如果连续上涨，说明回调已经发生并开始反弹了，不是第一根形态
    max_consecutive_bull = config.get("max_consecutive_bull", 1)
    pattern_idx = n - 2  # klines[-2] 的索引
    consecutive_bull_before = 0
    for i in range(pattern_idx - 1, max(pattern_idx - 1 - max_consecutive_bull, 0), -1):
        if _is_bullish_candle(klines[i]):
            consecutive_bull_before += 1
        else:
            break

    if consecutive_bull_before >= max_consecutive_bull:
        # 前面已经连续上涨，不是回调第一根形态
        return None

    # === 强度评分 ===
    strength = 0.0
    signal_reason = ""

    if near_uptrend_line and reasonable_pullback:
        strength = min(pattern["strength"] + 0.2, 1.0)
        signal_reason = f"趋势线+回调幅度合理+{pattern['pattern']}"
    elif near_uptrend_line:
        strength = pattern["strength"] * 0.9
        signal_reason = f"趋势线附近+{pattern['pattern']}"
    elif near_hl:
        strength = pattern["strength"] * 0.85
        signal_reason = f"HL附近+{pattern['pattern']}"
    else:
        strength = pattern["strength"] * 0.7
        signal_reason = f"回调中+{pattern['pattern']}"

    breakout_pct = float(distance_to_trend * 100)

    return {
        "signal_type": UPTREND_PULLBACK,
        "current_price": close_last,
        "breakout_pct": breakout_pct,
        "trend_slope": float(slope),
        "r_squared": float(r2),
        "pullback_depth": float(pullback_depth * 100),
        "distance_to_trend": float(distance_to_trend * 100),
        "pattern": pattern["pattern"],
        "signal_reason": signal_reason,
        "strength": strength,
    }
