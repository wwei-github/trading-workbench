# -*- coding: utf-8 -*-
"""质量分下限（docs/16 §5.1 P0-1）单测。

覆盖：配置解析（默认 0 / DB 覆盖 / env 覆盖）、过滤函数（边界、缺分、全留、全剔）、
API 校验（接受 0~110、拒绝越界与非数值）。
只跑纯函数与解析，不触网、不连库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api.scan import _validate_scan_filters  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402
from app.services.scanner import apply_quality_floor  # noqa: E402


class _Cfg:
    """最小 SystemConfig 替身：只给 resolve_scan_filters 读的 scan_filters/顶层列"""

    scan_filters = None
    swing_order = None

    def __init__(self, scan_filters=None):
        self.scan_filters = scan_filters


# ── 配置解析 ────────────────────────────────────────────────────────────────


def test_default_floor_is_off(monkeypatch):
    """未配任何值 → 0（不过滤），默认零行为变化"""
    monkeypatch.setattr(settings, "QUALITY_FLOOR", 0.0)
    assert resolve_scan_filters(_Cfg()).quality_floor == 0.0


def test_db_value_overrides_env(monkeypatch):
    """scan_filters.quality_floor 优先于 env"""
    monkeypatch.setattr(settings, "QUALITY_FLOOR", 70.0)
    flt = resolve_scan_filters(_Cfg({"quality_floor": 85}))
    assert flt.quality_floor == 85.0


def test_env_value_used_when_db_absent(monkeypatch):
    """DB 无该键 → 回落 env（P0 先发代码、后开开关的用法）"""
    monkeypatch.setattr(settings, "QUALITY_FLOOR", 80.0)
    assert resolve_scan_filters(_Cfg({})).quality_floor == 80.0


def test_invalid_db_value_falls_back(monkeypatch):
    """脏值（负数/非数值）不得生效，回落 env 或默认"""
    monkeypatch.setattr(settings, "QUALITY_FLOOR", 0.0)
    assert resolve_scan_filters(_Cfg({"quality_floor": -5})).quality_floor == 0.0
    assert resolve_scan_filters(_Cfg({"quality_floor": "abc"})).quality_floor == 0.0


# ── 过滤 ────────────────────────────────────────────────────────────────────


def _hit(symbol: str, score):
    return {"symbol": symbol, "quality_score": score, "volume_24h": 1.0}


def test_floor_zero_keeps_everything():
    hits = [_hit("AAAUSDT", 10), _hit("BBBUSDT", 0)]
    kept, dropped = apply_quality_floor(hits, 0)
    assert kept == hits and dropped == []


def test_floor_drops_low_scores_and_reports_symbols():
    hits = [_hit("HI", 92), _hit("MID", 80), _hit("LO", 79), _hit("VERYLO", None)]
    kept, dropped = apply_quality_floor(hits, 80)
    assert [h["symbol"] for h in kept] == ["HI", "MID"]   # 等于下限保留
    assert dropped == ["LO", "VERYLO"]                     # 缺分按 0 处理


def test_floor_all_dropped():
    kept, dropped = apply_quality_floor([_hit("A", 12), _hit("B", 55)], 80)
    assert kept == [] and dropped == ["A", "B"]


def test_floor_empty_input():
    assert apply_quality_floor([], 80) == ([], [])


# ── API 校验 ────────────────────────────────────────────────────────────────


def test_api_accepts_in_range():
    assert _validate_scan_filters({"quality_floor": 80})["quality_floor"] == 80.0
    assert _validate_scan_filters({"quality_floor": 0})["quality_floor"] == 0.0
    assert _validate_scan_filters({"quality_floor": 110})["quality_floor"] == 110.0


def test_api_rejects_out_of_range():
    with pytest.raises(HTTPException) as ei:
        _validate_scan_filters({"quality_floor": 111})
    assert ei.value.status_code == 400


def test_api_rejects_non_numeric():
    with pytest.raises(HTTPException):
        _validate_scan_filters({"quality_floor": "high"})
