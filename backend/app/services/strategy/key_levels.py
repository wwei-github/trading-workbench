"""关键位计算模块（统一口径，2026-09-14）

图表端点、信号检测、AI 事实包、风控锚定共用同一个 compute_key_levels——
Pine Auto S/R 式摆动点阶梯（借鉴 Auto S/R 指标 left=50/right=25 与 quick right=5）：

- quick 位：find_pivots(left=50, right=5) 的最新高点/低点各一档（右确认仅 5 根，覆盖最新结构）
- full 位：find_pivots(left=50, right=25) 每侧最近 CHART_PIVOT_LEVELS_PER_SIDE(3) 档
  （右确认 25 根，大级别位；full 是 quick 的子集）
- 近邻合并：价距 ≤ 2×区域半宽 + level_merge_threshold 的摆动点并入同一位，
  触及池 = quick 摆动点全集——touches 为组内摆动点数、weight 为时间加权触及强度、
  pattern_hits 为组内出现方向匹配 12 金K 的触及数（price 取组内首个选中点，确定性）

关键位是区域：中心价 ± zone_tolerance。区域半宽随波动自适应：
ATR% 越大区域越宽（0.5×ATR），限制在配置值 key_level_tolerance 的 [0.5×, 2×] 倍内
（默认配置 0.3% → 实际区域 0.15%~0.6%）。角色按当前价动态判定：
关键位在当前价上方=压力，下方=支撑（kind == role）。

历史注：此前数据层为"摆动点+时间加权聚类（权重 ≥1.2 门控）+震荡回归边界"，图表层为
Pine 摆动点阶梯且区域减半——两套数字不同曾三次造成理解偏差（SCRUSDT 止盈扎堆、
FLOCKUSDT 锚位、TREEUSDT 粘合），2026-09-14 起统一为本函数（docs/03 §2、docs/07 §2）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.services.strategy import candlestick
from app.services.strategy.candlestick import GOLDEN_12
from app.services.strategy.swing import find_pivots

# Pine 摆动点阶梯参数（原图表层 CHART_PIVOT_*，口径归一后迁入）
PIVOT_LEFT = 50                 # 左确认窗（历史纵深）：只留大级别结构
PIVOT_RIGHT = 25                # full 位右确认窗（大级别位确认滞后 25 根）
PIVOT_QUICK_RIGHT = 5           # quick 位右确认窗（最新结构，滞后仅 5 根）
LEVELS_PER_SIDE = 3             # full 位每侧最多档数

# 触及权重半衰期（已收盘 K 线根数）
TOUCH_HALF_LIFE = 60

# 形态确认加成：触及点出现方向匹配的 12 金K，该次触及权重乘数
PATTERN_TOUCH_BOOST = 1.5
# 形态确认窗（根）：启明星/黄昏星在极值后 1~2 根才完成，摆动点当根 + 后 2 根内命中都算
PATTERN_CONFIRM_WINDOW = 2

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
                weight: Optional[float] = None, pattern_hits: int = 0) -> dict:
    """构造关键位 dict。

    kind 由角色动态推导：位在当前价上方=压力位(resistance)，下方=支撑位(support)
    ——kind == role。
    """
    role = "resistance" if price > close_last else "support"
    lv = {
        "kind": role,
        "price": float(price),
        "zone_low": float(price * (1 - tol)),
        "zone_high": float(price * (1 + tol)),
        "touches": int(touches),
        "role": role,
        "pattern_hits": int(pattern_hits),  # 组内出现方向匹配 12 金K 的触及次数
    }
    if weight is not None:
        lv["weight"] = round(float(weight), 2)  # 时间加权触及强度（组内触及的半衰期加权和）
    return lv


def _touch_weight(idx: int, n_closed: int) -> float:
    """单次触及的时间权重：越近越重，半衰期 TOUCH_HALF_LIFE 根"""
    bars_ago = max(n_closed - int(idx), 0)
    return 2.0 ** (-bars_ago / TOUCH_HALF_LIFE)


def _pattern_boosts(swings: list[tuple], klines: list) -> dict[int, float]:
    """摆动点索引 → 该次触及的权重乘数：触及点附近出现方向匹配的 12 金K 时 >1。

    低点配看涨形态、高点配看跌形态（形态与位角色共振才算确认）；
    检查摆动点当根及其后 PATTERN_CONFIRM_WINDOW 根（星形三根K线在极值后完成）。
    只查已收盘 K 线（len(klines)-2 及以前）。
    """
    boosts: dict[int, float] = {}
    last_closed = len(klines) - 2
    for idx, kind, _ in swings:
        want = "bullish" if kind == "L" else "bearish"
        for j in range(int(idx), min(int(idx) + PATTERN_CONFIRM_WINDOW, last_closed) + 1):
            if any(
                p["direction"] == want and p["pattern"] in GOLDEN_12
                for p in candlestick.detect_all_patterns(klines, idx=j)
            ):
                boosts[idx] = PATTERN_TOUCH_BOOST
                break
    return boosts


def compute_key_levels(klines: list, config: dict) -> list[dict]:
    """统一关键位（图表端点 / 信号检测 / AI 事实包 / 风控锚定共用）。

    选位与近邻合并规则见模块 docstring。config 需含：
    - key_level_tolerance：区域半宽基准（默认 0.003）
    - level_merge_threshold：近邻合并外加间距（默认 0.005）
    """
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(klines)
    if n < PIVOT_LEFT + PIVOT_QUICK_RIGHT + 1:
        return []
    close_last = float(closes[-2]) if n >= 2 else float(closes[-1])
    if close_last <= 0:
        return []

    base_tol = float(config.get("key_level_tolerance", 0.003))
    merge_thr = float(config.get("level_merge_threshold", 0.005))

    # 区域半宽自适应：0.5×ATR，限制在配置容忍度的 [0.5×, 2×] 内（与风控锚定同一校准）
    tol = base_tol
    atr = calc_atr(klines)
    if atr and close_last > 0:
        tol = min(max(0.5 * atr / close_last, base_tol * 0.5), base_tol * 2)
    near_thr = 2 * tol + merge_thr

    quick_h, quick_l = find_pivots(closes, PIVOT_LEFT, PIVOT_QUICK_RIGHT)
    full_h, full_l = find_pivots(closes, PIVOT_LEFT, PIVOT_RIGHT)

    # 选位顺序与旧图表层一致：quick 最新高/低点优先，再 full 每侧最近 N 档；
    # _group 改造自旧 _add 去重——命中阈值不丢弃而是并入最近的组（价取组内首个选中点）
    groups: list[dict] = []  # {price, members: [(idx, "H"|"L")]}

    def _group(idx: int) -> None:
        price = float(closes[idx])
        best: tuple[float, dict] | None = None
        for g in groups:
            dist = abs(price - g["price"]) / g["price"] if g["price"] > 0 else 1e9
            if dist <= near_thr and (best is None or dist < best[0]):
                best = (dist, g)
        if best is None:
            groups.append({"price": price, "members": [(int(idx), None)]})
        elif all(int(idx) != m[0] for m in best[1]["members"]):
            best[1]["members"].append((int(idx), None))

    if quick_h:
        _group(quick_h[-1])
    if quick_l:
        _group(quick_l[-1])
    for idx in full_h[-LEVELS_PER_SIDE:]:
        _group(idx)
    for idx in full_l[-LEVELS_PER_SIDE:]:
        _group(idx)
    if not groups:
        return []

    # 触及池补扫（口径 b）：quick 全集（full ⊆ quick）中价距落入某组阈值内的摆动点
    # 都计入该组 touches/weight/pattern_hits——恢复"多次触及"的真实密度
    qh_set = set(quick_h)  # 高点索引集合（quick_h 与 quick_l 不相交，可判定极性）

    def _kind_of(idx: int) -> str:
        return "H" if idx in qh_set else "L"

    for idx in list(quick_h) + list(quick_l):
        price = float(closes[idx])
        best: tuple[float, dict] | None = None
        for g in groups:
            dist = abs(price - g["price"]) / g["price"] if g["price"] > 0 else 1e9
            if dist <= near_thr and (best is None or dist < best[0]):
                best = (dist, g)
        if best is not None and all(int(idx) != m[0] for m in best[1]["members"]):
            best[1]["members"].append((int(idx), _kind_of(int(idx))))

    n_closed = n - 1
    levels: list[dict] = []
    for g in groups:
        members = [(idx, kind if kind else ("H" if idx in qh_set else "L"))
                   for idx, kind in g["members"]]
        price_members = [(idx, kind, float(closes[idx])) for idx, kind in members]
        boosts = _pattern_boosts(price_members, klines)
        levels.append(_make_level(
            g["price"],
            touches=len(members),
            close_last=close_last,
            tol=tol,
            weight=sum(_touch_weight(idx, n_closed) for idx, _ in members),
            pattern_hits=sum(1 for idx, _ in members if idx in boosts),
        ))
    levels.sort(key=lambda lv: lv["price"])
    return levels


def _merge_overlapping_levels(levels: list[dict], near_gap: float) -> list[dict]:
    """同角色区域重叠或近于重合（间隔 ≤ near_gap）→ 合并为一个区域。

    【2026-09-14 起新路径不再调用】统一口径的近邻合并已在 compute_key_levels 内完成
    （去重阈值 2×tol+merge_thr 保证区域互不重叠）；函数保留供历史数据修复与测试使用。
    聚类按中心价间距分簇，但区域半宽是 ATR 自适应的——两簇中心距超过阈值时区域仍可能
    相互重叠，人眼是一个位，锚定止盈却会被当两档用（SCRUSDT 案例：0.023166~0.023634 与
    0.023516~0.023992 重叠未合并，止盈一、二扎堆在 0.37% 内）。合并取并集：zone_low=min、
    zone_high=max、中心=并集中点，touches/pattern_hits/weight 累加；role/kind 维持原值不重判
    （同角色合并，并集中点可能因并集偏宽越过前收，重判会与止损/铁律块的角色口径不一致）。
    """
    out: list[dict] = []
    for role in ("support", "resistance"):
        group = sorted(
            (dict(lv) for lv in levels if lv["role"] == role),
            key=lambda x: x["zone_low"],
        )
        merged: list[dict] = []
        for lv in group:
            if merged and lv["zone_low"] <= merged[-1]["zone_high"] * (1 + near_gap):
                g = merged[-1]
                g["zone_low"] = min(g["zone_low"], lv["zone_low"])
                g["zone_high"] = max(g["zone_high"], lv["zone_high"])
                g["price"] = (g["zone_low"] + g["zone_high"]) / 2
                g["touches"] = int(g["touches"]) + int(lv["touches"])
                g["pattern_hits"] = int(g["pattern_hits"]) + int(lv["pattern_hits"])
                g["weight"] = round(
                    float(g.get("weight", 0)) + float(lv.get("weight", 0)), 2
                )
            else:
                merged.append(lv)
        out.extend(merged)
    out.sort(key=lambda x: x["price"])
    return out


def _level_side(lv: dict, prev_close: float) -> str:
    """位在前收盘时的侧向：support-like（位在前收下方/持平）或 resistance-like。

    突破与触位的判定都需要"这一根K线之前价格在位的哪一侧"作上下文——
    收盘在支撑位上方是常态，只有从前收上方跌穿区域才是破位。
    """
    return "support" if prev_close >= lv["price"] else "resistance"


def find_touching_level(levels: list[dict], kline: list, prev_close: float) -> dict | None:
    """找出最新已收盘 K 线触及的关键位（持住侧规则）。

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
