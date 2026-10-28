# -*- coding: utf-8 -*-
"""逐轮漏斗快照批量接口（2026-10-28，docs/需求文档/18 最新筛选）。

「最新筛选」Tab 底部时序区一次画 24 根柱：每轮各关卡拦截数 + 该轮命中数。
逐轮调 /scans/{id}/funnel 会发 24 次请求，故新增 GET /scans/funnels?hours=24。

本文件锁定三件事：
1. 时间窗（只回近 N 小时）与升序（前端按时间正序画柱）；
2. funnel_json 为 NULL 的旧轮次不炸、回落 0/{}；
3. hit_count 按 scan_results 聚合（不是 scan_records.hit_count 列——那个是
   写库时快照，历史轮次可能与实际行数不一致，这里以实际行数为准）。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.scan import scan_funnels  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import ScanRecord, ScanResult  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()


def _scan(db, *, hours_ago=0.0, funnel=None):
    rec = ScanRecord(
        id=uuid.uuid4(),
        scan_type="scheduled",
        status="completed",
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
        funnel_json=funnel,
    )
    db.add(rec)
    db.commit()
    return rec


def _result(db, scan_id, symbol="BTCUSDT"):
    db.add(ScanResult(
        id=uuid.uuid4(),
        scan_record_id=scan_id,
        symbol=symbol,
        signal_type="trigger_pullback_confirm",
        current_price=100.0,
        breakout_pct=0.0,
        trend_slope=0.0,
        r_squared=0.9,
    ))
    db.commit()


FUNNEL = {
    "universe": 518,
    "scanned": 290,
    "errors": 0,
    "candidates": 0,
    "pending_pool": 14,
    "stages": {"gate_4h": 126, "htf_state_lock": 82},
    "htf_states": {"tangle": 100},
    "gate_4h_reasons": {"tangle": 83},
    "rejects": {"ETHUSDT": "gate_4h"},
    "details": {"ETHUSDT": "4h状态机空头拐头"},
}


def test_returns_items_ascending_and_maps_fields(db):
    older = _scan(db, hours_ago=3, funnel=FUNNEL)
    newer = _scan(db, hours_ago=1, funnel=FUNNEL)

    out = scan_funnels(hours=24, db=db)

    assert out["count"] == 2
    assert [i["scan_id"] for i in out["items"]] == [str(older.id), str(newer.id)]
    item = out["items"][-1]
    assert item["universe"] == 518
    assert item["scanned"] == 290
    assert item["candidates"] == 0
    assert item["pending_pool"] == 14
    assert item["stages"] == {"gate_4h": 126, "htf_state_lock": 82}
    assert item["htf_states"] == {"tangle": 100}
    assert item["gate_4h_reasons"] == {"tangle": 83}
    # 时序区不需要明细：不把 rejects/details 带进批量响应（省体积）
    assert "rejects" not in item and "details" not in item


def test_window_filters_old_scans(db):
    _scan(db, hours_ago=30, funnel=FUNNEL)
    fresh = _scan(db, hours_ago=2, funnel=FUNNEL)

    out = scan_funnels(hours=24, db=db)

    assert out["count"] == 1
    assert out["items"][0]["scan_id"] == str(fresh.id)


def test_null_funnel_falls_back_without_crash(db):
    rec = _scan(db, hours_ago=1, funnel=None)

    item = scan_funnels(hours=24, db=db)["items"][0]

    assert item["scan_id"] == str(rec.id)
    assert item["universe"] == 0 and item["scanned"] == 0
    assert item["stages"] == {} and item["htf_states"] == {}
    assert item["hit_count"] == 0


def test_hit_count_aggregates_scan_results(db):
    rec = _scan(db, hours_ago=1, funnel=FUNNEL)
    _result(db, rec.id, "BTCUSDT")
    _result(db, rec.id, "ETHUSDT")
    other = _scan(db, hours_ago=2, funnel=FUNNEL)
    _result(db, other.id, "SOLUSDT")

    items = {i["scan_id"]: i for i in scan_funnels(hours=24, db=db)["items"]}

    assert items[str(rec.id)]["hit_count"] == 2
    assert items[str(other.id)]["hit_count"] == 1


def test_hours_guard_and_empty(db):
    assert scan_funnels(hours=24, db=db)["items"] == []
