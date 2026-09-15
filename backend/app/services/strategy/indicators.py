"""通用指标工具（原 key_levels.py 内的公共函数，2026-09-14 随关键位功能移除迁出）

叶子模块：不 import strategy 包内其他模块（防循环导入）。
- calc_atr：AI 事实包（Agent 管线）共用；
- volume_ratio：classify_volume 分档与突破量能门槛（BREAKOUT_VOL_RATIO）共用；
- fmt_price：signal_reason 价格展示。
"""
from __future__ import annotations

from typing import Optional

# 放量突破的量能门槛：收盘K线成交量 ≥ 该倍数 × 近20根均量（volume_ratio 唯一口径）
BREAKOUT_VOL_RATIO = 1.2


def calc_atr(klines: list, period: int = 14) -> Optional[float]:
    """ATR(period)：最近 period 根已收盘 K 线的真实波幅均值（klines[-1] 未收盘）。

    供 AI 事实包与 risk_guard（止损宽度/强平口径）共用（risk_guard 由此处导入）。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l = float(closed[i][2]), float(closed[i][3])
        prev_c = float(closed[i - 1][4])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs)


def volume_ratio(klines: list) -> float:
    """最新已收盘 K 线成交量 ÷ 近 20 根已收盘均量（数据不足或均量为 0 时返回 0）。

    classify_volume 的分档与突破量能门槛（BREAKOUT_VOL_RATIO）共用此口径。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < 22:
        return 0.0
    avg = sum(float(k[5]) for k in closed[-21:-1]) / 20
    if avg <= 0:
        return 0.0
    return float(closed[-1][5]) / avg


def fmt_price(p: float) -> str:
    """价格展示（signal_reason 用）"""
    if p < 1:
        return f"{p:.6f}"
    if p < 100:
        return f"{p:.4f}"
    return f"{p:.2f}"
