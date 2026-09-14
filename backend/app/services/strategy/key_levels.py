"""关键位计算模块——支撑/压力单价格线（2026-09-14 去区域化重写，docs/08）

- 摆动点（收盘价，两侧各 order 根确认）：低点→支撑线、高点→压力线，
  角色由来源固定，不再按当前价动态互换；
- 全部摆动点按价距 ≤ level_merge_threshold 链式聚合为一条线（均值价，touches=成员数）——
  价格相近的线一律融合，无"最新点单独成线"例外（2026-09-14 收敛：图上不再出现贴脸双线）；
- 每侧最多 LEVELS_PER_SIDE=3 条（按时间新→旧截断，全图 ≤6 条）；
- pattern_hits：摆动点当根及确认窗内出现方向匹配 12 金K 的成员数（形态确认的触及次数）；
- 触及/突破判定用固定容差带 tol（=key_level_tolerance，DEFAULT_TOL 为镜像缺省）：
  触及 = 影线与 [price×(1−tol), price×(1+tol)] 相交且收盘在持住侧；
  突破 = 收盘越过 price×(1±tol)；
- 输出 {kind, role, price, touches, pattern_hits}——无区域（zone）/权重（weight）概念，
  历史数据中的 zone/weight 字段由消费方按"price 恒存在"口径兼容。

统一入口（2026-09-14）：compute_key_levels(klines, config) 内部完成摆动点→聚合线
全链路，图表端点/信号检测/AI 事实包/风控锚定共用——图上看到的线即 AI 锚定的位。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.services.strategy import candlestick
from app.services.strategy.candlestick import GOLDEN_12
from app.services.strategy.swing import find_swing_points, merge_swings

# 每侧最多保留的线数（按时间新→旧截断：更老的历史线让位）。2026-09-14 由 5 收敛为 3：
# 相近价位已由聚合融合，3 条/侧（全图 ≤6 条）在图上足够清晰，弱位堆积无益
LEVELS_PER_SIDE = 3

# 判定容差缺省值：触及带/突破带 = price×(1±DEFAULT_TOL)。
# 信号检测路径由 _detect 传 config 的 key_level_tolerance；risk_guard 拿不到 DB config，
# 引用本常量作镜像（调整 DB config 的 key_level_tolerance 不再自动跟随风控侧带宽）。
DEFAULT_TOL = 0.003

# 形态确认窗（根）：启明星/黄昏星在极值后 1~2 根才完成，摆动点当根 + 后 2 根内命中都算
PATTERN_CONFIRM_WINDOW = 2

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


def _make_level(price: float, role: str, touches: int, pattern_hits: int) -> dict:
    """构造关键位 dict（单价格线）：角色由调用方按来源传入（低点=support、高点=resistance）。"""
    return {
        "kind": role,
        "price": float(price),
        "touches": int(touches),
        "role": role,
        "pattern_hits": int(pattern_hits),  # 成员中出现方向匹配 12 金K 的触及次数
    }


def _pattern_hit_idxs(swings: list[tuple], klines: list) -> set[int]:
    """摆动点索引集合：该次触及当根及确认窗内出现方向匹配 12 金K（低点配看涨/高点配看跌）。

    检查摆动点当根及其后 PATTERN_CONFIRM_WINDOW 根（星形三根K线在极值后完成）。
    只查已收盘 K 线（len(klines)-2 及以前）。
    """
    hits: set[int] = set()
    last_closed = len(klines) - 2
    for idx, kind, _ in swings:
        want = "bullish" if kind == "L" else "bearish"
        for j in range(int(idx), min(int(idx) + PATTERN_CONFIRM_WINDOW, last_closed) + 1):
            if any(
                p["direction"] == want and p["pattern"] in GOLDEN_12
                for p in candlestick.detect_all_patterns(klines, idx=j)
            ):
                hits.add(int(idx))
                break
    return hits


def _aggregate_lines(
    points: list[tuple[float, int]], merge_thr: float, hits: set[int] = frozenset(),
) -> list[tuple[float, int, int, int]]:
    """按价格链式聚合为线：候选点与组内最后一点价距 ≤ merge_thr 则并入（均值价）。

    返回 [(均值价, touches, pattern_hits, newest_idx)]——newest_idx=组内最大 bar 索引，
    供"每侧保留最近 N 条"按时间截断。不做任何权重/门控过滤。
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    groups: list[list[tuple[float, int]]] = [[pts[0]]]
    for p in pts[1:]:
        if abs(p[0] - groups[-1][-1][0]) / groups[-1][-1][0] <= merge_thr:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [
        (
            sum(p[0] for p in g) / len(g),
            len(g),
            sum(1 for _, idx in g if idx in hits),
            max(idx for _, idx in g),
        )
        for g in groups
    ]


def compute_key_levels_from_swings(
    swings: list[tuple],
    closes: np.ndarray,
    n_closed: int,
    config: dict,
    klines: Optional[list] = None,
) -> list[dict]:
    """计算关键位线列表。

    swings: merge_swings 输出 [(idx, "H"|"L", price), ...]
    klines: 传入时启用形态确认计数（pattern_hits），否则为 0
    """
    highs_seq = [p for p in swings if p[1] == "H"]
    lows_seq = [p for p in swings if p[1] == "L"]
    if not highs_seq or not lows_seq:
        return []

    merge_thr = config.get("level_merge_threshold", 0.005)
    hits = _pattern_hit_idxs(swings, klines) if klines else set()

    levels: list[dict] = []
    for seq, role in ((highs_seq, "resistance"), (lows_seq, "support")):
        # 全部摆动点（含最新点）统一链式聚合——价格相近的线融合为一条；
        # 聚合保证相邻组均值价间距 > merge_thr，图上不会出现贴脸双线
        lines = _aggregate_lines(
            [(float(p[2]), int(p[0])) for p in seq], merge_thr, hits,
        )
        lines.sort(key=lambda x: x[3], reverse=True)  # 时间新→旧，截前 N 条
        for price, touches, hit, _idx in lines[:LEVELS_PER_SIDE]:
            levels.append(_make_level(price, role, touches, hit))
    levels.sort(key=lambda x: x["price"])
    return levels


def compute_key_levels(klines: list, config: dict) -> list[dict]:
    """全站统一入口（图表端点 / 信号检测 / AI 事实包 / 风控锚定共用）。

    内部完成摆动点 → 聚合线全链路；调用方给 klines 与
    {swing_order, level_merge_threshold} 即可。输出为单价格线（无区域概念）。
    """
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(klines)
    if n < config.get("min_klines", 30):
        return []
    order = config.get("swing_order", 3)
    high_idx, low_idx = find_swing_points(closes, closes, order)
    swings = merge_swings(high_idx, low_idx, closes, closes)
    if len(swings) < 4:
        return []
    return compute_key_levels_from_swings(swings, closes, n - 1, config, klines)


def _level_side(lv: dict, prev_close: float) -> str:
    """位在前收盘时的侧向：support-like（位在前收下方/持平）或 resistance-like。

    突破与触位的判定都需要"这一根K线之前价格在位的哪一侧"作上下文——
    收盘在支撑位上方是常态，只有从前收上方跌穿容差带才是破位。
    """
    return "support" if prev_close >= lv["price"] else "resistance"


def find_touching_level(
    levels: list[dict], kline: list, prev_close: float, tol: float = DEFAULT_TOL,
) -> dict | None:
    """找出最新已收盘 K 线触及的关键位（持住侧规则）。

    触及 = 影线与容差带 [price×(1−tol), price×(1+tol)] 重叠 且 收盘在"持住侧"
    （support-like 位要求收盘 ≥ 带下沿，resistance-like 要求收盘 ≤ 带上沿）。
    收盘越过远侧（破位方向）不属于触及——那归 find_broken_level 的放量门控管。
    同分取距价格线最近。
    kline: [open_time, open, high, low, close, ...]
    """
    h, l, c = float(kline[2]), float(kline[3]), float(kline[4])
    best: tuple[tuple, dict] | None = None
    for lv in levels:
        price = float(lv["price"])
        lo, hi = price * (1 - tol), price * (1 + tol)
        if not (l <= hi and h >= lo):
            continue  # 影线未与容差带重叠
        if _level_side(lv, prev_close) == "support":
            if c < lo:
                continue  # 收盘跌穿容差带：破位，不是触及
        else:
            if c > hi:
                continue  # 收盘涨破容差带：突破，不是触及
        dist = abs(c - price) / price if price > 0 else 1e9
        if best is None or dist < best[0]:
            best = (dist, lv)
    return best[1] if best else None


def find_broken_level(
    levels: list[dict], kline: list, prev_close: float, tol: float = DEFAULT_TOL,
) -> Optional[dict]:
    """找出最新已收盘 K 线放量突破的关键位（破位距离最大者）。

    突破 = 收盘越过容差带：resistance-like 位要求收盘 > price×(1+tol)（向上突破），
    support-like 位要求收盘 < price×(1−tol)（向下破位）。pct 从价格线起算。
    返回 None 表示本根无突破。
    kline: [open_time, open, high, low, close, ...]
    """
    c = float(kline[4])
    best: tuple[float, dict, str] | None = None
    for lv in levels:
        price = float(lv["price"])
        side = _level_side(lv, prev_close)
        if side == "resistance" and c > price * (1 + tol):
            edge, direction = price, "up"
        elif side == "support" and c < price * (1 - tol):
            edge, direction = price, "down"
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
