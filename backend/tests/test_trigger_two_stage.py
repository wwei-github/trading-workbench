"""两段式回踩确认决策矩阵单测（docs/19 §4.1 / §10.1，CI 一票否决）

覆盖 trigger.detect_trigger 的 7 行矩阵 + 前置过期 + pending 有效期开盘时间
比对边界。形态检测打桩（矩阵逻辑与形态识别解耦）；触碰判定用真实 EMA21。
"""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.ema import calc_ema  # noqa: E402

TS0 = 1716000000000  # ms
STEP = 3_600_000     # 1h
CHAN = 20            # BREAKOUT_CHANNEL_BARS 默认窗口


def _klines(closes: list[float], highs: list[float], lows: list[float]) -> list[list]:
    """7 列 K 线：[ts, open, high, low, close, vol, quote_vol]；open=前收，末根未收盘"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append([TS0 + i * STEP, prev, highs[i], lows[i], c, 100.0, 1e6])
        prev = c
    return out


def _base(n: int = 60, base: float = 100.0) -> tuple[list, list, list]:
    """横盘微扰序列：最后收盘不创新高（后续按需显式构造突破/触碰）"""
    closes = [base + 0.01 * (i % 5) for i in range(n)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    return closes, highs, lows


def _kl(closes, highs, lows) -> list[list]:
    return _klines(closes, highs, lows)


def _ema21_of(kl: list[list]) -> float:
    e = calc_ema([float(k[4]) for k in kl[:-1]], 21)
    assert e is not None
    return float(e[-1])


def _set_signal_candle(kl: list[list], close: float, high: float, low: float) -> None:
    """重写信号K线（klines[-2]，已收盘）；open 沿用前收约定"""
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, kl[-3][4], high, low, close, vol, qv]


def _no_touch_long(kl: list[list]) -> None:
    """信号K线整体悬于 EMA21 上方（low > ema）且不破上轨"""
    ema = _ema21_of(kl)
    c = ema + 0.2
    _set_signal_candle(kl, c, c + 0.05, c - 0.05)


def _touch_long(kl: list[list]) -> None:
    """下影触及 EMA21、收盘收回线上（low == ema ≤ close）"""
    ema = _ema21_of(kl)
    c = ema + 0.2
    _set_signal_candle(kl, c, c + 0.1, ema)


def _breakout_long(kl: list[list]) -> None:
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    c = chan_high + 0.5
    _set_signal_candle(kl, c, c + 0.1, c - 0.05)


def _anchor(kl: list[list], idx: int) -> datetime:
    return datetime.utcfromtimestamp(kl[idx][0] / 1000)


def _pending(kl: list[list], direction: int = 1, anchor_idx: int = -3) -> dict:
    return {"id": "x", "direction": direction, "stage1_candle_open": _anchor(kl, anchor_idx)}


GOLDEN_LONG = [{"pattern": "hammer", "direction": "bullish"}]
GOLDEN_SHORT = [{"pattern": "hanging_man", "direction": "bearish"}]


# ── 行7：无触碰无金K无 pending → 观察池 ────────────────────────────────────

def test_row7_no_trigger_no_pending():
    kl = _kl(*_base())
    _no_touch_long(kl)
    t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


# ── 行6：有效 pending + 无触碰无金K → 窗口到期 ─────────────────────────────

def test_row6_valid_pending_expires():
    kl = _kl(*_base())
    _no_touch_long(kl)
    t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-3))
    assert t is None
    assert op == {"op": "expire", "reason": "window_expired"}


def test_row6_direction_flip_expires_with_flip_reason():
    kl = _kl(*_base())
    _no_touch_long(kl)
    t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1, anchor_idx=-3))
    assert t is None
    assert op == {"op": "expire", "reason": "direction_flip"}


# ── 行1：Donchian 突破优先，pending → superseded ───────────────────────────

def test_row1_breakout_supersedes_pending():
    kl = _kl(*_base())
    _breakout_long(kl)
    t, op = trig.detect_trigger(kl, 1, pending=_pending(kl))
    assert t is not None and t["type"] == trig.BREAKOUT
    assert op == {"op": "supersede", "reason": "breakout"}


def test_row1_breakout_without_pending_no_op():
    kl = _kl(*_base())
    _breakout_long(kl)
    t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT
    assert op is None


def test_row1_breakout_with_flipped_pending_expires_as_flip():
    """方向反转的 pending 在任何强触发路径下死因都是 direction_flip（非 supersede）"""
    kl = _kl(*_base())
    _breakout_long(kl)
    t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1))
    assert t is not None and t["type"] == trig.BREAKOUT
    assert op == {"op": "expire", "reason": "direction_flip"}


# ── 行2：当根触碰+金K → 当根确认，pending → superseded ────────────────────

def test_row2_touch_and_golden_same_candle_confirm():
    kl = _kl(*_base())
    _touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["position"] == "support" and t["pattern"] == "hammer"
    assert "当根" in t["reason"]
    assert op == {"op": "supersede", "reason": "same_candle_confirm"}


def test_row2_same_candle_confirm_with_flipped_pending_expires_as_flip():
    kl = _kl(*_base())
    _touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert op == {"op": "expire", "reason": "direction_flip"}


# ── 行3：有效 pending + 本根金K未触碰 → 次根确认 ───────────────────────────

def test_row3_next_candle_confirm():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-3))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["pattern"] == "hammer"
    assert op == {"op": "confirm"}


def test_row3_pending_stale_falls_to_candlestick_and_expires():
    """pending 锚点不是上一根（隔轮）→ 失效：金K走 T2 原样，pending 过期"""
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-4))
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert op == {"op": "expire", "reason": "window_expired"}


def test_row3_pending_direction_flip_falls_to_candlestick():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1, anchor_idx=-3))
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert op == {"op": "expire", "reason": "direction_flip"}


# ── 行4：金K无触碰无 pending → T2 原样 ─────────────────────────────────────

def test_row4_golden_only_candlestick():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert op is None


def test_row4_wrong_direction_golden_ignored():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


# ── 行5：触碰无金K → 不触发，转 pending upsert ─────────────────────────────

def test_row5_touch_without_golden_upserts_pending():
    kl = _kl(*_base())
    _touch_long(kl)
    t, op = trig.detect_trigger(kl, 1)
    assert t is None
    assert op is not None and op["op"] == "upsert"
    assert op["direction"] == 1
    assert op["stage1_candle_open"] == _anchor(kl, -2)  # 锚点 = 信号K线开盘时间
    assert op["ema21"] > 0


def test_row5_touch_refresh_keeps_pending_flow():
    """已有 pending 再触碰：仍返回 upsert（scanner 决定刷新或反转重建）"""
    kl = _kl(*_base())
    _touch_long(kl)
    t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-2))
    assert t is None and op["op"] == "upsert"


# ── 有效期边界：锚点恰为 klines[-3] 才有效 ─────────────────────────────────

def test_validity_boundary_exactly_previous_candle():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        for idx in (-2, -4):
            t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=idx))
            assert t is not None and t["type"] == trig.CANDLESTICK, f"anchor_idx={idx} 应失效"
            assert op == {"op": "expire", "reason": "window_expired"}


# ── 空头对称：反抽受阻 + 当根金K → 当根确认；无 pending 无 op ─────────────

def test_short_symmetric_touch_and_confirm():
    base = 100.0
    closes = [base - 0.01 * (i % 5) for i in range(60)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    kl = _klines(closes, highs, lows)
    ema = _ema21_of(kl)
    # 上影反抽至 EMA21 受阻、收盘收回线下（close ≤ ema == high）
    c = ema - 0.2
    _set_signal_candle(kl, c, ema, c - 0.1)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(kl, -1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["position"] == "resistance"
    assert "当根" in t["reason"]
    assert op is None


def test_short_symmetric_touch_without_golden_upserts():
    base = 100.0
    closes = [base - 0.01 * (i % 5) for i in range(60)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    kl = _klines(closes, highs, lows)
    ema = _ema21_of(kl)
    c = ema - 0.2
    _set_signal_candle(kl, c, ema, c - 0.1)
    t, op = trig.detect_trigger(kl, -1)
    assert t is None and op["op"] == "upsert" and op["direction"] == -1
