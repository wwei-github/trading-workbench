# -*- coding: utf-8 -*-
"""命中引用装配（2026-10-27，docs/需求文档/15 §19）

覆盖 `services/hit_ref.py` 的口径：信号K线归属（尾窗/非尾窗）、命中方向派生优先级、
触碰K线反查、单条载荷形状与滞后告警阈值，以及 `ScanResultOut` 能否承载派生字段。
"""
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.config import settings
from app.schemas.scan import ScanResultOut
from app.services import hit_ref
from app.services import market_data as md


def _row(**kw):
    base = dict(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        scan_record_id=UUID("33333333-3333-3333-3333-333333333333"),
        symbol="BTCUSDT",
        current_price=80000.0,
        breakout_pct=1.2,
        r_squared=0.42,
        volume=1234.5,
        volume_24h=9876543.21,
        volume_type="normal",
        signal_type="trigger_pullback_confirm",
        position="support",
        created_at=datetime(2026, 10, 27, 3, 30),
        signal_created_at=None,
        votes_json={"net": 0},
        trend_slope=0.0,
        pattern="hammer",
        golden_combo=False,
        pattern_candle_open=None,
        entry_lag_pct=None,
        touch_ref="swing",
        position_ref="摆动低点",
        quality_score=88,
        is_repeat=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ===== 信号K线归属 =====


def test_signal_bar_previous_hour_outside_tail(monkeypatch):
    """非尾窗（:30 扫描）：信号根 = 上一根已收盘根"""
    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", 0)
    assert hit_ref.signal_candle_open(_row()) == datetime(2026, 10, 27, 2, 0)


def test_signal_bar_current_hour_inside_tail(monkeypatch):
    """尾窗（:57 扫描，幽灵尾根被视作信号根）：信号根 = 该整点根"""
    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", 5)
    r = _row(created_at=datetime(2026, 10, 27, 3, 57))
    assert hit_ref.signal_candle_open(r) == datetime(2026, 10, 27, 3, 0)


def test_signal_bar_prefers_signal_created_at(monkeypatch):
    """signal_created_at 优先于 created_at（候选产生时刻 vs 扫描完成时刻）"""
    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", 0)
    r = _row(
        created_at=datetime(2026, 10, 27, 3, 59),
        signal_created_at=datetime(2026, 10, 27, 3, 2),
    )
    assert hit_ref.signal_candle_open(r) == datetime(2026, 10, 27, 2, 0)


def test_signal_bar_none_when_no_timestamp():
    r = _row(created_at=None)
    assert hit_ref.signal_candle_open(r) is None


# ===== 方向派生 =====


@pytest.mark.parametrize(
    "signal_type,position,expected",
    [
        ("trigger_pullback_confirm", "support", 1),      # 回踩支撑企稳 = 做多
        ("trigger_pullback_confirm", "resistance", -1),  # 回踩压力受阻 = 做空
        ("trigger_breakout", "resistance", 1),           # 向上突破 = 做多
        ("trigger_breakout", "support", -1),             # 向下破位 = 做空
    ],
)
def test_direction_by_position(signal_type, position, expected):
    """position 有值时以它为准（不能用 votes.net：回踩确认逆 1h 方向票）"""
    r = _row(signal_type=signal_type, position=position, votes_json={"net": -3})
    assert hit_ref.hit_direction(r) == expected


def test_direction_falls_back_to_votes_net():
    r = _row(position=None, votes_json={"net": -2})
    assert hit_ref.hit_direction(r) == -1


def test_direction_falls_back_to_slope_and_unknown():
    assert hit_ref.hit_direction(_row(position=None, votes_json={}, trend_slope=0.4)) == 1
    assert hit_ref.hit_direction(_row(position=None, votes_json={}, trend_slope=-0.4)) == -1
    assert hit_ref.hit_direction(_row(position=None, votes_json={}, trend_slope=0)) == 0


# ===== 触碰K线 / 载荷 =====


def test_stage1_map_empty_ids_skips_query():
    assert hit_ref.stage1_map(object(), []) == {}
    assert hit_ref.stage1_map(object(), [None]) == {}


def test_stage1_map_reads_rows():
    sid = UUID("22222222-2222-2222-2222-222222222222")
    s1 = datetime(2026, 10, 27, 1, 0)

    class _Q:
        def all(self):
            return [(sid, s1)]

    class _Db:
        def execute(self, _stmt):
            return _Q()

    assert hit_ref.stage1_map(_Db(), [sid]) == {sid: s1}


def test_entry_lag_warn_threshold():
    assert hit_ref.entry_lag_warn(_row(entry_lag_pct=None)) is False
    assert hit_ref.entry_lag_warn(_row(entry_lag_pct=settings.COMBO_LAG_WARN_PCT - 0.01)) is False
    assert hit_ref.entry_lag_warn(_row(entry_lag_pct=settings.COMBO_LAG_WARN_PCT)) is True


def test_payload_shape(monkeypatch):
    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", 0)
    p = hit_ref.payload(_row(), datetime(2026, 10, 27, 1, 0))
    assert p["id"] == "11111111-1111-1111-1111-111111111111"
    assert p["signal_candle_open"] == "2026-10-27T02:00:00"
    assert p["stage1_candle_open"] == "2026-10-27T01:00:00"
    assert p["direction"] == 1
    assert p["touch_ref"] == "swing"
    assert p["quality_score"] == 88
    assert p["entry_lag_warn"] is False


def test_payload_without_stage1():
    p = hit_ref.payload(_row(signal_type="trigger_breakout", position="resistance"))
    assert p["stage1_candle_open"] is None
    assert p["direction"] == 1


def test_payload_for_none_id():
    assert hit_ref.payload_for(object(), None) is None


# ===== ScanResultOut 承载派生字段 =====


def test_scan_result_out_carries_derived_fields(monkeypatch):
    """model_validate → model_copy(update=...) 这条路径要能带上派生字段"""
    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", 0)
    out = ScanResultOut.model_validate(_row())
    assert out.signal_candle_open is None and out.direction is None  # 派生字段默认空
    filled = out.model_copy(
        update={
            "signal_candle_open": hit_ref.signal_candle_open(_row()),
            "stage1_candle_open": datetime(2026, 10, 27, 1, 0),
            "direction": 1,
            "golden_combo": False,
        }
    )
    assert filled.signal_candle_open == datetime(2026, 10, 27, 2, 0)
    assert filled.stage1_candle_open == datetime(2026, 10, 27, 1, 0)
    assert filled.direction == 1
