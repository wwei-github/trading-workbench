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


# 2026-09-29 复合确认规则后，单影线（锤/流星）不再单根确认——矩阵测试用
# 复合形态（吞没）打桩走"单形态直通"路径；锤/流星复合确认见文件末尾专测
GOLDEN_LONG = [{"pattern": "bullish_engulfing", "direction": "bullish"}]
GOLDEN_SHORT = [{"pattern": "bearish_engulfing", "direction": "bearish"}]


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


def test_row1_breakout_low_volume_allowed():
    """缩量突破：量比 1.0（< 1.2）→ 放量已降为加分项不卡死（2026-09-29），
    仍触发 BREAKOUT，reason 无"放量"前缀（达标才展示）"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = SWING_HIGH + 0.5
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o, vol=100.0)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT
    assert "放量" not in t["reason"]


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
    assert t["position"] == "support" and t["pattern"] == "bullish_engulfing"
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
    assert t["pattern"] == "bullish_engulfing"
    assert op == {"op": "confirm"}


def test_row3_pending_stale_no_trigger_expires():
    """pending 锚点不是上一根（隔轮）→ 失效：纯金K已下线不触发，pending 过期"""
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, anchor_idx=-4))
    assert t is None
    assert op == {"op": "expire", "reason": "window_expired"}


def test_row3_pending_direction_flip_no_trigger_expires():
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, direction=-1, anchor_idx=-3))
    assert t is None
    assert op == {"op": "expire", "reason": "direction_flip"}


# ── 行4：金K无触碰无 pending → 不触发（纯金K 2026-09-29 下线）──────────────

def test_row4_golden_only_no_trigger():
    """金K必须与触点配合：无触碰无 pending 的金K 一律不触发（正式网复盘负期望）"""
    kl = _kl(*_base())
    _no_touch_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


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
            assert t is None, f"anchor_idx={idx} 应失效且纯金K不触发"
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


# ── 行4 补充：远离 EMA21 的悬空金K同样不触发（纯金K下线后无门槛概念，覆盖极端距离）──

def _far_golden_long(kl: list[list]) -> None:
    """信号K线收盘悬于 EMA21 上方约 3%（> 1.5% 门槛），低位悬空不构成触碰"""
    ema = _ema21_of(kl)
    c = ema * 1.03
    _set_signal_candle(kl, c, c + 0.05, c - 0.05)


def test_row4_far_from_ema21_golden_rejected():
    """悬空金K（距 EMA21 ≈3%）→ 无触碰不触发（金K必须与触点配合），无 pending 无处置"""
    kl = _kl(*_base())
    _far_golden_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None and op is None


def test_row3_far_golden_with_valid_pending_still_confirms():
    """金K下线只影响零触碰分支：row3 次根确认有回踩语境（上根触碰）不受影响"""
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


# ── 趋势突破（2026-09-29 恢复，T1b trigger_trend_breakout）─────────────────

def _trend_long_klines(n: int = 60, base: float = 100.0, step: float = 0.5,
                       wick: float = 0.01) -> list[list]:
    """严格上升序列（收盘逐根新高，摆动位全破无未破前高）+ 放量长实体阳线信号K；
    信号收盘越 20 根滚动上轨（chan_high=前一根高=close+wick）≥0.2%"""
    closes = [base + step * i for i in range(n)]
    highs = [c + wick for c in closes]
    lows = [c - wick for c in closes]
    kl = _klines(closes, highs, lows)
    o = float(kl[-3][4])
    c = float(kl[-2][4])
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o, vol=150.0)
    return kl


def _trend_short_klines(n: int = 60, base: float = 100.0, step: float = 0.5,
                        wick: float = 0.01) -> list[list]:
    """严格下降序列 + 放量长实体阴线信号K（阴线列序 high=open、low=close）"""
    closes = [base - step * i for i in range(n)]
    highs = [c + wick for c in closes]
    lows = [c - wick for c in closes]
    kl = _klines(closes, highs, lows)
    o = float(kl[-3][4])
    c = float(kl[-2][4])
    _set_breakout_candle(kl, open_=o, close=c, high=o, low=c, vol=150.0)
    return kl


def test_row1b_trend_breakout_strong_trend_fires():
    """强趋势连续新高（无未破摆动前高）+ 收盘破 20 根 Donchian 上轨 → 趋势突破"""
    kl = _trend_long_klines()
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.TREND_BREAKOUT
    assert t["position"] == "resistance"
    assert "Donchian上轨" in t["reason"]
    assert t["breakout_level"] > 0 and t["candle_open"] is not None
    assert op is None


def test_row1b_trend_breakout_short_symmetric():
    kl = _trend_short_klines()
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, -1)
    assert t is not None and t["type"] == trig.TREND_BREAKOUT
    assert t["position"] == "support"
    assert "Donchian下轨" in t["reason"]


def test_row1b_trend_breakout_supersedes_pending():
    """趋势突破与结构突破同级强触发：同向 pending → supersede"""
    kl = _trend_long_klines()
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl))
    assert t is not None and t["type"] == trig.TREND_BREAKOUT
    assert op == {"op": "supersede", "reason": "breakout"}


def test_row1b_trend_breakout_low_volume_allowed():
    """量比 1.0 < 1.2 → 放量已降为加分项不卡死（2026-09-29），趋势突破仍触发，
    reason 无"放量"前缀（达标才展示）"""
    kl = _trend_long_klines()
    ts, o, h, l, c, _, qv = kl[-2]
    kl[-2] = [ts, o, h, l, c, 100.0, qv]  # 量比降回 1.0
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.TREND_BREAKOUT
    assert "放量" not in t["reason"]


def test_row1b_trend_breakout_over_max_pct_rejected():
    """越轨幅度超上限 → 追高拦截（上限按轨价衡量）：强趋势逐根推进天然贴近轨价
    （step/轨价 ≈0.39%），默认 1.5% 上限不拦（前一用例），收窄至 0.3% 拦下"""
    kl = _trend_long_klines()
    flt = trig.ScanFilters(
        min_volume_24h=3_000_000, breakout_channel_bars=20,
        breakout_threshold=0.002, breakout_vol_ratio=1.2,
        breakout_max_pct=0.3, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
    )
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(kl, 1, flt=flt)
    assert t is None and op is None


def test_row1b_trend_breakout_channel_bars_configurable():
    """Donchian 通道根数可配：40 根窗口下强趋势同样触发（breakout_channel_bars 生效）"""
    kl = _trend_long_klines()
    flt = trig.ScanFilters(
        min_volume_24h=3_000_000, breakout_channel_bars=40,
        breakout_threshold=0.002, breakout_vol_ratio=1.2,
        breakout_max_pct=1.5, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
    )
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, _ = trig.detect_trigger(kl, 1, flt=flt)
    assert t is not None and t["type"] == trig.TREND_BREAKOUT


def test_row1a_structural_priority_over_trend():
    """互斥归类·结构优先：收盘同时越过结构前高与 Donchian 轨 → 结构突破（T1a）"""
    kl = _kl(*_base())
    _breakout_long(kl)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, _ = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.BREAKOUT
    assert "前高" in t["reason"]


def _range_under_structure_klines() -> list[list]:
    """前段冲高 105 形成未破结构前高，中后段回落横盘 100.5；
    信号K线放量长实体破 20 根滚动上轨但未越结构前高 → 趋势突破（非结构突破）"""
    closes = [100.0] * 60
    closes[30] = 105.0                                   # 收盘口径摆动前高（3 根两侧更低确认）
    closes[31:38] = [101.0, 100.2, 100.0, 100.0, 100.0, 100.0, 100.0]
    for i in range(38, 60):
        closes[i] = 100.5                                # 20 根窗口横盘，轨 ≈ 100.8
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    kl = _klines(closes, highs, lows)
    o = float(kl[-3][4])
    c = 101.5                                            # 越轨 ≈0.7%（<1.5% 未追高）、低于前高 105
    _set_breakout_candle(kl, open_=o, close=c, high=c, low=o, vol=150.0)
    return kl


def test_row1b_trend_breakout_under_unbroken_structure():
    """互斥归类·回落口径：有未破结构前高但收盘未越它、破滚动轨 → 趋势突破"""
    kl = _range_under_structure_klines()
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, _ = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.TREND_BREAKOUT
    assert "Donchian上轨" in t["reason"]


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


# ── 单影线形态复合确认（2026-09-29 用户拍板：单锤/单流星不再单根确认）──────
# 真实形态构造（不 mock detect_all_patterns）：锤形线=小实体长下影短上影，
# 流星线=小实体长上影短下影，趋势K=实体≥75%全长

def _hammer_at(kl: list[list], idx: int) -> None:
    """把 kl[idx] 改写为看多锤形线：o=100.10 c=100.30（body 0.2）、
    l=99.10（下影 1.0 ≥ 2×body）、h=100.40（上影 0.1 ≤ 15%×1.3=0.195）"""
    ts = kl[idx][0]
    kl[idx] = [ts, 100.10, 100.40, 99.10, 100.30, 100.0, 1e6]


def _shooting_star_at(kl: list[list], idx: int) -> None:
    """把 kl[idx] 改写为看空流星线（倒锤形）：o=100.20 c=100.00（body 0.2）、
    h=101.20（上影 1.0 ≥ 2×body）、l=100.05（下影 0.05 ≤ 15%×1.15=0.1725）"""
    ts = kl[idx][0]
    kl[idx] = [ts, 100.20, 101.20, 100.05, 100.00, 100.0, 1e6]


def _trend_candle_at(kl: list[list], idx: int, direction: int = 1) -> None:
    """把 kl[idx] 改写为顺向趋势K：实体 0.15 / 全长 0.19 ≈0.79 ≥0.75（阳/阴按方向）。

    收盘控制在摆动结构区间内（c=100.15 < 前高 100.04×1.002），避免实体过大
    抢先触发突破分支（T1 优先）或与微扰序列凑成黄昏星（k3 大实体 + k2 小实体）。
    """
    ts = kl[idx][0]
    if direction == 1:
        o, h, l, c = 100.00, 100.17, 99.98, 100.15
    else:
        o, h, l, c = 100.15, 100.17, 99.98, 100.00
    kl[idx] = [ts, o, h, l, c, 100.0, 1e6]


def test_single_hammer_touch_no_trigger_becomes_pending():
    """单锤触碰触点 → 不触发（旧行为是当根确认），转 pending 等次根确认"""
    kl = _kl(*_base())
    _touch_long(kl)          # 当根触碰 EMA21（收盘收回线上）
    _hammer_at(kl, -2)       # 当根锤形（close=100.30 > ema、low=99.10 触及 → 仍触碰）
    t, op = trig.detect_trigger(kl, 1)
    assert t is None
    assert op is not None and op["op"] == "upsert"  # 触碰无有效金K → 转待确认


def test_double_hammer_confirms():
    """双锤：前根+当根连续锤形 → 有效金K证据，触碰路径当根确认"""
    kl = _kl(*_base())
    _hammer_at(kl, -3)       # 前根锤
    _hammer_at(kl, -2)       # 当根锤（low 触及 ema、收盘线上 → 当根触碰成立）
    t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["pattern"] == "hammer"
    assert "双锤组合" in t["reason"]


def test_double_shooting_star_confirms_short():
    """空头对称：前根+当根连续流星线 → 有效金K证据"""
    kl = _kl(*_base())
    _shooting_star_at(kl, -3)
    _shooting_star_at(kl, -2)
    t, op = trig.detect_trigger(kl, -1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["pattern"] == "shooting_star"
    assert "双锤组合" in t["reason"]


def test_hammer_plus_trend_candle_confirms():
    """锤+趋势K：前根锤 + 当根顺向长实体阳线 → 组合确认（pattern 落前根锤）"""
    kl = _kl(*_base())
    _hammer_at(kl, -3)
    _trend_candle_at(kl, -2, direction=1)
    t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["pattern"] == "hammer"
    assert "前根锤形线+当根趋势K" in t["reason"]


def test_trend_candle_without_prev_hammer_no_trigger():
    """当根趋势K但前根不是锤 → 无有效证据，不触发"""
    kl = _kl(*_base())
    _trend_candle_at(kl, -2, direction=1)  # 前根保持普通微扰K线
    t, op = trig.detect_trigger(kl, 1)
    assert t is None


def test_shooting_star_plus_trend_candle_confirms_short():
    """空头对称：前根流星 + 当根顺向长实体阴线 → 组合确认"""
    kl = _kl(*_base())
    _shooting_star_at(kl, -3)
    _trend_candle_at(kl, -2, direction=-1)
    t, op = trig.detect_trigger(kl, -1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["pattern"] == "shooting_star"
    assert "前根倒锤形线（流星）+当根趋势K" in t["reason"]


# ── resolve_golden_evidence 裁决函数直测（规则1/2/3 与否决路径）────────────

def test_resolve_direct_pass_for_composite_pattern():
    """规则1：复合形态（吞没）方向匹配 → 单形态直通、note 为空"""
    kl = _kl(*_base())
    golden, note = trig.resolve_golden_evidence(
        kl, "bullish", [{"pattern": "bullish_engulfing", "direction": "bullish"}])
    assert golden is not None and golden["pattern"] == "bullish_engulfing"
    assert note == ""


def test_resolve_direction_mismatch_rejected():
    """方向不匹配的金K（看多语境遇流星）→ 无证据"""
    kl = _kl(*_base())
    _hammer_at(kl, -3)
    _hammer_at(kl, -2)
    golden, _ = trig.resolve_golden_evidence(
        kl, "bearish", [{"pattern": "hammer", "direction": "bullish"}])
    assert golden is None
