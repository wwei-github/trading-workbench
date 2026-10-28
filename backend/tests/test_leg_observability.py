# -*- coding: utf-8 -*-
"""反向腿深度观测（2026-10-28，docs/16 §23）。

背景：TAUSDT 个案——回踩只有 1.30%（0.94 ATR）、腿 2 根，刚好越过
`pullback_leg_min_atr=0.5` 的底线就放行开单，事后 −0.63R。「过门」只回答
是/否，不回答「腿有多深」，于是无法用成交样本反推底线该设在哪。

本模块把腿门证据（bars / atr_mult）随信号落到 `quality_parts.leg_atr` /
`leg_bars`（只在 compute_quality_score 之后追加，不参与 total），并让被拦
候选的 probe 也带 `leg`（对照组）。只有纯函数与打桩，不触网不建库。
"""
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.scanner as sm  # noqa: E402
from app.services.scan_filters import TOUCH_RETEST, resolve_scan_filters  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.trigger import PULLBACK_CONFIRM  # noqa: E402

LEG = {"bars": 2, "atr_mult": 0.94, "leg_start_idx": 5, "leg_end_idx": 7,
       "leg_start_price": 101.0, "leg_end_price": 100.0, "atr": 1.0,
       "overshoot_atr": 0.0}
_PASS_VOTES = {"bull": ["MACD", "RSI"], "bear": [], "bull_n": 2, "bear_n": 0,
               "net": 2, "voted": 2, "results": {}}


def _flt(**over):
    base = replace(resolve_scan_filters(None),
                   pullback_touch_refs=(TOUCH_RETEST,),
                   pullback_touch_deny_long=(), pullback_touch_deny_short=(),
                   pullback_leg_gate_enabled=True)
    return replace(base, **over)


def _klines(n=40):
    ts = 1_700_000_000_000
    return [[ts + i * 3_600_000, 100.0, 100.6, 99.4, 100.1, 10.0] for i in range(n)]


def _stub_trigger_path(monkeypatch, leg=(True, LEG)):
    monkeypatch.setattr(trig, "_probe_touches",
                        lambda kl, d, f, **k: {TOUCH_RETEST: 100.5})
    monkeypatch.setattr(trig, "_pullback_leg_gate", lambda kl, d, f: leg)
    monkeypatch.setattr(trig, "detect_all_patterns", lambda kl, idx=-2: [])
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda kl, wanted, pats: (
                            {"pattern": "hammer", "direction": wanted,
                             "strength": 0.8, "offset": -2}, ""))
    monkeypatch.setattr(trig, "_pullback_volume_note", lambda kl: "")
    monkeypatch.setattr(trig, "pullback_stabilize_bonus",
                        lambda *a, **k: {"bonus": 0, "reasons": []})


# ── 触发层：信号带出腿证据 ──────────────────────────────────────────────


def test_trigger_carries_leg_evidence(monkeypatch):
    """当根确认的回踩信号必须带 leg_evidence（供 scanner 落分项）"""
    _stub_trigger_path(monkeypatch)
    out, _ = trig.detect_trigger(_klines(), 1, None, _flt())
    assert out is not None and out["type"] == PULLBACK_CONFIRM
    assert out["leg_evidence"] == LEG


def test_trigger_leg_evidence_none_when_gate_disabled(monkeypatch):
    """腿门关闭时 leg_evidence=None（不伪造深度）"""
    _stub_trigger_path(monkeypatch)
    out, _ = trig.detect_trigger(_klines(), 1, None,
                                 _flt(pullback_leg_gate_enabled=False))
    assert out is not None and out["leg_evidence"] is None


# ── 被拦候选 probe：带 leg（对照组） ────────────────────────────────────


def test_probe_reports_leg_when_touch_found(monkeypatch):
    monkeypatch.setattr(trig, "compute_indicator_votes", lambda kl, *a, **k: {"net": 2})
    _stub_trigger_path(monkeypatch)
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out["leg"] == {"ok": True, "bars": 2, "atr_mult": 0.94}


def test_probe_leg_none_without_touch(monkeypatch):
    monkeypatch.setattr(trig, "compute_indicator_votes", lambda kl, *a, **k: {"net": 2})
    _stub_trigger_path(monkeypatch)
    monkeypatch.setattr(trig, "_probe_touches", lambda kl, d, f, **k: {})
    out = trig.probe_blocked_context(_klines(), _flt())
    assert out["leg"] is None


# ── 扫描层：腿深度落 quality_parts ──────────────────────────────────────


class _FakePool:
    def __init__(self, htf):
        self.htf = htf

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self.htf[:limit]


def _candidate(monkeypatch, trig_dict):
    keys = ("analyze_ema", "classify_volume", "compute_indicator_votes",
            "htf_context", "range_score", "detect_trigger")
    orig = {k: getattr(sm, k) for k in keys}
    sm.analyze_ema = lambda k: None
    sm.classify_volume = lambda k: (1.0, "平量")
    sm.compute_indicator_votes = lambda k, h=None, htf_bg=None: _PASS_VOTES
    sm.htf_context = lambda k, core=None: {
        "direction": "long", "reject_reason": None, "block_both": False,
        "state": "aligned_bull", "state_label": "多头排列", "dir": 1,
        "macro_aligned": True, "evidence": {},
    }
    sm.range_score = lambda k, gate_ev=None: (0, [])
    sm.detect_trigger = lambda *a, **k: (trig_dict, None)
    try:
        cand, _ = sm.detect_indicator_candidate(
            _FakePool([[0, 100.0, 101.0, 99.0, 100.0, 10.0]]), "XUSDT",
            _klines(60), flt=_flt(), reject_out={})
        return cand
    finally:
        for k, v in orig.items():
            setattr(sm, k, v)


def _trig(**over):
    base = {"type": PULLBACK_CONFIRM, "direction": 1, "position": "support",
            "pattern": "hammer", "breakout_pct": 0.0, "reason": "r",
            "touch_ref": TOUCH_RETEST, "structure_gate": True,
            "leg_evidence": LEG}
    base.update(over)
    return base


def test_scanner_writes_leg_parts(monkeypatch):
    """候选的 quality_parts 应含 leg_atr / leg_bars（不参与 total）"""
    cand = _candidate(monkeypatch, _trig())
    assert cand is not None
    parts = cand["quality_parts"]
    assert parts["leg_atr"] == 0.94 and parts["leg_bars"] == 2
    assert parts["total"] == cand["quality_score"]


def test_scanner_skips_leg_parts_without_evidence(monkeypatch):
    """无腿证据（腿门关闭/突破类信号）时不写入 leg_* 键，避免 0 值被当成"浅回踩" """
    cand = _candidate(monkeypatch, _trig(leg_evidence=None))
    assert cand is not None
    assert "leg_atr" not in cand["quality_parts"]
    assert "leg_bars" not in cand["quality_parts"]
