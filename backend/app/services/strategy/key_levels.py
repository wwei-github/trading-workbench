"""关键位计算模块

关键位构成（见 docs/03-关键位筛选重构需求.md §2）：
- 最近摆动高点/低点、历史摆动点聚类（时间加权 ≥ 阈值）、震荡结构的回归边界——
  三种来源照常计算，但标签只保留两类（2026-09-12 两类化）：
  支撑位(support) / 压力位(resistance)，kind 由角色动态推导。

关键位是区域：中心价 ± zone_tolerance。区域半宽随波动自适应：
ATR% 越大区域越宽（0.5×ATR），限制在配置值 key_level_tolerance 的 [0.5×, 2×] 倍内——
高波动币不会因固定 0.5% 太窄而频繁假触及，低波动币不会太宽而到处都是信号。
角色按当前价动态判定：关键位在当前价上方=压力，下方=支撑（kind == role）。

时间加权（2026-09-12 优化）：
- 每次触及的权重 = 2^(-bars_ago / 半衰期)，半衰期 60 根已收盘 K 线
- 聚类簇按总权重过滤（≥1.2，约等于"近期 2 次触及"），纯久远旧点自然衰减淘汰；
  触及次数 touches 仍保留原始计数供展示
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.services.strategy.swing import linear_regression
from app.services.strategy.types import RANGE_BOUND

# 触及权重半衰期（已收盘 K 线根数）与聚类保留阈值（≈2 次近期触及）
TOUCH_HALF_LIFE = 60
MIN_CLUSTER_WEIGHT = 1.2

# 放量突破的量能门槛：收盘K线成交量 ≥ 该倍数 × 近20根均量（volume_ratio 唯一口径）
BREAKOUT_VOL_RATIO = 1.2


def calc_atr(klines: list, period: int = 14) -> Optional[float]:
    """ATR(period)：最近 period 根已收盘 K 线的真实波幅均值（klines[-1] 未收盘）。

    供关键位区域自适应宽度与 AI 事实包共用（risk_guard 由此处导入）。
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


def _make_level(price: float, touches: int, close_last: float, tol: float,
                weight: Optional[float] = None) -> dict:
    """构造关键位 dict。

    【2026-09-12 两类化】kind 由角色动态推导：位在当前价上方=压力位(resistance)，
    下方=支撑位(support)——kind == role，不再区分前高/前低/区间顶底等来源标签。
    """
    role = "resistance" if price > close_last else "support"
    lv = {
        "kind": role,
        "price": float(price),
        "zone_low": float(price * (1 - tol)),
        "zone_high": float(price * (1 + tol)),
        "touches": int(touches),
        "role": role,
    }
    if weight is not None:
        lv["weight"] = round(float(weight), 2)  # 时间加权触及强度（最近摆动点无聚类权重）
    return lv


def _touch_weight(idx: int, n_closed: int) -> float:
    """单次触及的时间权重：越近越重，半衰期 TOUCH_HALF_LIFE 根"""
    bars_ago = max(n_closed - int(idx), 0)
    return 2.0 ** (-bars_ago / TOUCH_HALF_LIFE)


def _cluster_levels(points: list[tuple[float, int]], merge_thr: float, n_closed: int,
                    ) -> list[tuple[float, int, float]]:
    """按价格聚类：相互距离 ≤ merge_thr 的点合并（时间加权），
    返回 [(中心价, 触及次数, 总权重)]，仅保留总权重 ≥ MIN_CLUSTER_WEIGHT 的簇
    （权重 1.2 上限单点 1.0，天然要求 ≥2 次触及，且纯久远旧点会被衰减淘汰）"""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    groups: list[list[tuple[float, int]]] = [[pts[0]]]
    for p in pts[1:]:
        if abs(p[0] - groups[-1][-1][0]) / groups[-1][-1][0] <= merge_thr:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        weight = sum(_touch_weight(idx, n_closed) for _, idx in g)
        if weight >= MIN_CLUSTER_WEIGHT:
            out.append((sum(p[0] for p in g) / len(g), len(g), weight))
    return out


def compute_key_levels(
    swings: list[tuple],
    closes: np.ndarray,
    n_closed: int,
    signal_type: str,
    config: dict,
    klines: Optional[list] = None,
) -> list[dict]:
    """计算关键位列表。

    swings: merge_swings 输出 [(idx, "H"|"L", price), ...]
    signal_type: classify_structure 的分类结果（震荡时补充区间边界）
    klines: 传入时可按 ATR 自适应区域半宽（不传则用配置固定值）
    """
    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]
    if not highs_seq or not lows_seq:
        return []

    close_last = float(closes[-2])
    base_tol = config.get("key_level_tolerance", 0.005)
    merge_thr = config.get("level_merge_threshold", 0.005)

    # 区域半宽自适应：0.5×ATR，限制在配置容忍度的 [0.5×, 2×] 内
    tol = base_tol
    if klines:
        atr = calc_atr(klines)
        if atr and close_last > 0:
            tol = min(max(0.5 * atr / close_last, base_tol * 0.5), base_tol * 2)

    levels: list[dict] = []

    # 1. 最近摆动点（天然最新，无聚类权重）
    levels.append(_make_level(highs_seq[-1][2], 1, close_last, tol))
    levels.append(_make_level(lows_seq[-1][2], 1, close_last, tol))

    # 2. 历史摆动点聚类（除最近点外，时间加权）
    for price, touches, weight in _cluster_levels(
            [(p[2], p[0]) for p in highs_seq[:-1]], merge_thr, n_closed):
        levels.append(_make_level(price, touches, close_last, tol, weight))
    for price, touches, weight in _cluster_levels(
            [(p[2], p[0]) for p in lows_seq[:-1]], merge_thr, n_closed):
        levels.append(_make_level(price, touches, close_last, tol, weight))

    # 3. 区间回归边界（仅震荡结构）：按角色归类为压力位/支撑位
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
                levels.append(_make_level(upper, len(rh), close_last, tol))
                levels.append(_make_level(lower, len(rl), close_last, tol))

    return levels


def _level_side(lv: dict, prev_close: float) -> str:
    """位在前收盘时的侧向：support-like（位在前收下方/持平）或 resistance-like。

    突破与触位的判定都需要"这一根K线之前价格在位的哪一侧"作上下文——
    收盘在支撑位上方是常态，只有从前收上方跌穿区域才是破位。
    """
    return "support" if prev_close >= lv["price"] else "resistance"


def find_touching_level(levels: list[dict], kline: list, prev_close: float) -> dict | None:
    """找出最新已收盘 K 线触及的关键位（持住侧规则，2026-09-12）。

    触及 = 影线与区域重叠 且 收盘在"持住侧"（support-like 位要求收盘 ≥ zone_low，
    resistance-like 要求收盘 ≤ zone_high）。收盘在区域内是子集，天然涵盖；
    收盘越过区域远侧（破位方向）不属于触及——那归 find_broken_level 的放量门控管。
    同分取距中心最近。
    kline: [open_time, open, high, low, close, ...]
    """
    h, l, c = float(kline[2]), float(kline[3]), float(kline[4])
    best: tuple[tuple, dict] | None = None
    for lv in levels:
        zl, zh = lv["zone_low"], lv["zone_high"]
        if not (l <= zh and h >= zl):
            continue  # 影线未与区域重叠
        if _level_side(lv, prev_close) == "support":
            if c < zl:
                continue  # 收盘跌穿区域：破位，不是触及
        else:
            if c > zh:
                continue  # 收盘涨破区域：突破，不是触及
        dist = abs(c - lv["price"]) / lv["price"] if lv["price"] > 0 else 1e9
        if best is None or dist < best[0]:
            best = (dist, lv)
    return best[1] if best else None


def find_broken_level(levels: list[dict], kline: list, prev_close: float) -> Optional[dict]:
    """找出最新已收盘 K 线放量突破的关键位（破位距离最大者）。

    突破 = 收盘越过整个区域：resistance-like 位要求收盘 > zone_high（向上突破），
    support-like 位要求收盘 < zone_low（向下破位）。返回 None 表示本根无突破。
    kline: [open_time, open, high, low, close, ...]
    """
    c = float(kline[4])
    best: tuple[float, dict, str] | None = None
    for lv in levels:
        side = _level_side(lv, prev_close)
        if side == "resistance" and c > lv["zone_high"]:
            edge, direction = lv["zone_high"], "up"
        elif side == "support" and c < lv["zone_low"]:
            edge, direction = lv["zone_low"], "down"
        else:
            continue
        pct = (c - edge) / edge * 100 if edge > 0 else 0.0
        if best is None or abs(pct) > abs(best[0]):
            best = (pct, lv, direction)
    if best is None:
        return None
    return {"level": best[1], "direction": best[2], "pct": best[0]}


def fmt_price(p: float) -> str:
    """价格展示（signal_reason 用）"""
    if p < 1:
        return f"{p:.6f}"
    if p < 100:
        return f"{p:.4f}"
    return f"{p:.2f}"
