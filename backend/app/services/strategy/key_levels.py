"""关键位计算模块

关键位构成（见 docs/03-关键位筛选重构需求.md §2）：
- 前高/前低：最近一个摆动高点/低点（收盘价计算）
- 支撑位/压力位：更早的摆动点按价格聚类（≥2 次触及），被突破后角色互换
- 区间顶部/底部：震荡结构下用摆动点回归拟合的上下边界

关键位是区域：中心价 ± zone_tolerance（默认 ±0.5%），价格进入区域即算"到位"。
角色按当前价动态判定：关键位在当前价上方=压力，下方=支撑。
"""
from __future__ import annotations

import numpy as np

from app.services.strategy.swing import linear_regression
from app.services.strategy.types import (
    POS_PREV_HIGH,
    POS_PREV_LOW,
    POS_RANGE_BOTTOM,
    POS_RANGE_TOP,
    POS_RESISTANCE,
    POS_SUPPORT,
    RANGE_BOUND,
)


def _make_level(kind: str, price: float, touches: int, close_last: float, tol: float) -> dict:
    """构造关键位 dict，角色按当前价动态判定（上方=压力，下方=支撑）"""
    return {
        "kind": kind,
        "price": float(price),
        "zone_low": float(price * (1 - tol)),
        "zone_high": float(price * (1 + tol)),
        "touches": int(touches),
        "role": "resistance" if price > close_last else "support",
    }


def _cluster_levels(prices: list[float], merge_thr: float) -> list[tuple[float, int]]:
    """按价格聚类：相互距离 ≤ merge_thr 的点合并，返回 [(中心价, 触及次数)]，仅保留 ≥2 次触及"""
    if not prices:
        return []
    pts = sorted(prices)
    groups: list[list[float]] = [[pts[0]]]
    for p in pts[1:]:
        if abs(p - groups[-1][-1]) / groups[-1][-1] <= merge_thr:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [(sum(g) / len(g), len(g)) for g in groups if len(g) >= 2]


def compute_key_levels(
    swings: list[tuple],
    closes: np.ndarray,
    n_closed: int,
    signal_type: str,
    config: dict,
) -> list[dict]:
    """计算关键位列表。

    swings: merge_swings 输出 [(idx, "H"|"L", price), ...]
    signal_type: classify_structure 的分类结果（震荡时补充区间边界）
    """
    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]
    if not highs_seq or not lows_seq:
        return []

    close_last = float(closes[-2])
    tol = config.get("key_level_tolerance", 0.005)
    merge_thr = config.get("level_merge_threshold", 0.005)

    levels: list[dict] = []

    # 1. 前高/前低（最近一个摆动点）
    levels.append(_make_level(POS_PREV_HIGH, highs_seq[-1][2], 1, close_last, tol))
    levels.append(_make_level(POS_PREV_LOW, lows_seq[-1][2], 1, close_last, tol))

    # 2. 支撑/压力位：除最近点外的摆动点聚类（前高/前低已单独列出）
    for price, touches in _cluster_levels([p[2] for p in highs_seq[:-1]], merge_thr):
        levels.append(_make_level(POS_RESISTANCE, price, touches, close_last, tol))
    for price, touches in _cluster_levels([p[2] for p in lows_seq[:-1]], merge_thr):
        levels.append(_make_level(POS_SUPPORT, price, touches, close_last, tol))

    # 3. 区间顶/底（仅震荡结构）：摆动点回归拟合边界
    if signal_type == RANGE_BOUND:
        rh = highs_seq[-min(5, len(highs_seq)):]
        rl = lows_seq[-min(5, len(lows_seq)):]
        xh = np.array([p[0] for p in rh], dtype=float)
        yh = np.array([p[2] for p in rh], dtype=float)
        xl = np.array([p[0] for p in rl], dtype=float)
        yl = np.array([p[2] for p in rl], dtype=float)
        slope_h, inter_h, _ = linear_regression(xh, yh)
        slope_l, inter_l, _ = linear_regression(xl, yl)
        if np.isfinite(slope_h) and np.isfinite(slope_l):
            upper = slope_h * n_closed + inter_h
            lower = slope_l * n_closed + inter_l
            if upper > lower > 0 and upper / lower - 1 >= 0.005:
                levels.append(_make_level(POS_RANGE_TOP, upper, len(rh), close_last, tol))
                levels.append(_make_level(POS_RANGE_BOTTOM, lower, len(rl), close_last, tol))

    return levels


def find_touching_level(levels: list[dict], kline: list) -> dict | None:
    """找出最新已收盘 K 线触及的关键位。

    优先级：收盘价进入区域 > 影线刺入区域；同优先级取距中心最近。
    kline: [open_time, open, high, low, close, ...]
    """
    o, h, l, c = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
    best: tuple[tuple, dict] | None = None
    for lv in levels:
        zl, zh = lv["zone_low"], lv["zone_high"]
        if zl <= c <= zh:
            pri = 0  # 收盘触及
        elif l <= zh and h >= zl:
            pri = 1  # 影线刺入
        else:
            continue
        dist = abs(c - lv["price"]) / lv["price"] if lv["price"] > 0 else 1e9
        key = (pri, dist)
        if best is None or key < best[0]:
            best = (key, lv)
    return best[1] if best else None


def fmt_price(p: float) -> str:
    """价格展示（signal_reason 用）"""
    if p < 1:
        return f"{p:.6f}"
    if p < 100:
        return f"{p:.4f}"
    return f"{p:.2f}"
