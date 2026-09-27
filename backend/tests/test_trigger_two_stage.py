"""两段式回踩确认决策矩阵单测（docs/19 §4.1 / §10.1，CI 一票否决）

覆盖 trigger.detect_trigger 的 7 行矩阵 + 前置过期 + pending 有效期开盘时间
比对边界。形态检测打桩（矩阵逻辑与形态识别解耦）；触碰判定用真实 EMA21。
"""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.ema import calc_ema  # noqa: E402

TS0 = 1716000000000  # ms
STEP = 3_600_000     # 1h
CHAN = 20            # BREAKOUT_CHANNEL_BARS 默认窗口


@pytest.fixture(autouse=True)
def _pin_breakout_settings(monkeypatch):
    """固定突破门槛与新增上限，免疫本地 .env 覆盖（pydantic-settings 优先级高于默认值）"""
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
    monkeypatch.setattr(settings, "BREAKOUT_CHANNEL_BARS", 20)
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 1.5)
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 1.5)


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
    """光头光脚放量长实体突破K线：收盘高于20根上轨约0.5%，量1.5倍均量"""
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4])
    c = chan_high + 0.5
    ts, _, _, _, _, _, qv = kl[-2]
    kl[-2] = [ts, o, c, o, c, 150.0, qv]


def _breakout_short(kl: list[list]) -> None:
    """空头版：光头光脚放量长实体阴线，收盘低于20根下轨约0.5%

    阴线 c < o：high=open、low=close（与阳线夹具的列序相反，勿混用）
    """
    chan_low = min(float(k[3]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4])
    c = chan_low - 0.5
    ts, _, _, _, _, _, qv = kl[-2]
    kl[-2] = [ts, o, o, c, c, 150.0, qv]


def _set_breakout_candle(kl, *, open_, close, high, low, vol=150.0) -> None:
    """自定义突破K线形态（拒绝用例专用：显式控制实体/影线/量能）"""
    ts, _, _, _, _, _, qv = kl[-2]
    kl[-2] = [ts, open_, high, low, close, vol, qv]


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


# ── 突破三重收紧（2026-09-25）：幅度 / 量能 / 长实体短影线 ──────────────────

def test_row1_breakout_rejected_small_move():
    """毛边突破：收盘仅高于上轨 0.05%（< 0.2% 门槛）→ 不触发；
    K线跨越 EMA21 落入回踩触碰分支 → upsert pending（正确的管线落空行为）"""
    kl = _kl(*_base())
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4])
    c = chan_high * (1 + 0.0005)
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_rejected_low_volume():
    """缩量突破：量比 1.0（< 1.2 门槛）→ 不触发，落空转 pending"""
    kl = _kl(*_base())
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4])
    c = chan_high + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o, vol=100.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_rejected_long_upper_shadow():
    """长上影突破：上影 0.25 > 实体 0.80 的 20% → 不触发（防毛刺假突破），落空转 pending"""
    kl = _kl(*_base())
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4])
    c = chan_high + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c + 0.25, low=o)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_rejected_small_body():
    """短实体突破：实体/全长 0.72 < 0.75 门槛 → 不触发；
    整根悬于 EMA21 上方不构成回踩 → 观察池"""
    kl = _kl(*_base())
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    o = float(kl[-3][4]) + 0.55
    c = chan_high + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c + 0.05, low=o - 0.05)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


def test_row1_breakout_short_symmetric_positive():
    """空头正向：光头光脚放量长实体阴线跌破下轨 ≥0.2% → 触发 BREAKOUT（position=support）"""
    kl = _kl(*_base())
    _breakout_short(kl)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, -1)
    assert t is not None and t["type"] == trig.BREAKOUT
    assert t["position"] == "support" and t["direction"] == -1
    assert op is None  # 无 pending → 无处置动作


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


# ── 悬空金K距离门槛（2026-09-27 正式网复盘，A 项）───────────────────────────

def _far_golden_long(kl: list[list]) -> None:
    """信号K线收盘悬于 EMA21 上方约 3%（> 1.5% 门槛），低位悬空不构成触碰"""
    ema = _ema21_of(kl)
    c = ema * 1.03
    _set_signal_candle(kl, c, c + 0.05, c - 0.05)


def test_row4_far_from_ema21_golden_rejected():
    """悬空金K（距 EMA21 ≈3% > 1.5%）→ 纯金K不触发，无 pending 无处置"""
    kl = _kl(*_base())
    _far_golden_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


def test_row3_far_golden_with_valid_pending_still_confirms():
    """门槛只拦 T2 纯金K：row3 次根确认有回踩语境（上根触碰）不受影响"""
    kl = _kl(*_base())
    _far_golden_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-3))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert op == {"op": "confirm"}


def test_row4_far_golden_with_stale_pending_expires():
    """悬空金K + 隔轮 pending → 不触发，pending 按窗口过期处置"""
    kl = _kl(*_base())
    _far_golden_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-4))
    assert t is None
    assert op == {"op": "expire", "reason": "window_expired"}


def test_row4_far_golden_with_flipped_pending_expires_as_flip():
    kl = _kl(*_base())
    _far_golden_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1, anchor_idx=-4))
    assert t is None
    assert op == {"op": "expire", "reason": "direction_flip"}


def test_row4_distance_gate_disabled_triggers_candlestick(monkeypatch):
    """CANDLE_MAX_EMA21_DIST_PCT=0 关闭门槛 → row4 T2 原样触发"""
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 0)
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert op is None


# ── 突破幅度上限（2026-09-27 正式网复盘，B 项）─────────────────────────────

def _far_breakout_candle(kl: list[list], pct: float) -> None:
    """光头光脚放量长实体突破K线，收盘越上轨 pct%（形态/量能均过三门槛，
    仅幅度变量隔离测试上限判定）；影线留足余量避开浮点边界"""
    chan_high = max(float(k[2]) for k in kl[-2 - CHAN:-2])
    c = chan_high * (1 + pct / 100)
    _set_breakout_candle(kl, open_=c - 0.1, close=c, high=c, low=c - 0.11, vol=150.0)


def test_row1_breakout_over_max_pct_rejected():
    """越轨 2% > 1.5% 上限 → 突破不触发（追高拦截）；无金K不触碰 → 观察池"""
    kl = _kl(*_base())
    _far_breakout_candle(kl, 2.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


def test_row1_breakout_within_max_pct_triggers():
    """越轨 1.4% ≤ 1.5% 上限 → 突破正常触发（上限只拦 '>上限'）；
    形态打桩隔离 A 门槛（长实体本体会被真实检测命中金K，距 EMA21 1.4% 悬空被拦）"""
    kl = _kl(*_base())
    _far_breakout_candle(kl, 1.4)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT
    assert op is None


def test_row1_breakout_max_disabled_allows_far_breakout(monkeypatch):
    """BREAKOUT_MAX_PCT=0 关闭上限 → 超幅突破原样触发（形态打桩隔离 A 门槛）"""
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 0)
    kl = _kl(*_base())
    _far_breakout_candle(kl, 2.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT


def test_row1_breakout_over_max_short_symmetric_rejected():
    """空头对称：越下轨 2% 超上限 → 不触发"""
    kl = _kl(*_base())
    chan_low = min(float(k[3]) for k in kl[-2 - CHAN:-2])
    c = chan_low * (1 - 0.02)
    _set_breakout_candle(kl, open_=c + 0.1, close=c, high=c + 0.11, low=c, vol=150.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, -1)
    assert t is None and op is None


# ── T3 回踩双对象：已确认关键位（docs/21 §8，2026-09-27）──────────────────

def _level(price: float, state: str = "intact", flip: int | None = None) -> dict:
    """关键位位表条目（compute_key_levels 产出的最小字段集）"""
    return {"price": price, "sources": ["swing_1h"], "touches": 1,
            "rejections": 0, "kind": "low", "strength": 2,
            "state": state, "role": None, "flip_direction": flip,
            "bars_since_event": 1, "distance_pct": 0.0}


def test_level_touch_flipped_pullback_confirms():
    """翻转已确认位回踩+金K → 当根确认，reason 标注「翻转位」（EMA21 未触碰，
    证据只可能来自关键位——收盘 99.3 低于 EMA21≈100.02）"""
    kl = _kl(*_base())
    _set_signal_candle(kl, 99.3, 99.4, 99.1)  # low 进 99.0±0.25 带内、收盘收回位上
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, levels=[_level(99.0, "flipped_confirmed", 1)],
                                    atr=1.0)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert "翻转位(99" in t["reason"] and "当根" in t["reason"]
    assert op is None


def test_level_touch_ema21_fallback_unchanged():
    """位表存在但位未触碰（95 距现价过远）、EMA21 触碰 → 原行为兜底，
    reason 仍是「回踩EMA21(...)」——levels/atr 传入不改变 EMA21 路径"""
    kl = _kl(*_base())
    _touch_long(kl)  # low == ema21 ≈ 100.02，远在 95 位带之外
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, levels=[_level(95.0)], atr=1.0)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert "回踩EMA21(" in t["reason"]


def test_level_touch_band_boundary():
    """0.25×ATR 边界：low 恰在位+0.25×ATR（99.25）→ 触碰成立；
    99.26（带外）→ 不触碰，金K 落回 T2 纯形态路径"""
    kl = _kl(*_base())
    _set_signal_candle(kl, 99.5, 99.6, 99.25)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[_level(99.0)], atr=1.0)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert "关键位(99" in t["reason"]  # intact 位标注「关键位」
    # 带外一根：影线差 0.01 不构成触碰
    kl2 = _kl(*_base())
    _set_signal_candle(kl2, 99.5, 99.6, 99.26)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t2, _ = trig.detect_trigger(kl2, 1, levels=[_level(99.0)], atr=1.0)
    assert t2 is not None and t2["type"] == trig.CANDLESTICK  # 纯金K，非回踩确认
    assert "关键位" not in t2["reason"]


def test_level_touch_broken_and_invalid_excluded():
    """broken（刚突破未确认）/invalid 位不参与回踩确认——假突破回踩正是
    要避开的入场；位全被排除后金K 落回 T2 纯形态"""
    kl = _kl(*_base())
    _set_signal_candle(kl, 99.3, 99.4, 99.1)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[
            _level(99.0, "broken", 1), _level(98.5, "invalid")], atr=1.0)
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert "关键位" not in t["reason"] and "翻转位" not in t["reason"]


def test_level_touch_without_golden_upserts_pending():
    """触碰关键位但无金K → 行5 转 pending，ema21 键存位价（DB 列兼容）"""
    kl = _kl(*_base())
    _set_signal_candle(kl, 99.3, 99.4, 99.1)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1, levels=[_level(99.0)], atr=1.0)
    assert t is None
    assert op is not None and op["op"] == "upsert"
    assert op["direction"] == 1
    assert abs(op["ema21"] - 99.0) < 1e-9


# ── position 统一口径：现价最近关键位角色（2026-09-27）──────────────────────

def _level_r(price: float, role: str, state: str = "intact") -> dict:
    """带角色的关键位条目（compute_key_levels 产出 role 非空）"""
    lv = _level(price, state)
    lv["role"] = role
    return lv


def test_position_nearest_key_level_overrides_direction():
    """位置统一口径：多头金K 但现价最近的关键位是上方压力位 → position=resistance
    （不再按方向派生 support），与关键位块展示同源"""
    kl = _kl(*_base())
    _no_touch_long(kl)  # close ≈ EMA21+0.2 ≈ 100.2
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[
            _level_r(100.5, "resistance"), _level_r(95.0, "support")], atr=1.0)
    assert t is not None and t["type"] == trig.CANDLESTICK
    assert t["position"] == "resistance"


def test_position_fallback_when_roles_missing():
    """位表存在但全部无角色（invalid/旧数据）→ 回落按方向派生：多头=support"""
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[_level(100.5), _level(95.0)], atr=1.0)
    assert t is not None
    assert t["position"] == "support"


def test_position_breakout_up_nearest_support():
    """向上突破但最近位是下方支撑位（无更高位）→ position=support（旧口径固定 resistance）"""
    kl = _kl(*_base())
    _breakout_long(kl)  # close ≈ 上轨+0.5 ≈ 100.8，下方 100.5 为最近位
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, _ = trig.detect_trigger(kl, 1, levels=[_level_r(100.5, "support")])
    assert t is not None and t["type"] == trig.BREAKOUT
    assert t["position"] == "support"


def test_position_breakout_down_nearest_resistance():
    """跌破下轨但最近位是上方压力位（无更低位）→ position=resistance（旧口径固定 support）"""
    kl = _kl(*_base())
    _breakout_short(kl)  # close ≈ 下轨-0.5 ≈ 99.2，上方 99.5 为最近位
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, _ = trig.detect_trigger(kl, -1, levels=[_level_r(99.5, "resistance")])
    assert t is not None and t["type"] == trig.BREAKOUT
    assert t["position"] == "resistance"


# ── 双锚共振标志（EMA21 与关键位同时命中，质量分加权用）────────────────────

def test_dual_touch_both_ema_and_level():
    """当根确认时 EMA21 与关键位同时触碰 → dual_touch=True（reason 仍标关键位）"""
    kl = _kl(*_base())
    _touch_long(kl)  # low == ema21、收盘收回线上
    ema = _ema21_of(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[_level_r(ema, "support")],
                                   atr=1.0)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["dual_touch"] is True
    assert "关键位(" in t["reason"]


def test_dual_touch_single_ema_only():
    """仅 EMA21 触碰（位表存在但位太远）→ dual_touch=False"""
    kl = _kl(*_base())
    _touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, levels=[_level_r(95.0, "support")],
                                   atr=1.0)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["dual_touch"] is False
