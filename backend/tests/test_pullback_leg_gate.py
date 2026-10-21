"""回踩前置反向腿门单测（2026-10-22，docs/需求文档/08）

覆盖 trigger._pullback_leg_gate 的度量口径，以及 detect_trigger 的接入行为：
- 做多/做空：无反向腿拒绝、有反向腿放行；
- Stage-1 触碰无金K：腿门不过不建 pending；
- retest 豁免；
- 总开关/零阈值停用；
- ATR 对齐、无前视。

形态检测与 1H 结构门打桩，EMA21 触碰用真实 EMA 计算。
"""
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scan_filters import (  # noqa: E402
    TOUCH_EMA21,
    TOUCH_RETEST,
    resolve_scan_filters,
)
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.technical import atr_series  # noqa: E402

TS0 = 1716000000000
STEP = 3_600_000

GOLDEN_LONG = [{"pattern": "bullish_engulfing", "direction": "bullish"}]
GOLDEN_SHORT = [{"pattern": "bearish_engulfing", "direction": "bearish"}]


@pytest.fixture(autouse=True)
def _stub_1h_structure_gate(monkeypatch):
    """本文件聚焦腿门；1H 结构门打桩为通过（其逻辑另有专测）。"""
    monkeypatch.setattr(
        trig, "structure_1h",
        lambda k1, direction, cfg=None: ({"d21": 1.0}, {"aligned": True}),
    )


def _flt(**overrides):
    """基准筛选参数：仅 EMA21 触点 + 高突破门槛（避免合成K线误走突破路径）。"""
    base = replace(
        resolve_scan_filters(None),
        pullback_touch_refs=(TOUCH_EMA21,),
        breakout_threshold=0.05,
        breakout_max_pct=0.0,
    )
    return replace(base, **overrides)


def _klines(closes, highs, lows) -> list[list]:
    """7 列 K 线：[ts, open, high, low, close, vol, quote_vol]；open=前收，末根未收盘"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append([TS0 + i * STEP, prev, highs[i], lows[i], c, 100.0, 1e6])
        prev = c
    return out


def _series(extra_closes, extra_highs, extra_lows) -> list[list]:
    """54 根横盘基准 + 6 根自定义（idx 54..59；idx 58=信号K线，59=未收盘）。"""
    closes = [100.0 + 0.01 * (i % 5) for i in range(54)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    return _klines(closes + extra_closes, highs + extra_highs, lows + extra_lows)


def _short_reject() -> list[list]:
    """下跌中继：idx56 见低 → idx57 单根反抽到 EMA21 → idx58 看跌吞没。

    idx58 吞没自身高点 100.5 > idx57 高点 100.0：若腿终点把信号K线算进去，
    会以「idx56 低点 → idx58 高点」凑出 2 根腿（2026-10-22 检测修复的漏洞）。
    """
    return _series(
        [99.3, 99.4, 99.2, 99.8, 99.2, 99.2],
        [99.4, 99.5, 99.3, 100.0, 100.5, 99.3],
        [99.2, 99.3, 99.0, 99.1, 98.5, 99.1],
    )


def _short_accept() -> list[list]:
    """idx54 见低 → idx55-57 三根反弹到 EMA21 → idx58 看跌吞没。"""
    return _series(
        [99.3, 99.5, 99.7, 99.9, 99.2, 99.2],
        [99.4, 99.6, 99.8, 100.0, 100.0, 99.3],
        [99.0, 99.2, 99.4, 99.6, 98.5, 99.1],
    )


def _long_reject() -> list[list]:
    """上涨中继：idx56 见高 → idx57 单根回撤到 EMA21 → idx58 看涨吞没。

    idx58 吞没自身低点 99.8 < idx57 低点 100.0：腿终点若含信号K线，
    会以「idx56 高点 → idx58 低点」凑出 2 根腿（做空镜像漏洞）。
    """
    return _series(
        [100.3, 100.5, 100.7, 100.0, 100.3, 100.3],
        [100.6, 100.8, 101.0, 100.2, 101.2, 100.4],
        [100.1, 100.3, 100.5, 100.0, 99.8, 100.2],
    )


def _long_accept() -> list[list]:
    """idx54 见高 → idx55-57 三根回撤到 EMA21 → idx58 看涨吞没。"""
    return _series(
        [100.7, 100.5, 100.3, 100.0, 100.3, 100.3],
        [101.0, 100.8, 100.6, 100.2, 101.2, 100.4],
        [100.5, 100.3, 100.1, 100.0, 100.0, 100.2],
    )


def _anchor(kl: list[list], idx: int) -> datetime:
    return datetime.utcfromtimestamp(kl[idx][0] / 1000)


# ── helper 口径 ────────────────────────────────────────────────────────────

def test_leg_gate_short_accept_evidence():
    kl = _short_accept()
    ok, ev = trig._pullback_leg_gate(kl, -1, _flt())
    assert ok is True
    assert ev["bars"] == 3
    assert ev["atr_mult"] >= 0.5


def test_leg_gate_short_reject_one_bar_rebound():
    ok, ev = trig._pullback_leg_gate(_short_reject(), -1, _flt())
    assert ok is False
    assert ev["bars"] == 1


def test_leg_gate_long_accept_evidence():
    ok, ev = trig._pullback_leg_gate(_long_accept(), 1, _flt())
    assert ok is True
    assert ev["bars"] == 3


def test_leg_gate_long_reject_one_bar_decline():
    ok, ev = trig._pullback_leg_gate(_long_reject(), 1, _flt())
    assert ok is False
    assert ev["bars"] == 1


def test_leg_gate_excludes_signal_candle_from_leg_end():
    """腿终点严格在信号K线之前：反转K线自身极值不得充当反向腿终点。

    回归 2026-10-22 检测漏洞：idx58 吞没高点/低点远超 idx57 反抽/回撤极值时，
    旧口径会误判为 2 根腿并放行。
    """
    kl_s = _short_reject()
    ok_s, ev_s = trig._pullback_leg_gate(kl_s, -1, _flt())
    assert ok_s is False and ev_s["leg_end_idx"] < len(kl_s) - 2

    kl_l = _long_reject()
    ok_l, ev_l = trig._pullback_leg_gate(kl_l, 1, _flt())
    assert ok_l is False and ev_l["leg_end_idx"] < len(kl_l) - 2


def test_leg_gate_atr_alignment():
    """腿幅分母 = 腿终点那根已收盘K线的 ATR，与 atr_series 对齐。"""
    kl = _short_accept()
    ok, ev = trig._pullback_leg_gate(kl, -1, _flt())
    atrs = atr_series(kl, 14)
    assert atrs is not None and len(atrs) == len(kl) - 1
    assert ev["atr"] == float(atrs[ev["leg_end_idx"]])


def test_leg_gate_no_lookahead():
    """改未收盘根 klines[-1] 不改变判定（腿门只用已收盘K线）。"""
    kl = _short_accept()
    before = trig._pullback_leg_gate(kl, -1, _flt())
    ts, o, h, low, c, vol, qv = kl[-1]
    kl[-1] = [ts, o, h * 2, low / 2, c * 1.5, vol, qv]
    after = trig._pullback_leg_gate(kl, -1, _flt())
    assert before[0] == after[0]
    assert before[1]["bars"] == after[1]["bars"]
    assert before[1]["atr_mult"] == pytest.approx(after[1]["atr_mult"])


def test_leg_gate_zero_thresholds_disabled():
    ok, ev = trig._pullback_leg_gate(
        _short_reject(), -1,
        _flt(pullback_leg_min_bars=0, pullback_leg_min_atr=0.0),
    )
    assert ok is True
    assert ev["reason"] == "disabled"


# ── detect_trigger 接入 ────────────────────────────────────────────────────

def test_trigger_rejects_short_without_rebound():
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(_short_reject(), -1, flt=_flt(), diag=diag)
    assert t is None and op is None
    assert any("无有效反弹腿" in x for x in diag.get("reasons", []))


def test_trigger_accepts_short_after_rebound():
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(_short_accept(), -1, flt=_flt())
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_EMA21
    assert op is None


def test_trigger_rejects_long_without_decline():
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(_long_reject(), 1, flt=_flt(), diag=diag)
    assert t is None and op is None
    assert any("无有效下跌腿" in x for x in diag.get("reasons", []))


def test_trigger_accepts_long_after_decline():
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(_long_accept(), 1, flt=_flt())
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_EMA21
    assert op is None


def test_trigger_stage1_no_pending_when_leg_rejected():
    """触碰但无金K：腿门不过 → 不 upsert pending。"""
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(_short_reject(), -1, flt=_flt())
    assert t is None and op is None


def test_trigger_gate_disabled_switch_allows_pending():
    flt = _flt(pullback_leg_gate_enabled=False)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(_short_reject(), -1, flt=flt)
    assert t is None
    assert op is not None and op["op"] == "upsert"
    assert op["touch_ref"] == TOUCH_EMA21


def test_trigger_retest_exempt_from_leg_gate():
    """retest 触点不调用腿门：即使 helper 打成拒绝，retest 仍确认。"""
    kl = _short_reject()
    flt = _flt(pullback_touch_refs=(TOUCH_RETEST, TOUCH_EMA21))
    retest = {"price": 100.0, "direction": -1, "candle_open": _anchor(kl, -3)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT), \
         patch.object(trig, "_pullback_leg_gate",
                      return_value=(False, {"bars": 0, "atr_mult": 0.0})):
        t, op = trig.detect_trigger(
            kl, -1, flt=flt, retest=retest)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_RETEST
    assert op is None
