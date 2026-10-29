# -*- coding: utf-8 -*-
"""筛选关卡归因修正（docs/23 §1.2，T1/T3/T4，2026-10-29）。

只验证「同一拒绝动作下关卡名/说明更准确」，不改变候选数：
- T1 腿门失败被记成 range_score → 归回 leg_gate
- T3 方向歧义但状态机明确禁开 → 归回 htf_state_lock
- T4 反向票（3 票）在 votes 前判定 → direction_flip 可达
"""
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.services import scanner as sm  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402


def _k1(n: int = 80):
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        base = 100 + i * 0.1
        out.append([ts + i * 3_600_000, base, base + 0.5, base - 0.5, base + 0.1, 100.0])
    return out


class _Pool:
    def __init__(self, k1, k4):
        self.k1, self.k4 = k1, k4

    def get_klines(self, symbol, interval, limit, **kw):
        return (self.k4 if interval == "4h" else self.k1)[-limit:]


def _htf_ok() -> dict:
    return {
        "state": "aligned_bull", "state_label": "多头排列", "direction": "long",
        "dir": 1, "block_both": False, "reject_reason": None,
        "evidence": {"close_vs_ema144": 1.2, "slope_pct": 0.8, "adx": 25.0},
    }


@pytest.fixture()
def pool(monkeypatch):
    """4h 放行 + 方向票同向 + 震荡闸门通过的最小链路。"""
    monkeypatch.setattr(sm, "htf_context", lambda *a, **k: _htf_ok())
    monkeypatch.setattr(sm, "compute_indicator_votes",
                        lambda *a, **k: {"bull_n": 3, "bear_n": 0, "net": 3})
    monkeypatch.setattr(sm, "range_score", lambda *a, **k: (0.0, []))
    k = _k1()
    return _Pool(k, k)


def test_leg_gate_not_masked_by_range_score(pool, monkeypatch):
    """T1：腿门失败导致本根进不了待确认池，随后被 range_score 记名 → 归回 leg_gate

    T9（2026-10-29）：同时把腿深结构化（bars/atr_mult/overshoot_atr）带出到
    reject_out，才能落 funnel_json.leg_evidence 供「无触发」页签按档筛选。
    """
    def fake_trig(*a, **kw):
        diag = kw.get("diag")
        if diag is not None:
            diag["reasons"] = ["回踩EMA21前无有效下跌腿：1根/0.2ATR < 门槛2根/0.5ATR"]
            diag["stage"] = "leg_gate"
            diag["leg"] = {"bars": 1, "atr_mult": 0.2, "overshoot_atr": 0.0}
        return None, None

    monkeypatch.setattr(sm, "detect_trigger", fake_trig)
    monkeypatch.setattr(sm, "range_score", lambda *a, **k: (sm.RANGE_SCORE_REJECT, []))

    rv: dict = {}
    cand, _op = sm.detect_indicator_candidate(
        pool, "XUSDT", pool.k1, flt=resolve_scan_filters(None), reject_out=rv)
    assert cand is None
    assert rv["stage"] == "leg_gate"
    assert "下跌腿" in (rv.get("detail") or "")
    assert rv["leg"] == {"bars": 1, "atr_mult": 0.2, "overshoot_atr": 0.0}


def test_undetermined_with_lock_records_state_lock(pool, monkeypatch):
    """T3：方向歧义但状态机明确禁开 → htf_state_lock（不再记 gate_4h）"""
    monkeypatch.setattr(sm, "htf_context", lambda *a, **k: {
        "state": "turn_bull", "state_label": "多头拐头", "direction": None,
        "dir": 0, "block_both": True, "reject_reason": "undetermined",
        "evidence": {"close_vs_ema144": 0.4, "slope_pct": 0.2, "adx": 16.0},
    })
    rv: dict = {}
    cand, _op = sm.detect_indicator_candidate(
        pool, "XUSDT", pool.k1, flt=resolve_scan_filters(None), reject_out=rv)
    assert cand is None
    assert rv["stage"] == "htf_state_lock"
    assert rv["detail"] == "多头拐头"


def test_tangle_still_records_gate_4h(pool, monkeypatch):
    """T3 对照：tangle 无方向语义，仍记 gate_4h（不被状态锁抢走）"""
    monkeypatch.setattr(sm, "htf_context", lambda *a, **k: {
        "state": "tangle", "state_label": "三线纠缠", "direction": None,
        "dir": 0, "block_both": True, "reject_reason": "tangle",
        "evidence": {"close_vs_ema144": 0.1, "slope_pct": 0.1, "adx": 12.0},
    })
    rv: dict = {}
    cand, _op = sm.detect_indicator_candidate(
        pool, "XUSDT", pool.k1, flt=resolve_scan_filters(None), reject_out=rv)
    assert cand is None
    assert rv["stage"] == "gate_4h"


def test_direction_flip_reachable_before_votes(pool, monkeypatch):
    """T4：3 张票全反向时 direction_flip 可达（此前被 votes 关先 return 掉）"""
    monkeypatch.setattr(sm, "compute_indicator_votes",
                        lambda *a, **k: {"bull_n": 0, "bear_n": 3, "net": -3})
    monkeypatch.setattr(sm, "detect_trigger", lambda *a, **k: (
        {"type": sm.BREAKOUT, "direction": 1, "position": "resistance",
         "breakout_pct": 0.5}, None))
    flt = replace(resolve_scan_filters(None),
                  direction_vote_gate_enabled=True,
                  direction_conflict_gate_enabled=True)
    rv: dict = {}
    cand, _op = sm.detect_indicator_candidate(
        pool, "XUSDT", pool.k1, flt=flt, reject_out=rv)
    assert cand is None
    assert rv["stage"] == "direction_flip"
    assert "3张反向" in (rv.get("detail") or "")
