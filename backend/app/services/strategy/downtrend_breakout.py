"""策略一：下跌趋势突破

优化逻辑：
1. 用摆动点识别历史下跌趋势（LH+LL），这部分仍需 order 确认
2. 只看最后一根已收盘 K 线是否突破下跌趋势线
3. 突破必须是刚发生的：前一根 K 线收盘价在趋势线下方
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
from app.services.strategy.types import DOWNTREND_BREAKOUT


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
    close_prev = float(closes[-3])  # 倒数第 2 根已收盘
    n_closed = n - 1

    # === 条件1：确认历史下跌趋势 ===
    recent_highs = highs_seq[-3:] if len(highs_seq) >= 3 else highs_seq[-2:]
    is_lh_sequence = all(
        recent_highs[i][2] > recent_highs[i + 1][2]
        for i in range(len(recent_highs) - 1)
    )

    recent_lows = lows_seq[-3:] if len(lows_seq) >= 3 else lows_seq[-2:]
    is_ll_sequence = all(
        recent_lows[i][2] > recent_lows[i + 1][2]
        for i in range(len(recent_lows) - 1)
    )

    if not (is_lh_sequence or is_ll_sequence):
        return None

    # === 条件2：拟合下跌趋势线 ===
    x = np.array([p[0] for p in recent_highs], dtype=float)
    y = np.array([p[2] for p in recent_highs], dtype=float)
    slope, intercept, r2 = linear_regression(x, y)

    if not np.isfinite(slope) or slope >= 0:
        return None

    r2_threshold = config.get("r_squared_threshold", 0.5)
    if r2 < r2_threshold:
        return None

    # 趋势线在最后已收盘 K 线位置的预测值
    trend_value_now = slope * n_closed + intercept
    # 趋势线在前一根已收盘 K 线位置的预测值
    trend_value_prev = slope * (n_closed - 1) + intercept

    if trend_value_now <= 0:
        return None

    # === 条件3：最新已收盘 K 线突破趋势线 ===
    breakout_threshold = config.get("breakout_threshold", 0.005)
    breakout_pct = (close_last - trend_value_now) / trend_value_now
    broke_trendline = breakout_pct >= breakout_threshold

    if not broke_trendline:
        return None

    # === 条件4：突破必须是刚发生的 ===
    # 前一根收盘价在趋势线下方（或刚好在趋势线上），说明是最新这根刚突破
    prev_vs_trend = (close_prev - trend_value_prev) / trend_value_prev
    was_below_trend = prev_vs_trend < breakout_threshold

    if not was_below_trend:
        # 前一根已经突破了，不是新突破
        return None

    # 限制异常突破
    if breakout_pct > 5.0:
        return None

    # === 形态加分（可选）===
    patterns = detect_all_patterns(klines, idx=-2)
    pattern = None
    for p in patterns:
        if p["direction"] == "bullish":
            pattern = p
            break

    strength = 0.7
    signal_reason = "突破下跌趋势线"

    if pattern:
        strength = min(pattern["strength"] + 0.3, 1.0)
        signal_reason = f"突破趋势线 + {pattern['pattern']}"

    return {
        "signal_type": DOWNTREND_BREAKOUT,
        "current_price": close_last,
        "breakout_pct": float(breakout_pct * 100),
        "trend_slope": float(slope),
        "r_squared": float(r2),
        "formed_hh": True,
        "broke_trendline": True,
        "pattern": pattern["pattern"] if pattern else None,
        "signal_reason": signal_reason,
        "strength": strength,
    }
