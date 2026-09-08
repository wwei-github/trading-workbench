"""策略一：下跌趋势突破

逻辑：
1. 识别下跌趋势：连续 LH（更低高点）+ LL（更跌低点），即摆动高点和摆动低点均下移
2. 用最近的摆动高点拟合下跌趋势线（斜率 < 0）
3. 最新收盘价向上突破该趋势线，或形成 HH（更高高点）打破 LH 序列
4. 重点关注：LH 序列被打破（出现第一个 HH）
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
from app.services.strategy.types import DOWNTREND_BREAKOUT


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

    # 条件1：近期为下跌趋势（LH + LL）
    # 检查最近 3 个摆动高点是否连续下移
    recent_highs = highs_seq[-3:]
    is_lh_sequence = all(recent_highs[i][2] > recent_highs[i + 1][2] for i in range(len(recent_highs) - 1))

    recent_lows = lows_seq[-3:]
    is_ll_sequence = all(recent_lows[i][2] > recent_lows[i + 1][2] for i in range(len(recent_lows) - 1))

    if not (is_lh_sequence or is_ll_sequence):
        return None

    # 条件2：用最近的摆动高点拟合下跌趋势线
    x = np.array([p[0] for p in recent_highs], dtype=float)
    y = np.array([p[2] for p in recent_highs], dtype=float)
    slope, intercept, r2 = linear_regression(x, y)

    if not np.isfinite(slope) or slope >= 0:
        return None

    r2_threshold = config.get("r_squared_threshold", 0.5)
    if r2 < r2_threshold:
        return None

    # 趋势线在当前位置的预测值
    trend_value = slope * (n - 1) + intercept
    if trend_value <= 0 or trend_value < close_last * 0.1:
        return None

    # 条件3：突破判定（二选一）
    breakout_threshold = config.get("breakout_threshold", 0.005)
    breakout_pct = (close_last - trend_value) / trend_value

    # 方式A：收盘价突破趋势线
    broke_trendline = breakout_pct >= breakout_threshold

    # 方式B：形成 HH（最近摆动高点 > 前一个摆动高点），打破 LH 序列
    formed_hh = False
    if len(highs_seq) >= 2:
        formed_hh = highs_seq[-1][2] > highs_seq[-2][2]

    if not (broke_trendline or formed_hh):
        return None

    # 限制异常突破
    if breakout_pct > 5.0:
        return None

    strength = 0.0
    if formed_hh and broke_trendline:
        strength = 1.0
    elif formed_hh:
        strength = 0.7
    else:
        strength = 0.5

    return {
        "signal_type": DOWNTREND_BREAKOUT,
        "current_price": close_last,
        "breakout_pct": float(breakout_pct * 100),
        "trend_slope": float(slope),
        "r_squared": float(r2),
        "formed_hh": formed_hh,
        "broke_trendline": broke_trendline,
        "strength": strength,
    }
