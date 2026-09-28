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
# _base 微扰序列的摆动结构（收盘价口径 pivot：i%5==4 为高点、i%5==0 为低点），
# 2026-09-28 突破位改摆动结构口径——最近未破前高/前低
SWING_HIGH = 100.04
SWING_LOW = 100.00


@pytest.fixture(autouse=True)
def _pin_breakout_settings(monkeypatch):
    """固定突破门槛与新增上限，免疫本地 .env 覆盖（pydantic-settings 优先级高于默认值）"""
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
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
    """光头光脚放量长实体突破K线：收盘高于摆动前高约0.5%，量1.5倍均量"""
    o = float(kl[-3][4])
    c = SWING_HIGH + 0.5
    ts, _, _, _, _, _, qv = kl[-2]
    kl[-2] = [ts, o, c, o, c, 150.0, qv]


def _breakout_short(kl: list[list]) -> None:
    """空头版：光头光脚放量长实体阴线，收盘低于摆动前低约0.5%

    阴线 c < o：high=open、low=close（与阳线夹具的列序相反，勿混用）
    """
    o = float(kl[-3][4])
    c = SWING_LOW - 0.5
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
GOLDEN_SHORT = [{"pattern": "shooting_star", "direction": "bearish"}]


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
    """毛边突破：收盘仅高于前高 0.05%（< 0.2% 门槛）→ 不触发；
    K线跨越 EMA21 落入回踩触碰分支 → upsert pending（正确的管线落空行为）"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = SWING_HIGH * (1 + 0.0005)
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_rejected_low_volume():
    """缩量突破：量比 1.0（< 1.2 门槛）→ 不触发，落空转 pending"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = SWING_HIGH + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o, vol=100.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_rejected_long_upper_shadow():
    """长上影突破：上影把实体占比压到 0.67 < 0.75 门槛 → 不触发，落空转 pending"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = SWING_HIGH + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c + 0.25, low=o)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is not None and op["op"] == "upsert"


def test_row1_breakout_short_shadow_allowed():
    """影线短但非零：单侧上影 22% 全长、实体 78% 占主体 → 放行触发
    （2026-09-28 放宽影线口径：不再要求近光头光脚，主体占比即影线约束）"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = o + 0.78  # 实体 0.78 / 全长 1.00 = 78% ≥ 75%
    _set_breakout_candle(kl, open_=o, close=c, high=c + 0.22, low=o)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT


def test_row1_breakout_rejected_small_body():
    """短实体突破：实体/全长 0.72 < 0.75 门槛 → 不触发；
    整根悬于 EMA21 上方不构成回踩 → 观察池"""
    kl = _kl(*_base())
    o = float(kl[-3][4]) + 0.55
    c = SWING_HIGH + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c + 0.05, low=o - 0.05)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


def test_row1_breakout_short_symmetric_positive():
    """空头正向：长实体放量阴线跌破前低 ≥0.2% → 触发 BREAKOUT（position=support）"""
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
    """光头光脚放量长实体突破K线，收盘越前高 pct%（形态/量能均过三门槛，
    仅幅度变量隔离测试上限判定）；影线留足余量避开浮点边界"""
    c = SWING_HIGH * (1 + pct / 100)
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
    """空头对称：破前低 2% 超上限 → 不触发"""
    kl = _kl(*_base())
    c = SWING_LOW * (1 - 0.02)
    _set_breakout_candle(kl, open_=c + 0.1, close=c, high=c + 0.11, low=c, vol=150.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, -1)
    assert t is None and op is None
