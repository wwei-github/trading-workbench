"""入场触发器检测（指标驱动筛选管线，2026-09-17；两段式回踩确认 2026-09-25 docs/19）

方向由技术指标多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1 trigger_breakout：收盘突破 Donchian 通道（BREAKOUT_CHANNEL_BARS 根高低轨）
- T2 trigger_candlestick：信号K线（klines[-2]）出现 GOLDEN_12 形态且方向一致
- T3 trigger_pullback_confirm：回踩 EMA21 + 同向金K 两段式确认——
  当根同时触碰+金K（当根确认），或上根触碰（pending）+ 本根金K（次根确认）；
  仅触碰无金K 不再当根触发，转入 pending 待确认池（scanner 持久化）

优先级（决策矩阵，docs/19 §4.1）：T1 > 当根确认 > 次根确认 > 纯金K(T2) >
触碰转 pending；pending 仅在"恰好下一根K线"有效（开盘时间精确比对）。

position 语义沿用旧约定：触发所锚定的位角色——向上突破/反抽受阻=resistance、
看涨形态/回踩企稳=support。
"""
from datetime import datetime
from typing import Optional, Tuple

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
PULLBACK = "trigger_pullback"          # 历史类型：两段式上线后不再产生新记录
PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩EMA21后金K确认（当根/次根）


def _detect_touch(klines: list[list], direction: int) -> Optional[float]:
    """回踩 EMA21 企稳检测（EMA21 现算，不依赖 analyze_ema 三线）。

    多头=下影触及且收盘收回线上（low ≤ EMA21 ≤ close），空头对称
    （close ≤ EMA21 ≤ high）。命中返回触碰时的 EMA21 值，未命中 None。
    """
    closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
    e21 = calc_ema(closes, 21)
    if e21 is None:
        return None
    ema21 = float(e21[-1])
    if ema21 <= 0 or not np.isfinite(ema21):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and low_last <= ema21 <= close_last:
        return ema21
    if direction == -1 and close_last <= ema21 <= high_last:
        return ema21
    return None


def _close_op(pending: Optional[dict], direction: int, supersede_reason: str) -> Optional[dict]:
    """pending 终态处置（行1/行2 路径）：方向反转一律 expire(direction_flip)——
    死因是票崩而非被抢先，审计语义优先；同方向被更强触发（T1 突破/当根确认）
    抢先 → supersede。"""
    if not pending:
        return None
    if pending.get("direction") != direction:
        return {"op": "expire", "reason": "direction_flip"}
    return {"op": "supersede", "reason": supersede_reason}


def detect_trigger(
    klines: list[list], direction: int,
    pending: Optional[dict] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """检测与候选方向一致的入场触发（两段式决策矩阵，docs/19 §4.1）。

    direction: 1 多 / -1 空（指标多数决的符号）
    pending: 该币活跃 pending 快照（{id, direction, stage1_candle_open}）或 None
    返回 (trigger, pending_op) 二元组：
    - trigger: 命中的触发 dict（type/direction/position/pattern/breakout_pct/reason）
      或 None（无触发 → 观察池）
    - pending_op: 本轮对活跃 pending 的处置，None 或
      {"op": "supersede"|"confirm"|"expire"|"upsert", ...}
      ——upsert/expire 后续在 scanner 侧结合 DB 行补全（方向反转拆两步、
      quality/votes 快照由 scanner 填充），trigger.py 保持无 DB 依赖。

    信号判定全部基于已收盘K线（klines[-2]）；pending 有效期 = 其 stage1_candle_open
    恰为 klines[-3] 的开盘时间（上一根已收盘K线），免疫扫描时刻漂移与轮次缺失。
    """
    n = len(klines)
    if n < 30 or direction not in (1, -1):
        return None, None
    close_last = float(klines[-2][4])
    wanted = "bullish" if direction == 1 else "bearish"

    # pending 有效期判定：Stage-1 锚点必须是上一根已收盘K线（klines[-3]）
    pending_valid = False
    if pending:
        try:
            anchor = datetime.utcfromtimestamp(float(klines[-3][0]) / 1000)
            pending_valid = pending.get("stage1_candle_open") == anchor
        except (IndexError, TypeError, ValueError, OSError):
            pending_valid = False

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
            }, _close_op(pending, direction, "breakout")
        if direction == -1 and close_last < chan_low:
            return {
                "type": BREAKOUT, "direction": direction, "position": "support",
                "pattern": None,
                "breakout_pct": (close_last - chan_low) / chan_low * 100,
                "reason": f"指标同向·收盘跌破{chan_n}根Donchian下轨({chan_low:.6g})",
            }, _close_op(pending, direction, "breakout")

    # 并行计算两个确认证据：当根同向金K + 回踩EMA21触碰
    patterns = detect_all_patterns(klines, idx=-2)
    golden = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    golden = golden if (golden and golden["direction"] == wanted) else None
    touch_ema = _detect_touch(klines, direction)

    # 行2：当根触碰+金K → 当根确认（比纯形态更强，pending 一律被抢先）
    if touch_ema is not None and golden:
        return {
            "type": PULLBACK_CONFIRM, "direction": direction,
            "position": "support" if direction == 1 else "resistance",
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·回踩EMA21({touch_ema:.6g})企稳且当根"
                      f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
        }, _close_op(pending, direction, "same_candle_confirm")

    # 行3/行4：未触碰但有金K → 有效 pending 则次根确认，否则纯金K（T2 原样）
    if touch_ema is None and golden:
        if pending_valid and pending and pending.get("direction") == direction:
            return {
                "type": PULLBACK_CONFIRM, "direction": direction,
                "position": "support" if direction == 1 else "resistance",
                "pattern": golden["pattern"], "breakout_pct": 0.0,
                "reason": f"指标同向·上根回踩EMA21企稳·本根"
                          f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
            }, {"op": "confirm"}
        return {
            "type": CANDLESTICK, "direction": direction,
            "position": "support" if direction == 1 else "resistance",
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}",
        }, ({"op": "expire",
             "reason": "direction_flip" if pending.get("direction") != direction else "window_expired"}
            if pending else None)

    # 行5：触碰但无金K → 不触发，转 pending 待确认（scanner 侧 upsert）
    if touch_ema is not None:
        return None, {
            "op": "upsert", "direction": direction,
            "stage1_candle_open": datetime.utcfromtimestamp(float(klines[-2][0]) / 1000),
            "ema21": touch_ema,
        }

    # 行6/行7：未触碰无金K → 有效 pending 窗口到期；无效 pending 也顺手终态（错过即过期）
    if pending:
        reason = "direction_flip" if pending.get("direction") != direction else "window_expired"
        return None, {"op": "expire", "reason": reason}
    return None, None
