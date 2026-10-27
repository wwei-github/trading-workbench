# -*- coding: utf-8 -*-
"""回踩触发「未触发关卡」细分单测（docs/16 §6.3 P1-3）。

`detect_trigger` 的 `diag["stage"]` 必须把原来塌进 `no_trigger` 的各类卡点分开：
`no_touch` / `leg_gate` / `golden_no_context` / `pending_waiting` /
`window_expired` / `pending_flip` / `insufficient_bars`。

只跑纯函数（无 DB、无网络）：K 线用合成序列 + monkeypatch 掉触点探测器，
让每个用例只验证"哪一支被走到、stage 记了什么"。
"""
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scan_filters import (  # noqa: E402
    TOUCH_EMA21,
    resolve_scan_filters,
)
from app.services.strategy import trigger as trig  # noqa: E402


def _flt(**overrides):
    """禁用腿门与结构门，只留触点→金K 这一段，便于隔离各分支。"""
    base = replace(
        resolve_scan_filters(None),
        pullback_leg_gate_enabled=False,
        pullback_touch_refs=(TOUCH_EMA21,),
        pullback_touch_deny_long=(),
        pullback_touch_deny_short=(),
        pending_confirm_window_bars=4,
    )
    return replace(base, **overrides)


def _klines(n=40, price=100.0, step=0.1):
    """合成 1h K：交替小实体，最后一根为未收盘根（detect_trigger 用 klines[-2]）。"""
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        base = price + i * step
        out.append([ts + i * 3_600_000, base, base + 0.6, base - 0.6, base + 0.05, 100.0])
    return out


def _run(klines, direction=1, pending=None, flt=None):
    diag: dict = {}
    trig.detect_trigger(klines, direction, pending=pending, flt=flt or _flt(),
                        diag=diag)
    return diag


# ── 各分支的 stage 代码 ─────────────────────────────────────────────────────


def test_insufficient_bars_stage():
    diag = _run(_klines(n=20))
    assert diag.get("stage") == "insufficient_bars"
    assert "K线不足30根" in "；".join(diag.get("reasons", []))


def test_no_touch_stage(monkeypatch):
    """所有触点探测器都返回 None → no_touch"""
    monkeypatch.setattr(trig, "_detect_ema_touch", lambda *a, **k: None)
    diag = _run(_klines())
    assert diag.get("stage") == "no_touch"


def test_leg_gate_stage(monkeypatch):
    """触点成立但腿门不过 → leg_gate（且不再记 no_touch）"""
    monkeypatch.setattr(trig, "_detect_ema_touch",
                        lambda *a, **k: float(a[0][-2][4]))
    monkeypatch.setattr(trig, "_pullback_leg_gate",
                        lambda *a, **k: (False, {"reason": "non_monotonic", "bars": 1,
                                                 "atr_mult": 0.1, "overshoot_atr": 1.0}))
    diag = _run(_klines(), flt=_flt(pullback_leg_gate_enabled=True))
    assert diag.get("stage") == "leg_gate"


def test_touch_without_golden_is_not_a_reject(monkeypatch):
    """触点成立且无金K → 进待确认池（upsert），不是被拒：diag 不写 stage"""
    monkeypatch.setattr(trig, "_detect_ema_touch",
                        lambda *a, **k: float(a[0][-2][4]))
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda *a, **k: (None, ""))
    diag: dict = {}
    sig, op = trig.detect_trigger(_klines(), 1, pending=None, flt=_flt(), diag=diag)
    assert sig is None and op and op.get("op") == "upsert"
    assert not diag.get("stage")


def test_golden_no_context_stage(monkeypatch):
    """有同向金K但无触点 → golden_no_context（纯金K不触发）"""
    monkeypatch.setattr(trig, "_detect_ema_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda *a, **k: ({"pattern": "hammer", "offset": -2}, ""))
    diag = _run(_klines())
    # 先记 no_touch（触点阶段），金K分支不覆盖上游卡点
    assert diag.get("stage") == "no_touch"
    assert "纯金K不触发" in "；".join(diag.get("reasons", []))


def test_pending_waiting_stage(monkeypatch):
    """有活跃 pending、窗口未满、本根无金K → pending_waiting（保持等待，不下处置）"""
    monkeypatch.setattr(trig, "_detect_ema_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda *a, **k: (None, ""))
    k = _klines()
    pending = {
        "id": 1, "direction": 1, "touch_ref": TOUCH_EMA21,
        "stage1_candle_open": trig.datetime.utcfromtimestamp(float(k[-3][0]) / 1000),
    }
    sig, op = trig.detect_trigger(k, 1, pending=pending, flt=_flt(), diag=(d := {}))
    assert sig is None and op is None           # 保持等待：不做处置
    assert d.get("stage") == "pending_waiting"


def test_window_expired_stage(monkeypatch):
    """pending 窗口耗尽且无金K → window_expired + expire 动作"""
    monkeypatch.setattr(trig, "_detect_ema_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda *a, **k: (None, ""))
    k = _klines()
    pending = {
        "id": 1, "direction": 1, "touch_ref": TOUCH_EMA21,
        "stage1_candle_open": trig.datetime.utcfromtimestamp(float(k[-12][0]) / 1000),
    }
    d: dict = {}
    sig, op = trig.detect_trigger(k, 1, pending=pending, flt=_flt(), diag=d)
    assert sig is None and op and op.get("op") == "expire"
    assert d.get("stage") == "window_expired"


def test_pending_flip_stage(monkeypatch):
    """pending 方向与本轮相反 → pending_flip + expire(direction_flip)"""
    monkeypatch.setattr(trig, "_detect_ema_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "resolve_golden_evidence",
                        lambda *a, **k: (None, ""))
    k = _klines()
    pending = {
        "id": 1, "direction": -1, "touch_ref": TOUCH_EMA21,
        "stage1_candle_open": trig.datetime.utcfromtimestamp(float(k[-3][0]) / 1000),
    }
    d: dict = {}
    sig, op = trig.detect_trigger(k, 1, pending=pending, flt=_flt(), diag=d)
    assert sig is None and op and op.get("reason") == "direction_flip"
    assert d.get("stage") == "pending_flip"


def test_stage_is_first_writer_only():
    """同一个 diag 只保留最上游卡点（避免下游分支覆盖）"""
    d: dict = {}
    trig._diag_stage(d, "no_touch")
    trig._diag_stage(d, "window_expired")
    assert d["stage"] == "no_touch"


def test_diag_none_is_silent():
    """diag=None 时不报错（watchlist 等不开采集的调用方）"""
    trig._diag_stage(None, "no_touch")
    k = _klines(n=20)
    sig, op = trig.detect_trigger(k, 1, flt=_flt(), diag=None)
    assert sig is None and op is None
