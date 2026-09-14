"""策略统一入口：Donchian 通道突破 + 12金K 形态信号（docs/09，2026-09-14 关键位功能移除）

流程：
1. 摆动点（收盘价）→ 结构分类（上涨/下跌/反转/震荡/未分类，仅作 signal_type 标签）
2. Donchian 通道突破优先：信号K线收盘严格越过前 N 根高低轨 + 量能 ≥1.2×均量 + EMA 同向
   → breakout 信号（不要求形态）
3. 形态路径：该 K 线出现 GOLDEN_12 形态（方向决定 position）+ EMA 严格门控 → 信号
4. 输出信号 dict（无 key_levels/hit_level——关键位功能整体移除；风控锚定改用
   recent_swings 摆动点 + 近端影线极值，见 risk_guard）

信号 position 语义（形态/突破方向派生，不再来自关键位角色）：
看涨形态/向上突破=resistance、看跌形态/向下突破=support。
"""
from __future__ import annotations

import numpy as np
from typing import Optional

from app.config import settings
from app.services.strategy import candlestick
from app.services.strategy.candlestick import GOLDEN_12, PATTERN_LABEL_MAP
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import BREAKOUT_VOL_RATIO, fmt_price, volume_ratio
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
    "recent_swings",
    "GOLDEN_12",
    "LABEL_MAP",
]


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
    """统一过滤：Donchian 突破 + 12金K 形态。返回命中的信号列表（0 或 1 个）"""
    try:
        sig = _detect(klines, config)
    except Exception:
        return []
    return [sig] if sig else []


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

    # EMA 门控（突破与形态两条路径共用）：
    # a) 仅 4 种趋势态可出信号（拐头/纠缠/未知 → 过滤）
    # b) 方向合理性：EMA 偏向必须与信号方向一致
    ema = config.get("ema")
    if ema is None:
        ema = analyze_ema(klines)
    ema_state = ema["state"] if ema else None

    # 2. Donchian 通道突破优先：信号K线收盘严格越过前 N 根已收盘K线的高低轨
    #    （窗口不含信号K线自身、不含未收盘根，防"自己破自己"）+ 量能达标 + EMA 同向。
    #    量能/EMA 不过则落到形态路径（突破K线自身也可能带形态）
    chan_n = int(config.get("breakout_channel_bars", settings.BREAKOUT_CHANNEL_BARS))
    if chan_n > 0 and n >= chan_n + 2:
        window = klines[-2 - chan_n:-2]
        chan_high = max(float(k[2]) for k in window)
        chan_low = min(float(k[3]) for k in window)
        up = close_last > chan_high
        down = close_last < chan_low
        if up or down:
            ratio = volume_ratio(klines)
            wanted = "bullish" if up else "bearish"
            if (
                ratio >= BREAKOUT_VOL_RATIO
                and ema_state in EMA_ALLOWED
                and EMA_BIAS[ema_state] == wanted
            ):
                track = chan_high if up else chan_low
                strength = 0.8 if ratio >= 2.0 else 0.7  # 倍量再加档
                strength = min(strength + 0.1, 1.0)  # EMA 同向
                return {
                    "signal_type": BREAKOUT,
                    # 突破的 position 按突破方向派生（向上=resistance、向下=support）
                    "position": "resistance" if up else "support",
                    "current_price": close_last,
                    "breakout_pct": (close_last - track) / track * 100,  # 越过轨道幅度（带符号）
                    "trend_slope": 0.0,
                    "r_squared": 0.0,
                    "pattern": None,
                    "pattern_direction": wanted,
                    "signal_reason": (
                        f"放量突破{chan_n}根Donchian{'上' if up else '下'}轨"
                        f"({fmt_price(track)})·{ratio:.1f}×均量"
                    ),
                    "ema_state": ema_state,
                    "strength": strength,
                    "reversal": structure.get("reversal"),
                }

    # 3. 形态路径：信号K线（klines[-2]）出现 GOLDEN_12 形态，方向决定 position
    #    （看涨→support 回踩企稳、看跌→resistance 反抽受阻）
    patterns = candlestick.detect_all_patterns(klines, idx=-2)
    pattern = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    if pattern is None:
        return None
    wanted = pattern["direction"]

    # 4. EMA 严格门控（权重高于单根 K 线形态）：
    #    a) 仅 4 种趋势态可出信号（拐头/纠缠/未知 → 过滤）
    #    b) 方向合理性：EMA 偏向必须与形态方向一致——空头排列/死叉下的看涨形态无效
    if ema_state not in EMA_ALLOWED:
        return None
    if EMA_BIAS[ema_state] != wanted:
        return None

    # 5. 组装信号
    position = "support" if wanted == "bullish" else "resistance"
    position_label = POSITION_LABEL_MAP.get(position, position)
    reason = (
        f"{position_label} + "
        f"{PATTERN_LABEL_MAP.get(pattern['pattern'], pattern['pattern'])}"
    )

    strength = pattern["strength"]
    if signal_type == TREND_REVERSAL:
        strength += 0.1  # 反转结构加权
    strength += 0.1  # 均线形态与信号同向（严格门控后必然同向）
    strength = min(strength, 1.0)

    return {
        "signal_type": signal_type,
        "position": position,
        "current_price": close_last,
        "breakout_pct": 0.0,
        "trend_slope": 0.0,
        "r_squared": 0.0,
        "pattern": pattern["pattern"],
        "pattern_direction": pattern["direction"],
        "signal_reason": reason,
        "ema_state": ema_state,
        "strength": strength,
        "reversal": structure.get("reversal"),
    }
