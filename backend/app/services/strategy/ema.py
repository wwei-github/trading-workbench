"""EMA 均线计算与形态状态判定

默认三线 EMA21 / EMA55 / EMA144，基于已收盘 K 线（剔除最后一根未收盘）。

状态判定（优先级从高到低）：
- 金叉/死叉：EMA21 与 EMA55 最近 3 根内发生交叉（事件最新鲜，优先级最高）
- 拐头向上/向下：EMA21 斜率最近 3 根内变号（动能转换先于排列变化）
- 多头排列/空头排列：三线顺序排列（快>中>慢 为多头 / 快<中<慢 为空头）
- 纠结：以上均不满足
"""
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# 状态 → 中文标签（AI 提示词与前端共用同一套状态键）
EMA_STATE_LABELS = {
    "bullish_align": "多头排列",
    "bearish_align": "空头排列",
    "bullish_cross": "金叉",
    "bearish_cross": "死叉",
    "turning_up": "拐头向上",
    "turning_down": "拐头向下",
    "mixed": "纠缠",
}

DEFAULT_PERIODS = (21, 55, 144)


def calc_ema(values: np.ndarray, period: int) -> Optional[np.ndarray]:
    """标准 EMA：前 period 个用 SMA 作种子，其后按系数 2/(period+1) 递推。

    数据不足 period 时返回 None。
    """
    n = len(values)
    if n < period:
        return None
    arr = np.asarray(values, dtype=float)
    ema = np.full(n, np.nan)
    ema[period - 1] = arr[:period].mean()
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = arr[i] * k + ema[i - 1] * (1 - k)
    return ema


def analyze_ema(
    klines: list[list], periods: tuple[int, int, int] = DEFAULT_PERIODS,
) -> Optional[dict]:
    """分析均线形态状态。

    klines: [[open_time, open, high, low, close, ...], ...]（最后一根未收盘，内部剔除）
    数据不足（已收盘 < 最长周期 + 10）时返回 None。

    返回: {fast_period, mid_period, slow_period, fast, mid, slow,
          state, state_label, rising, detail}
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    closes = np.array([float(k[4]) for k in closed], dtype=float)

    p_fast, p_mid, p_slow = periods
    if len(closes) < max(periods) + 10:
        return None

    f = calc_ema(closes, p_fast)
    m = calc_ema(closes, p_mid)
    s = calc_ema(closes, p_slow)
    if f is None or m is None or s is None:
        return None

    fn, mn, sn = float(f[-1]), float(m[-1]), float(s[-1])

    # EMA21 斜率（最近 4 根的逐根差分）
    slopes = np.diff(f[-5:])
    rising = bool(slopes[-1] > 0)

    # 交叉：最近 3 根内 EMA21 与 EMA55 上下穿
    cross_up = cross_down = False
    for i in range(len(f) - 3, len(f)):
        if f[i] > m[i] and f[i - 1] <= m[i - 1]:
            cross_up = True
        elif f[i] < m[i] and f[i - 1] >= m[i - 1]:
            cross_down = True

    # 拐头：斜率最近 3 根内变号（动能转换）
    prev_slopes = slopes[:-1]
    turning_up = rising and bool((prev_slopes < 0).any())
    turning_down = (not rising) and bool((prev_slopes > 0).any())

    if cross_up:
        state = "bullish_cross"
    elif cross_down:
        state = "bearish_cross"
    elif turning_up:
        state = "turning_up"
    elif turning_down:
        state = "turning_down"
    elif fn > mn > sn:
        state = "bullish_align"
    elif fn < mn < sn:
        state = "bearish_align"
    else:
        state = "mixed"

    arrow = "↑" if rising else "↓"
    detail = (
        f"EMA{p_fast}={fn:.6g}{arrow}, EMA{p_mid}={mn:.6g}, EMA{p_slow}={sn:.6g}"
    )

    return {
        "fast_period": p_fast,
        "mid_period": p_mid,
        "slow_period": p_slow,
        "fast": fn,
        "mid": mn,
        "slow": sn,
        "state": state,
        "state_label": EMA_STATE_LABELS[state],
        "rising": rising,
        "detail": detail,
    }
