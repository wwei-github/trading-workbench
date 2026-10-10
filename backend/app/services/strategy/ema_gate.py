"""EMA 趋势方向门（docs/19 §2，2026-10-10 融合拍板：影子层退役并入原生触发层）。

两个纯函数，供 trigger.py 的 trend_pullback 触发类型作前置方向过滤：
- gate_4h：4H EMA144 趋势闸门（三重否决——斜率走平 / 缠绕震荡 / ADX 弱），
  输出方向 long/short 或 None；
- structure_1h：1H 三线排列 + ATR 归一发散度结构门，输出结构证据或 None。

已收盘判定纪律由调用方保证（klines 已剔除未收盘根）。
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.services.strategy.technical import analyze_adx

# EMA144 斜率回看根数（4h）与走平阈值（20 根变化幅度/价格 < 0.5% → 走平）
SLOPE_BARS = 20
SLOPE_FLAT_PCT = 0.005
# 缠绕判定：近 10 根收盘价穿越 EMA144 次数 ≥3 → 震荡拒绝
WRAP_WINDOW = 10
WRAP_MAX = 2


def ema_series(closes: np.ndarray, period: int) -> Optional[np.ndarray]:
    """EMA 全序列（前 period-1 根 NaN）。样本不足返回 None。"""
    if len(closes) < period + 5:
        return None
    a = 2.0 / (period + 1)
    out = np.full(len(closes), np.nan)
    out[period - 1] = closes[:period].mean()
    for i in range(period, len(closes)):
        out[i] = a * closes[i] + (1 - a) * out[i - 1]
    return out


@dataclass
class EmaGateConfig:
    """方向门参数（触发层固定口径，与 docs/19 出厂值一致）。"""
    div_fast_mid: float = 0.5       # 1h |EMA21-EMA55|/ATR 下限
    div_mid_slow: float = 1.0       # 1h |EMA55-EMA144|/ATR 下限
    adx_min: float = 20.0           # 4h ADX 趋势闸门下限


def gate_4h(k4: list[list], cfg: Optional[EmaGateConfig] = None) -> Optional[tuple[str, dict]]:
    """4H EMA144 趋势闸门：三重否决（走平/缠绕/ADX）全过才给方向。

    返回 (direction "long"/"short", 证据 dict) 或 None（闸门不过）。
    证据 direction 语义为 "up"/"down"（4h K 线趋势方向，观测/前端展示口径）。
    """
    cfg = cfg or EmaGateConfig()
    if len(k4) < 200:
        return None
    c = np.array([float(k[4]) for k in k4], dtype=float)
    e144 = ema_series(c, 144)
    if e144 is None or not np.isfinite(e144[-1]) or not np.isfinite(e144[-SLOPE_BARS]):
        return None
    price = float(c[-1])
    slope_pct = abs(float(e144[-1]) - float(e144[-SLOPE_BARS])) / price

    # 缠绕：近 WRAP_WINDOW 根收盘价在 EMA144 两侧的穿越次数
    wraps = 0
    for i in range(len(k4) - WRAP_WINDOW, len(k4)):
        side_now = c[i] - e144[i]
        side_prev = c[i - 1] - e144[i - 1]
        if side_now * side_prev < 0:
            wraps += 1

    adx_info = analyze_adx(k4, period=14)
    adx_val = float(adx_info["adx"]) if adx_info else None

    ev = {"slope_pct": round(slope_pct, 5), "wraps": wraps,
          "adx": round(adx_val, 1) if adx_val is not None else None,
          "close_vs_ema144": round(price / float(e144[-1]) - 1, 5)}

    if slope_pct < SLOPE_FLAT_PCT:
        return None  # 走平
    if wraps > WRAP_MAX:
        return None  # 缠绕
    if adx_val is None or adx_val < cfg.adx_min:
        return None  # 无趋势

    up = price > e144[-1] and e144[-1] >= e144[-SLOPE_BARS]
    down = price < e144[-1] and e144[-1] <= e144[-SLOPE_BARS]
    if up:
        return "long", {**ev, "direction": "up"}
    if down:
        return "short", {**ev, "direction": "down"}
    return None


def structure_1h(k1: list[list], direction: str,
                 cfg: Optional[EmaGateConfig] = None) -> Optional[tuple[dict, dict]]:
    """1H 三线排列 + 发散度结构门（direction="long"/"short"）。

    返回 ({d21,d55,d144,atr}, 结构证据 dict) 或 None（结构不过）。
    """
    cfg = cfg or EmaGateConfig()
    if len(k1) < 200:
        return None
    c = np.array([float(k[4]) for k in k1], dtype=float)
    e21 = ema_series(c, 21)
    e55 = ema_series(c, 55)
    e144 = ema_series(c, 144)
    if any(e is None or not np.isfinite(e[-1]) for e in (e21, e55, e144)):
        return None

    # ATR14（1h，Wilder 简化：TR 均值滚动）
    h = np.array([float(k[2]) for k in k1], dtype=float)
    low = np.array([float(k[3]) for k in k1], dtype=float)
    tr = np.maximum(h[1:] - low[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0
    if atr <= 0:
        return None

    d21, d55, d144 = float(e21[-1]), float(e55[-1]), float(e144[-1])
    if direction == "long":
        aligned = d21 > d55 > d144
        div_fm = (d21 - d55) / atr
        div_ms = (d55 - d144) / atr
    else:
        aligned = d21 < d55 < d144
        div_fm = (d55 - d21) / atr
        div_ms = (d144 - d55) / atr
    ev = {"aligned": aligned, "div_fast_mid": round(div_fm, 3),
          "div_mid_slow": round(div_ms, 3), "atr": round(atr, 8)}
    if not aligned or div_fm < cfg.div_fast_mid or div_ms < cfg.div_mid_slow:
        return None
    return {"d21": d21, "d55": d55, "d144": d144, "atr": atr}, ev
