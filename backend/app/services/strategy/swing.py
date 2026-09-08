"""摆动点识别与 HH/HL/LH/LL 结构分析工具"""
from __future__ import annotations

import numpy as np
from typing import Optional


def find_swing_points(
    highs: np.ndarray,
    lows: np.ndarray,
    order: int = 3,
) -> tuple[list[int], list[int]]:
    """识别摆动高点和摆动低点索引

    摆动高点：highs[i] > 左右各 order 根的最高价
    摆动低点：lows[i]  < 左右各 order 根的最低价
    返回: (swing_high_idx, swing_low_idx)
    """
    n = len(highs)
    if n < 2 * order + 1:
        return [], []

    high_idx: list[int] = []
    low_idx: list[int] = []

    for i in range(order, n - order):
        left_h = highs[i - order : i]
        right_h = highs[i + 1 : i + order + 1]
        if highs[i] > left_h.max() and highs[i] > right_h.max():
            high_idx.append(i)

        left_l = lows[i - order : i]
        right_l = lows[i + 1 : i + order + 1]
        if lows[i] < left_l.min() and lows[i] < right_l.min():
            low_idx.append(i)

    return high_idx, low_idx


def merge_swings(
    high_idx: list[int],
    low_idx: list[int],
    highs: np.ndarray,
    lows: np.ndarray,
) -> list[tuple[int, str, float]]:
    """合并摆动点为按时间排序的序列

    返回: [(index, 'H'|'L', price), ...]
    过滤掉连续同类型的摆动点（保留极值）
    """
    points: list[tuple[int, str, float]] = []
    for i in high_idx:
        points.append((i, "H", float(highs[i])))
    for i in low_idx:
        points.append((i, "L", float(lows[i])))

    points.sort(key=lambda x: x[0])

    # 过滤连续同类型，保留极值
    filtered: list[tuple[int, str, float]] = []
    for p in points:
        if filtered and filtered[-1][1] == p[1]:
            # 连续高点保留更高的，连续低点保留更低的
            if p[1] == "H" and p[2] > filtered[-1][2]:
                filtered[-1] = p
            elif p[1] == "L" and p[2] < filtered[-1][2]:
                filtered[-1] = p
        else:
            filtered.append(p)

    return filtered


def classify_structure(swings: list[tuple[int, str, float]]) -> dict:
    """分析摆动点结构，判断 HH/HL/LH/LL

    返回结构特征：
    - hh_count: 更高高点数量（最近若干个 H 中）
    - lh_count: 更低高点数量
    - hl_count: 更高低点数量
    - ll_count: 更低低点数量
    - trend: 'up' | 'down' | 'range' | 'unknown'
    - last_h, last_l: 最近的高低点价格
    """
    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]

    hh = lh = hl = ll = 0
    # 比较相邻高点
    for i in range(1, len(highs_seq)):
        if highs_seq[i][2] > highs_seq[i - 1][2]:
            hh += 1
        else:
            lh += 1
    # 比较相邻低点
    for i in range(1, len(lows_seq)):
        if lows_seq[i][2] > lows_seq[i - 1][2]:
            hl += 1
        else:
            ll += 1

    last_h = highs_seq[-1][2] if highs_seq else None
    last_l = lows_seq[-1][2] if lows_seq else None

    # 判断趋势方向（基于最近 4 个摆动点的结构）
    recent = swings[-4:] if len(swings) >= 4 else swings
    recent_h = [p for p in recent if p[1] == "H"]
    recent_l = [p for p in recent if p[1] == "L"]

    trend = "unknown"
    if len(recent_h) >= 2 and len(recent_l) >= 2:
        up_cond = recent_h[-1][2] > recent_h[-2][2] and recent_l[-1][2] > recent_l[-2][2]
        down_cond = recent_h[-1][2] < recent_h[-2][2] and recent_l[-1][2] < recent_l[-2][2]
        if up_cond:
            trend = "up"
        elif down_cond:
            trend = "down"
        else:
            trend = "range"

    return {
        "hh_count": hh,
        "lh_count": lh,
        "hl_count": hl,
        "ll_count": ll,
        "trend": trend,
        "last_h": last_h,
        "last_l": last_l,
        "highs_seq": highs_seq,
        "lows_seq": lows_seq,
    }


def linear_regression(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """最小二乘线性回归，返回 (slope, intercept, r_squared)"""
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    sx = x.sum()
    sy = y.sum()
    sxy = (x * y).sum()
    sxx = (x * x).sum()
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot != 0 else 0.0
    return float(slope), float(intercept), float(r_squared)
