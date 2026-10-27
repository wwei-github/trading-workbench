"""回踩触点方向禁用表单测（2026-10-27，docs/需求文档/15 §16）

覆盖：
- `ScanFilters.touch_refs_for`：多头/空头各自禁用、空表不禁用、全禁回落通用白名单；
- `resolve_scan_filters`：DB list 优先、env 逗号串兜底、非法项静默丢弃；
- `trigger.detect_trigger` 接线：多头禁 ema21 时不再探测该触点，空头仍探测；
- API 校验：合法列表通过、未知名拒绝、空列表按「不禁用」通过。

背景（正式网 331 笔回踩单复核）：多头×ema21 44 笔 -16.67R（早段 -6.09 /
近段 -10.58 / 现规则 -6.94，三期皆负，跨 37 个币种）；空头×ema21 42 笔
+8.17R（近段 +13.18 / 现规则 +14.11）——同一根均线在两侧的信息量不同，
只能按方向禁用，不能全局收窄白名单。
"""
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.scan import _validate_scan_filters  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.scan_filters import (  # noqa: E402
    TOUCH_EMA21,
    TOUCH_EMA21_4H,
    TOUCH_RETEST,
    TOUCH_SWING,
    resolve_scan_filters,
    ScanFilters,
)
from app.services.strategy import trigger as trig  # noqa: E402

ALL6 = (TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA21_4H, "ema55", "ema144")


def _flt(**overrides) -> ScanFilters:
    base = replace(
        resolve_scan_filters(None),
        pullback_touch_refs=ALL6,
        pullback_touch_deny_long=(),
        pullback_touch_deny_short=(),
    )
    return replace(base, **overrides)


# ── touch_refs_for ─────────────────────────────────────────────────────────
def test_deny_empty_keeps_all_refs():
    flt = _flt()
    assert flt.touch_refs_for(1) == ALL6
    assert flt.touch_refs_for(-1) == ALL6


def test_deny_long_drops_ema21_only_for_long():
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,))
    assert TOUCH_EMA21 not in flt.touch_refs_for(1)
    assert set(flt.touch_refs_for(1)) == set(ALL6) - {TOUCH_EMA21}
    # 空头不受影响（+8.17R 一侧保留）
    assert flt.touch_refs_for(-1) == ALL6


def test_deny_short_is_mirror():
    flt = _flt(pullback_touch_deny_short=(TOUCH_EMA21_4H,))
    assert flt.touch_refs_for(1) == ALL6
    assert TOUCH_EMA21_4H not in flt.touch_refs_for(-1)


def test_deny_all_falls_back_to_base():
    """全禁不让回踩管线停摆（回落通用白名单，与 `_touch_refs` 全空同一口径）。"""
    flt = _flt(pullback_touch_deny_long=ALL6)
    assert flt.touch_refs_for(1) == ALL6


def test_deny_unknown_ref_is_noop():
    flt = _flt(pullback_touch_deny_long=("not_a_touch",))
    assert flt.touch_refs_for(1) == ALL6


# ── resolve_scan_filters：DB 优先 / env 兜底 ───────────────────────────────
def test_resolve_env_csv(monkeypatch):
    monkeypatch.setattr(settings, "PULLBACK_TOUCH_DENY_LONG", "ema21, bogus ,")
    monkeypatch.setattr(settings, "PULLBACK_TOUCH_DENY_SHORT", "")
    flt = resolve_scan_filters(None)
    assert flt.pullback_touch_deny_long == (TOUCH_EMA21,)
    assert flt.pullback_touch_deny_short == ()


def test_resolve_db_list_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "PULLBACK_TOUCH_DENY_LONG", "ema55")
    cfg = SimpleNamespace(scan_filters={"pullback_touch_deny_long": [TOUCH_EMA21, "bad"]})
    flt = resolve_scan_filters(cfg)
    assert flt.pullback_touch_deny_long == (TOUCH_EMA21,)


def test_resolve_db_bad_type_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(settings, "PULLBACK_TOUCH_DENY_LONG", "ema21")
    cfg = SimpleNamespace(scan_filters={"pullback_touch_deny_long": "ema21"})
    flt = resolve_scan_filters(cfg)
    assert flt.pullback_touch_deny_long == (TOUCH_EMA21,)


def test_resolve_db_empty_list_means_disabled():
    """显式空列表 = 不禁用（覆盖 env，用于单方向回退）。"""
    cfg = SimpleNamespace(scan_filters={"pullback_touch_deny_long": []})
    flt = resolve_scan_filters(cfg)
    assert flt.pullback_touch_deny_long == ()


# ── trigger 接线 ──────────────────────────────────────────────────────────
TS0 = 1716000000_000
STEP = 3_600_000


def _flat_klines(n: int = 60) -> list[list]:
    out = []
    prev = 100.0
    for i in range(n):
        c = 100.0
        out.append([TS0 + i * STEP, prev, c + 0.3, c - 0.3, c, 100.0, 1e6])
        prev = c
    return out


@pytest.fixture
def probe(monkeypatch):
    """把触点探测全部打桩，记录被探测到的 (方向, 周期)。"""
    calls: list[tuple[int, int]] = []

    def _ema_touch(klines, direction, period, flt, htf_dir=None):
        calls.append((direction, period))
        return None

    monkeypatch.setattr(trig, "_detect_ema_touch", _ema_touch)
    monkeypatch.setattr(trig, "_detect_retest", lambda *a, **k: None)
    monkeypatch.setattr(trig, "_detect_swing_touch", lambda *a, **k: None)
    monkeypatch.setattr(trig, "_detect_htf_ema21_touch", lambda *a, **k: None)
    monkeypatch.setattr(
        trig, "structure_1h",
        lambda k1, direction, cfg=None: ({"d21": 1.0}, {"aligned": True}),
    )
    return calls


def test_long_denied_ema21_not_probed(probe):
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,))
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        trig.detect_trigger(_flat_klines(), 1, flt=flt)
    assert 21 not in [p for d, p in probe if d == 1], "多头不应探测 ema21"
    assert 55 in [p for d, p in probe if d == 1] and 144 in [p for d, p in probe if d == 1]


def test_short_still_probes_ema21(probe):
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,))
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        trig.detect_trigger(_flat_klines(), -1, flt=flt)
    assert 21 in [p for d, p in probe if d == -1], "空头侧 ema21 必须保留"


def test_no_deny_probes_both_sides(probe):
    flt = _flt()
    with patch.object(trig, "detect_all_patterns", return_value=[]):
        trig.detect_trigger(_flat_klines(), 1, flt=flt)
        trig.detect_trigger(_flat_klines(), -1, flt=flt)
    assert 21 in [p for d, p in probe if d == 1]
    assert 21 in [p for d, p in probe if d == -1]


# ── API 校验 ──────────────────────────────────────────────────────────────
def test_api_accepts_deny_list():
    out = _validate_scan_filters({"pullback_touch_deny_long": [TOUCH_EMA21]})
    assert out["pullback_touch_deny_long"] == [TOUCH_EMA21]


def test_api_accepts_empty_list():
    out = _validate_scan_filters({"pullback_touch_deny_short": []})
    assert out["pullback_touch_deny_short"] == []


def test_api_rejects_unknown_touch():
    with pytest.raises(HTTPException) as ei:
        _validate_scan_filters({"pullback_touch_deny_long": ["ema9"]})
    assert ei.value.status_code == 400


def test_api_rejects_non_list():
    with pytest.raises(HTTPException):
        _validate_scan_filters({"pullback_touch_deny_long": "ema21"})


# ── 存量 pending 复核（2026-10-27：禁用后不得再从待确认漏进来） ─────────────
def _pending_for(idx: int, direction: str = "long", touch_ref: str = TOUCH_EMA21) -> dict:
    from datetime import datetime as _dt
    ts = _flat_klines()[idx][0]
    return {
        "id": 1, "direction": direction, "touch_ref": touch_ref,
        "stage1_candle_open": _dt.utcfromtimestamp(ts / 1000),
    }


def _hammer() -> list[dict]:
    return [{"pattern": "hammer", "strength": 3, "offset": -2,
             "direction": "bullish"}]


def test_pending_with_denied_ref_not_confirmed():
    """多头禁 ema21 后，既有 ema21 pending 不能再确认（否则最多漏 4 根）。"""
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,),
               pullback_leg_gate_enabled=False)
    with patch.object(trig, "detect_all_patterns", return_value=_hammer()):
        t, _op = trig.detect_trigger(
            _flat_klines(), 1, pending=_pending_for(-3), flt=flt)
    assert t is None


def test_pending_with_allowed_ref_still_confirms():
    """对照组：不禁用时同一 pending 正常确认（保证上面那条不是被别的门拦下）。"""
    flt = _flt(pullback_touch_deny_short=(TOUCH_SWING,),
               pullback_leg_gate_enabled=False)
    with patch.object(trig, "detect_all_patterns", return_value=_hammer()):
        t, _op = trig.detect_trigger(
            _flat_klines(), 1, pending=_pending_for(-3), flt=flt)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == TOUCH_EMA21


def test_pending_short_ema21_still_confirms_under_long_deny():
    """空头侧保留：多禁 ema21 不影响空头的 ema21 pending。"""
    flt = _flt(pullback_touch_deny_long=(TOUCH_EMA21,),
               pullback_leg_gate_enabled=False)
    bear = [{"pattern": "bearish_engulfing", "strength": 3, "offset": -2,
             "direction": "bearish"}]
    with patch.object(trig, "detect_all_patterns", return_value=bear):
        t, _op = trig.detect_trigger(
            _flat_klines(), -1,
            pending=_pending_for(-3, direction="short"), flt=flt)
    assert t is not None and t["touch_ref"] == TOUCH_EMA21
