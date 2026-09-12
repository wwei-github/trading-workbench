"""策略统一入口：关键位 + 12金K 统一过滤（见 docs/03-关键位筛选重构需求.md §5.2）

流程：
1. 摆动点（收盘价）→ 结构分类（上涨/下跌/反转/震荡/未分类）
2. 计算关键位（只分支撑位/压力位两类，kind 由角色推导）
3. 放量突破优先：收盘越过整个关键位区域 + 量能 ≥1.2×均量 + EMA 同向 → breakout 信号
   （不要求形态；前收盘上下文区分"突破"与"常态居位"）
4. 触位：最新已收盘 K 线持住侧触及关键位区域 → position
5. 该 K 线出现 12 金K？→ pattern（方向需与关键位角色匹配）
6. EMA 均线形态严格门控：仅认可多头/空头排列、金叉/死叉 4 态，且方向须与信号一致
7. 输出信号

方向铁律（docs/03 §8）：只在支撑位做多、只在压力位做空，唯一例外是放量突破。
"""
from __future__ import annotations

import numpy as np
from typing import Optional

from app.services.strategy import candlestick
from app.services.strategy.ema import analyze_ema
from app.services.strategy.candlestick import GOLDEN_12, PATTERN_LABEL_MAP
from app.services.strategy.key_levels import (
    BREAKOUT_VOL_RATIO,
    compute_key_levels,
    find_broken_level,
    find_touching_level,
    fmt_price,
    volume_ratio,
)
from app.services.strategy.structure import classify_structure
from app.services.strategy.swing import find_swing_points, merge_swings
from app.services.strategy.types import (
    BREAKOUT,
    POSITION_LABEL_MAP,
    LABEL_MAP,
    TREND_REVERSAL,
)

__all__ = [
    "detect_all_signals",
    "detect_any_signal",
    "recent_swings",
    "compute_signal_key_levels",
    "GOLDEN_12",
    "LABEL_MAP",
]


def compute_signal_key_levels(klines: list[list], config: dict) -> list[dict]:
    """分析时刻的关键位快照（手动搜索 / 定时 AI 分析的事实包共用）。

    与 _detect 前两步同源：摆动点 → 结构分类 → 关键位（含 ATR 自适应区域与时间加权），
    但不做触及/形态/EMA 门控——AI 需要全量关键位而非仅信号命中位。
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
    structure = classify_structure(swings, closes, n - 1, config)
    return compute_key_levels(swings, closes, n - 1, structure["signal_type"], config, klines)


# EMA 状态 → 趋势偏向
EMA_BIAS = {
    "bullish_align": "bullish",
    "bullish_cross": "bullish",
    "bearish_align": "bearish",
    "bearish_cross": "bearish",
}

# 严格筛选（用户规则）：仅认可 4 种趋势态——拐头向上/向下、纠缠、未知一律不出信号
EMA_ALLOWED = {"bullish_align", "bearish_align", "bullish_cross", "bearish_cross"}


def recent_swings(klines: list[list], order: int = 3, n: int = 2) -> dict:
    """最近 n 个已确认摆动高点/低点，附 HH/LH/HL/LL 结构分类。

    与 _detect 同源（收盘价摆动点，两侧各 order 根确认）。
    返回: {"highs": [{"label","price","bars_ago"}...新→旧], "lows": [...]}
    分类规则与 Pine 结构标签一致：高点 HH(高于前高点)/LH(低于)，无前参照为 H；
    低点 HL(高于前低点)/LL(低于)，无前参照为 L。bars_ago 以最新一根（含未收盘）为 0。
    """
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    if len(closes) < 2 * order + 1:
        return {"highs": [], "lows": []}
    high_idx, low_idx = find_swing_points(closes, closes, order)
    swings = merge_swings(high_idx, low_idx, closes, closes)
    n_bars = len(klines)

    def _build(seq: list[tuple[int, str, float]], kind: str) -> list[dict]:
        out: list[dict] = []
        for i in range(len(seq) - 1, -1, -1):  # 新 → 旧
            if len(out) >= n:
                break
            idx, _, price = seq[i]
            prev = seq[i - 1][2] if i > 0 else None
            if kind == "H":
                label = "H" if prev is None else ("HH" if price > prev else "LH")
            else:
                label = "L" if prev is None else ("HL" if price > prev else "LL")
            out.append({"label": label, "price": price, "bars_ago": n_bars - 1 - idx})
        return out

    return {
        "highs": _build([s for s in swings if s[1] == "H"], "H"),
        "lows": _build([s for s in swings if s[1] == "L"], "L"),
    }


def detect_all_signals(klines: list[list], config: dict) -> list[dict]:
    """统一过滤：关键位 + 12金K。返回命中的信号列表（0 或 1 个）"""
    try:
        sig = _detect(klines, config)
    except Exception:
        return []
    return [sig] if sig else []


def detect_any_signal(klines: list[list], config: dict) -> Optional[dict]:
    """兼容旧接口：返回首个命中信号或 None"""
    signals = detect_all_signals(klines, config)
    return signals[0] if signals else None


def _detect(klines: list[list], config: dict) -> Optional[dict]:
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    n = len(klines)
    if n < config.get("min_klines", 30):
        return None

    # 1. 摆动点（收盘价）+ 结构分类
    order = config.get("swing_order", 3)
    high_idx, low_idx = find_swing_points(closes, closes, order)
    swings = merge_swings(high_idx, low_idx, closes, closes)
    if len(swings) < 4:
        return None

    n_closed = n - 1
    close_last = float(closes[-2])

    structure = classify_structure(swings, closes, n_closed, config)
    signal_type = structure["signal_type"]

    # 2. 计算关键位（传 klines 启用 ATR 自适应区域半宽）
    levels = compute_key_levels(swings, closes, n_closed, signal_type, config, klines)
    if not levels:
        return None

    # EMA 门控共用（触位与突破两条路径都需要）：
    # a) 仅 4 种趋势态可出信号（拐头/纠缠/未知 → 过滤）
    # b) 方向合理性：EMA 偏向必须与信号方向一致
    ema = config.get("ema")
    if ema is None:
        ema = analyze_ema(klines)
    ema_state = ema["state"] if ema else None

    # 3. 放量突破优先：收盘越过整个关键位区域（涨破压力/跌破支撑）且量能达标
    #    且 EMA 与突破方向同向 → 发突破信号（不要求 K 线形态）。
    #    前收盘上下文区分"突破"与"常态居位"：价格本来就高于支撑位不算突破。
    #    门控不过则该位作废，落到触位路径（破位位因持住侧检查不会再被当触及）
    prev_close = float(closes[-3]) if n >= 3 else close_last
    broken = find_broken_level(levels, klines[-2], prev_close)
    if broken is not None:
        ratio = volume_ratio(klines)
        wanted = "bullish" if broken["direction"] == "up" else "bearish"
        if (
            ratio >= BREAKOUT_VOL_RATIO
            and ema_state in EMA_ALLOWED
            and EMA_BIAS[ema_state] == wanted
        ):
            blv = broken["level"]
            up = broken["direction"] == "up"
            edge = blv["zone_high"] if up else blv["zone_low"]
            strength = 0.8 if ratio >= 2.0 else 0.7  # 倍量再加档
            if blv.get("touches", 1) >= 2:
                strength += 0.1
            strength = min(strength + 0.1, 1.0)  # EMA 同向
            return {
                "signal_type": BREAKOUT,
                "position": blv["kind"],
                "current_price": close_last,
                "breakout_pct": float(broken["pct"]),  # 越过区域边缘的幅度（带符号）
                "trend_slope": 0.0,
                "r_squared": 0.0,
                "pattern": None,
                "pattern_direction": wanted,
                "signal_reason": (
                    f"放量突破{'压力' if up else '支撑'}区域({fmt_price(edge)})·"
                    f"{ratio:.1f}×均量"
                ),
                "ema_state": ema_state,
                "strength": strength,
                "key_levels": levels,
                "hit_level": blv,
                "reversal": structure.get("reversal"),
            }

    # 4. 触位路径：最新已收盘 K 线（klines[-2]）持住侧触及关键位区域
    hit = find_touching_level(levels, klines[-2], prev_close)
    if hit is None:
        return None

    # 5. 12 金K + 方向匹配（支撑位→看涨形态，压力位→看跌形态）
    patterns = candlestick.detect_all_patterns(klines, idx=-2)
    wanted = "bullish" if hit["role"] == "support" else "bearish"
    pattern = next(
        (p for p in patterns if p["direction"] == wanted and p["pattern"] in GOLDEN_12),
        None,
    )
    if pattern is None:
        return None

    # 6. EMA 严格门控（权重高于单根 K 线形态，ema_state 已在突破分支前算好）：
    #    a) 仅 4 种趋势态可出信号（拐头/纠缠/未知 → 过滤）
    #    b) 位置合理性：EMA 偏向必须与位置方向一致——空头排列/死叉下触及支撑位无效，
    #       多头排列/金叉下触及压力位无效
    if ema_state not in EMA_ALLOWED:
        return None
    if EMA_BIAS[ema_state] != wanted:
        return None

    # 7. 组装信号
    position = hit["kind"]
    position_label = POSITION_LABEL_MAP.get(position, position)
    role_label = "支撑" if hit["role"] == "support" else "压力"
    deviation = (close_last - hit["price"]) / hit["price"] * 100
    reason = (
        f"{position_label}({fmt_price(hit['price'])})·{role_label} + "
        f"{PATTERN_LABEL_MAP.get(pattern['pattern'], pattern['pattern'])}"
    )

    strength = pattern["strength"]
    if hit["touches"] >= 2:
        strength += 0.1  # 多次触及的关键位更可靠
    if signal_type == TREND_REVERSAL:
        strength += 0.1  # 反转结构加权
    strength += 0.1  # 均线形态与信号同向（严格门控后必然同向）
    strength = min(strength, 1.0)

    return {
        "signal_type": signal_type,
        "position": position,
        "current_price": close_last,
        "breakout_pct": float(deviation),  # 距关键位中心的偏离（带符号）
        "trend_slope": 0.0,
        "r_squared": 0.0,
        "pattern": pattern["pattern"],
        "pattern_direction": pattern["direction"],
        "signal_reason": reason,
        "ema_state": ema_state,
        "strength": strength,
        "key_levels": levels,
        "hit_level": hit,
        "reversal": structure.get("reversal"),
    }
