"""回踩前置反向腿门单测（2026-10-22，docs/需求文档/08）

覆盖 trigger._pullback_leg_gate 的度量口径，以及 detect_trigger 的接入行为：
- 做多/做空：无反向腿拒绝、有反向腿放行；
- Stage-1 触碰无金K：腿门不过不建 pending；
- 非单调/陈旧腿拒绝（2026-10-22-2，ARIA 个案）；
- 陈旧腿重锚最近一段（2026-10-22-3，MAGMA 个案）；
- retest 不豁免（同样须过腿门）；
- 行3 确认侧以确认根前K线复校腿门（2026-10-22-2）；
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


def _short_non_monotonic() -> list[list]:
    """反弹腿非单边（ARIA 个案镜像）：idx54 见低 → idx55 反弹冲高 101.0
    （腿内更高高点）→ idx56 回落至 100.2（腿终点）→ idx58 看跌吞没触碰 EMA21。

    腿幅 1.2 / ATR≈0.71 = 1.68 倍、bars=2 本可通过基础门槛；但腿内高点比终点
    高 1.12 ATR，属于「反弹已走完再回落」的陈旧腿 → 非单调拒绝。
    """
    return _series(
        [99.3, 100.3, 99.8, 99.6, 99.3, 99.3],
        [99.4, 101.0, 100.2, 100.0, 100.0, 99.4],
        [99.0, 99.2, 99.6, 99.4, 99.2, 99.2],
    )


def _long_non_monotonic() -> list[list]:
    """下跌腿非单边（做空镜像）：idx54 见高 → idx55 急跌至 99.0（腿内更低低点）
    → idx56 反抽至 99.8（腿终点）→ idx58 看涨吞没触碰 EMA21。

    腿内低点比终点低 1.15 ATR → 非单调拒绝（不是一段下跌后的回踩）。
    """
    return _series(
        [100.7, 99.7, 100.2, 100.4, 100.5, 100.5],
        [100.9, 100.0, 100.4, 100.6, 100.7, 100.9],
        [100.2, 99.0, 99.8, 100.0, 99.9, 100.2],
    )


def _long_v_recovery_retest() -> list[list]:
    """MAGMA 个案复现（2026-10-22-3）：更早的高点 101.5 → 深跌见底 98.4 →
    V 型反弹到最近高点 100.8 → 回踩至 99.5 后收出锤形线。

    旧口径取回看窗绝对最高点 101.5 作腿起点，会把早已走完的 98.4 算进
    「本次回踩腿」并以非单调拒绝；以 98.4 为分界重锚后，腿 100.8→99.5
    单边下移（3 根、约 1.7 ATR），应放行。
    """
    return _series(
        [100.6, 101.2, 99.0, 99.4, 100.0, 100.5, 100.2, 100.0, 99.9, 100.3, 100.3],
        [100.9, 101.5, 99.4, 99.8, 100.3, 100.8, 100.5, 100.3, 100.2, 100.4, 100.6],
        [100.2, 100.8, 98.4, 99.0, 99.6, 100.2, 99.9, 99.7, 99.5, 99.0, 100.1],
    )


def _long_pending_then_rally() -> list[list]:
    """ARIA 型行3 反例：idx56 回踩 EMA21 建 pending（前置下跌腿 idx50→55），
    idx57-59 连涨远离触点，idx60 看涨吞没且不再触碰触点。

    Stage-1 时腿门通过（腿端 idx55、腿内无更深低点）；确认根前腿内已出现
    idx55 低点 99.8 低于新腿端 idx58 低点 100.6（约 1.48 ATR）→ 行3 复校拒绝。
    62 根：idx60=信号K线，idx61=未收盘。
    """
    closes = [100.0 + 0.01 * (i % 5) for i in range(54)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    for i, (c, h, l) in enumerate([
        (100.8, 101.0, 100.6), (100.6, 100.9, 100.4),
        (100.4, 100.7, 100.2), (100.2, 100.5, 100.0),
    ], start=50):
        closes[i], highs[i], lows[i] = c, h, l
    closes += [100.0, 99.9, 100.2, 100.5, 100.9, 101.2, 101.4, 101.3]
    highs += [100.3, 100.1, 100.3, 100.6, 101.0, 101.3, 101.6, 101.4]
    lows += [99.9, 99.8, 99.9, 100.2, 100.6, 101.0, 101.1, 101.1]
    return _klines(closes, highs, lows)


def _long_pending_then_confirm() -> list[list]:
    """行3 正例：idx57 回踩 EMA21（无金K）→ pending；idx58 看涨吞没且不碰
    EMA21，确认根前腿门复校仍通过（腿端 idx57，腿内无更深低点）→ 次根确认。
    60 根：idx58=信号K线，idx59=未收盘。
    """
    return _klines(
        [100.0 + 0.01 * (i % 5) for i in range(54)]
        + [100.7, 100.5, 100.3, 100.0, 100.6, 100.6],
        [100.0 + 0.01 * (i % 5) + 0.3 for i in range(54)]
        + [101.0, 100.8, 100.6, 100.2, 101.2, 100.7],
        [100.0 + 0.01 * (i % 5) - 0.3 for i in range(54)]
        + [100.5, 100.3, 100.1, 100.0, 100.4, 100.3],
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


def test_leg_gate_rejects_non_monotonic_short_leg():
    """反弹腿内出现比终点更高的高点（>0.5 ATR）→ 非单边腿拒绝。

    ARIA 个案：一段上涨后的 retest 看涨吞没实际来自宽幅震荡腿，腿内极值
    比终点更深/更高，终点不是真实腿端。
    """
    ok, ev = trig._pullback_leg_gate(_short_non_monotonic(), -1, _flt())
    assert ok is False
    assert ev["reason"] == "non_monotonic"
    assert ev["bars"] == 2 and ev["atr_mult"] >= 0.5
    assert ev["overshoot_atr"] > trig.LEG_OVERSHOOT_ATR


def test_leg_gate_rejects_non_monotonic_long_leg():
    """下跌腿内出现比终点更低的低点（>0.5 ATR）→ 非单边腿拒绝（做空镜像）。"""
    ok, ev = trig._pullback_leg_gate(_long_non_monotonic(), 1, _flt())
    assert ok is False
    assert ev["reason"] == "non_monotonic"
    assert ev["overshoot_atr"] > trig.LEG_OVERSHOOT_ATR


def test_leg_gate_reanchors_stale_leg_at_recent_start():
    """MAGMA 个案（2026-10-22-3）：腿内更深低点发生在更早时，以该低点为分界
    重锚最近一段腿（100.8 → 99.5，3 根、单调），而不是用窗口绝对最高点 101.5。
    """
    kl = _long_v_recovery_retest()
    ok, ev = trig._pullback_leg_gate(kl, 1, _flt())
    assert ok is True
    assert ev["leg_start_idx"] == 59
    assert ev["leg_start_price"] == pytest.approx(100.8)
    assert ev["leg_end_idx"] == 62
    assert ev["bars"] == 3
    assert ev["atr_mult"] >= 0.5
    assert ev["overshoot_atr"] <= trig.LEG_OVERSHOOT_ATR


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


def test_trigger_accepts_long_after_v_recovery_retest():
    """MAGMA 复现（2026-10-22-3）：V 型反弹后回踩，锤形线在重锚后的单边腿
    末端当根确认（旧口径会因腿内含更早低点而误拒）。"""
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(_long_v_recovery_retest(), 1, flt=_flt())
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_EMA21
    assert op is None


def test_trigger_stage1_no_pending_when_leg_rejected():
    """触碰但无金K：腿门不过 → 不 upsert pending。"""
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(_short_reject(), -1, flt=_flt())
    assert t is None and op is None


def test_trigger_rejects_non_monotonic_short_leg():
    """ARIA 镜像：宽幅震荡里的反弹腿 → 不确认（非单边腿诊断）。"""
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(
            _short_non_monotonic(), -1, flt=_flt(), diag=diag)
    assert t is None and op is None
    reasons = "；".join(diag.get("reasons", []))
    assert "无有效反弹腿" in reasons and "非单边腿" in reasons
    assert "更高高点" in reasons


def test_trigger_rejects_non_monotonic_long_leg():
    """下跌腿内出现更低低点（做空镜像）→ 不确认。"""
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(
            _long_non_monotonic(), 1, flt=_flt(), diag=diag)
    assert t is None and op is None
    reasons = "；".join(diag.get("reasons", []))
    assert "无有效下跌腿" in reasons and "更低低点" in reasons


def test_trigger_row3_confirm_rechecks_leg_context():
    """行3 确认侧复校（2026-10-22-2，ARIA 个案）：Stage-1 建池时过门，但确认根前
    价格已连涨远离触点、腿内出现更低低点 → 本根不确认、pending 保持等待。"""
    kl = _long_pending_then_rally()
    pending = {
        "direction": 1, "touch_ref": TOUCH_EMA21,
        "stage1_candle_open": _anchor(kl, 56),
    }
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(
            kl, 1, flt=_flt(), pending=pending, diag=diag)
    assert t is None and op is None
    reasons = "；".join(diag.get("reasons", []))
    assert "待确认未确认" in reasons and "更低低点" in reasons


def test_trigger_row3_confirm_with_valid_leg_context():
    """行3 正例：确认根前腿门复校通过 → 照常确认（复校不等于封杀次根确认）。"""
    kl = _long_pending_then_confirm()
    pending = {
        "direction": 1, "touch_ref": TOUCH_EMA21,
        "stage1_candle_open": _anchor(kl, 57),
    }
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, flt=_flt(), pending=pending)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_EMA21
    assert op == {"op": "confirm"}


def test_trigger_gate_disabled_switch_allows_pending():
    flt = _flt(pullback_leg_gate_enabled=False)
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        t, op = trig.detect_trigger(_short_reject(), -1, flt=flt)
    assert t is None
    assert op is not None and op["op"] == "upsert"
    assert op["touch_ref"] == TOUCH_EMA21


def test_trigger_retest_requires_leg_gate():
    """retest 不再豁免（2026-10-22-2，ARIA 个案）：无有效反向腿 → 不确认。

    ARIA：一段上涨后触及结构推导回测位的看涨吞没被命中，实质是宽幅震荡里
    的陈旧腿；retest 触点必须与其余触点一样先过腿门。
    """
    kl = _short_reject()
    flt = _flt(pullback_touch_refs=(TOUCH_RETEST, TOUCH_EMA21))
    retest = {"price": 100.0, "direction": -1, "candle_open": _anchor(kl, -3)}
    diag: dict = {}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(
            kl, -1, flt=flt, retest=retest, diag=diag)
    assert t is None and op is None
    assert any("无有效反弹腿" in x for x in diag.get("reasons", []))


def test_trigger_retest_confirms_with_valid_leg():
    """retest 触点在有真实反向腿时照常确认（豁免移除不等于封杀该路径）。"""
    kl = _short_accept()
    flt = _flt(pullback_touch_refs=(TOUCH_RETEST, TOUCH_EMA21))
    retest = {"price": 100.0, "direction": -1, "candle_open": _anchor(kl, -3)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_SHORT):
        t, op = trig.detect_trigger(kl, -1, flt=flt, retest=retest)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_RETEST
    assert op is None
