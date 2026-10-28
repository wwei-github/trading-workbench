# -*- coding: utf-8 -*-
"""被 4h 门拦下候选的触点/金K解析单测（docs/16 §16.3 D1）。

背景：4h 前置门在触点探测**之前**就把候选拦掉，`shadow_marks` 只存 4h 证据，
导致反事实回放只能事后重算链路（复现率 2.4%）。本模块给被拦候选补一次
「触点 / 金K」解析并随快照落库。

只跑纯函数：K 线用合成序列 + monkeypatch 掉方向票与触点探测器，
验证「方向取值 / 触点标注 / 金K证据 / 异常不抛」四条。
"""
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scan_filters import (  # noqa: E402
    TOUCH_EMA21,
    TOUCH_SWING,
    resolve_scan_filters,
)
from app.services.strategy import trigger as trig  # noqa: E402


def _flt(**overrides):
    base = replace(
        resolve_scan_filters(None),
        pullback_touch_refs=(TOUCH_EMA21,),
        pullback_touch_deny_long=(),
        pullback_touch_deny_short=(),
    )
    return replace(base, **overrides)


def _klines(n=40, price=100.0, step=0.1):
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        base = price + i * step
        out.append([ts + i * 3_600_000, base, base + 0.6, base - 0.6, base + 0.05, 100.0])
    return out


def _patch(monkeypatch, net=2, touch=99.5, golden=None):
    monkeypatch.setattr(trig, "compute_indicator_votes",
                        lambda kl, *a, **k: {"net": net})
    monkeypatch.setattr(trig, "_probe_touches",
                        lambda kl, d, f, **k: {TOUCH_EMA21: touch} if touch is not None else {})
    monkeypatch.setattr(trig, "detect_all_patterns", lambda kl, idx=-2: [])
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda kl, wanted, pats: (golden, "组合说明" if golden else ""))


def test_returns_none_direction_when_votes_flat(monkeypatch):
    """|net|<1 时不猜方向：触点与金K 都留 None。"""
    _patch(monkeypatch, net=0)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out == {"votes_net": 0, "touch_ref": None, "touch_price": None,
                   "golden": None, "leg": None}


def test_votes_sign_decides_direction(monkeypatch):
    """net 为正 → 多头口径（bullish），net 为负 → 空头口径。"""
    seen = {}
    _patch(monkeypatch, net=-3)
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda kl, wanted, pats: (seen.setdefault("wanted", wanted) and None, ""))
    trig.probe_blocked_context(_klines(), _flt())
    assert seen["wanted"] == "bearish"


def test_touch_ref_and_price_recorded(monkeypatch):
    _patch(monkeypatch, net=2, touch=99.5)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out["votes_net"] == 2
    assert out["touch_ref"] == TOUCH_EMA21
    assert out["touch_price"] == 99.5


def test_no_touch_leaves_ref_none(monkeypatch):
    _patch(monkeypatch, net=2, touch=None)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out["touch_ref"] is None and out["touch_price"] is None


def test_golden_payload_kept(monkeypatch):
    """金K证据只留 pattern/offset/note 三个字段（体积与口径都要稳）。"""
    golden = {"pattern": "hammer", "direction": "bullish", "offset": -2, "strength": 0.8}
    _patch(monkeypatch, net=2, golden=golden)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out["golden"] == {"pattern": "hammer", "offset": -2, "note": "组合说明"}


def test_exception_never_raises(monkeypatch):
    """解析异常也要返回结构完整的 dict——影子快照绝不拖累扫描。"""
    def boom(*a, **k):
        raise RuntimeError("投票炸了")
    monkeypatch.setattr(trig, "compute_indicator_votes", boom)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out == {"votes_net": None, "touch_ref": None, "touch_price": None,
                   "golden": None, "leg": None}


def test_deny_applies_when_other_refs_exist(monkeypatch):
    """多头禁用 ema21、白名单还有 swing 时，ema21 不再参与探测。

    这里不能桩 `_probe_touches`——方向禁用表正是在它内部由
    `flt.touch_refs_for(direction)` 生效，桩掉就把被测逻辑一起桩掉了。
    改为桩最底层的探测器，让真实白名单逻辑跑一遍。
    """
    monkeypatch.setattr(trig, "compute_indicator_votes", lambda kl, *a, **k: {"net": 2})
    monkeypatch.setattr(trig, "_detect_ema_touch",
                        lambda kl, d, period, f, htf_dir=None: 99.5)
    monkeypatch.setattr(trig, "_detect_swing_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "detect_all_patterns", lambda kl, idx=-2: [])
    monkeypatch.setattr(trig, "resolve_golden_evidence", lambda kl, w, p: (None, ""))
    flt = _flt(pullback_touch_refs=(TOUCH_EMA21, TOUCH_SWING),
               pullback_touch_deny_long=(TOUCH_EMA21,))
    out = trig.probe_blocked_context(_klines(), flt)
    assert out["touch_ref"] is None


def test_all_denied_falls_back(monkeypatch):
    """白名单被全禁时回落通用白名单（docs/15 §16 的防停摆语义），不是静默停机。"""
    monkeypatch.setattr(trig, "compute_indicator_votes", lambda kl, *a, **k: {"net": 2})
    monkeypatch.setattr(trig, "_detect_ema_touch",
                        lambda kl, d, period, f, htf_dir=None: 99.5)
    monkeypatch.setattr(trig, "detect_all_patterns", lambda kl, idx=-2: [])
    monkeypatch.setattr(trig, "resolve_golden_evidence", lambda kl, w, p: (None, ""))
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,))
    out = trig.probe_blocked_context(_klines(), flt)
    assert out["touch_ref"] == TOUCH_EMA21


def test_short_side_still_probes_when_long_denied(monkeypatch):
    """同一份禁用表只作用于多头：空头仍应探测并标出 ema21。"""
    monkeypatch.setattr(trig, "compute_indicator_votes", lambda kl, *a, **k: {"net": -2})
    monkeypatch.setattr(trig, "_detect_ema_touch",
                        lambda kl, d, period, f, htf_dir=None: 99.5)
    monkeypatch.setattr(trig, "detect_all_patterns", lambda kl, idx=-2: [])
    monkeypatch.setattr(trig, "resolve_golden_evidence", lambda kl, w, p: (None, ""))
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,))
    out = trig.probe_blocked_context(_klines(), flt)
    assert out["touch_ref"] == TOUCH_EMA21
