"""入场触发器检测（指标驱动筛选管线，2026-09-17）

方向由技术指标多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1 trigger_breakout：收盘突破 Donchian 通道（BREAKOUT_CHANNEL_BARS 根高低轨）
- T2 trigger_candlestick：信号K线（klines[-2]）出现 GOLDEN_12 形态且方向一致
- T3 trigger_pullback：回踩 EMA21 企稳——多头=下影触及且收盘收回线上，空头对称

优先级 T1 > T2 > T3（突破事件最新鲜、形态次之、回踩是低吸等待型）；
无触发返回 None → 候选留在观察池（不落库，下轮扫描全量重算）。

position 语义沿用旧约定：触发所锚定的位角色——向上突破/反抽受阻=resistance、
看涨形态/回踩企稳=support。
"""
from typing import Optional

from app.config import settings
from app.services.strategy.candlestick import (
    GOLDEN_12,
    PATTERN_LABEL_MAP,
    detect_all_patterns,
)
from app.services.strategy.ema import calc_ema
import numpy as np

# 触发类型 = signal_type（AI 提示词 SIGNAL_TYPE_LABELS 已注册同名标签）
BREAKOUT = "trigger_breakout"
CANDLESTICK = "trigger_candlestick"
PULLBACK = "trigger_pullback"


def detect_trigger(
    klines: list[list], direction: int,
) -> Optional[dict]:
    """检测与候选方向一致的入场触发（基于已收盘K线，klines[-1] 未收盘）。

    direction: 1 多 / -1 空（指标多数决的符号）
    返回 {type, direction, position, pattern, breakout_pct, reason} 或 None。
    """
    n = len(klines)
    if n < 30 or direction not in (1, -1):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    wanted = "bullish" if direction == 1 else "bearish"

    # T1 Donchian 突破（窗口不含信号K线自身与未收盘根，防"自己破自己"）
    chan_n = settings.BREAKOUT_CHANNEL_BARS
    if n >= chan_n + 2:
        window = klines[-2 - chan_n:-2]
        chan_high = max(float(k[2]) for k in window)
        chan_low = min(float(k[3]) for k in window)
        if direction == 1 and close_last > chan_high:
            return {
                "type": BREAKOUT, "direction": direction, "position": "resistance",
                "pattern": None,
                "breakout_pct": (close_last - chan_high) / chan_high * 100,
                "reason": f"指标同向·收盘突破{chan_n}根Donchian上轨({chan_high:.6g})",
            }
        if direction == -1 and close_last < chan_low:
            return {
                "type": BREAKOUT, "direction": direction, "position": "support",
                "pattern": None,
                "breakout_pct": (close_last - chan_low) / chan_low * 100,
                "reason": f"指标同向·收盘跌破{chan_n}根Donchian下轨({chan_low:.6g})",
            }

    # T2 黄金12形态（信号K线方向必须与候选方向一致）
    patterns = detect_all_patterns(klines, idx=-2)
    pattern = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    if pattern and pattern["direction"] == wanted:
        return {
            "type": CANDLESTICK, "direction": direction,
            "position": "support" if direction == 1 else "resistance",
            "pattern": pattern["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·{PATTERN_LABEL_MAP.get(pattern['pattern'], pattern['pattern'])}",
        }

    # T3 回踩 EMA21 企稳（EMA21 现算，不依赖 analyze_ema 三线）
    closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
    e21 = calc_ema(closes, 21)
    if e21 is not None:
        ema21 = float(e21[-1])
        if ema21 > 0 and np.isfinite(ema21):
            if direction == 1 and low_last <= ema21 <= close_last:
                return {
                    "type": PULLBACK, "direction": direction, "position": "support",
                    "pattern": None, "breakout_pct": 0.0,
                    "reason": f"指标同向·回踩EMA21({ema21:.6g})企稳",
                }
            if direction == -1 and close_last <= ema21 <= high_last:
                return {
                    "type": PULLBACK, "direction": direction, "position": "resistance",
                    "pattern": None, "breakout_pct": 0.0,
                    "reason": f"指标同向·反抽EMA21({ema21:.6g})受阻",
                }

    return None
