# -*- coding: utf-8 -*-
"""逐轮漏斗快照接口（GET /api/scans/funnels）字段回归。

docs/23 T10（2026-10-29）：最新筛选吸顶条要展示「4h 门拒因分布 + 本轮 4h 降级」，
接口此前只透出 htf_states / gate_4h_reasons，降级币数没出来（funnel_json 里其实
已有 htf_degraded 计数）。本测试锁住字段透出与旧轮次兼容（无键 → null）。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.scan import ScanRecord  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()

    def _override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), session
    app.dependency_overrides.pop(get_db, None)
    session.close()


def _add_scan(db, funnel: dict, *, scan_type: str = "scheduled") -> ScanRecord:
    rec = ScanRecord(
        id=uuid.uuid4(),
        scan_type=scan_type,
        status="completed",
        coin_count=100,
        created_at=datetime.utcnow() - timedelta(minutes=5),
        funnel_json=funnel,
    )
    db.add(rec)
    db.commit()
    return rec


def test_funnels_exposes_htf_degraded_and_reasons(client):
    """本轮 4h 降级币数与门内拒因分布都要透出（吸顶条 T10 数据源）"""
    http, db = client
    _add_scan(db, {
        "universe": 100, "scanned": 98, "errors": 2, "candidates": 3,
        "pending_pool": 1, "pending_active": 2,
        "stages": {"gate_4h": 9, "htf_state_lock": 4},
        "htf_states": {"tangle": 6, "aligned_bull": 3},
        "gate_4h_reasons": {"flat": 5, "tangle": 2, "undetermined": 1, "degraded": 1},
        "htf_degraded": 7,
        "htf_degraded_symbols": ["AAA", "BBB"],
    })

    body = http.get("/api/scans/funnels", params={"hours": 24}).json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["htf_degraded"] == 7
    assert item["gate_4h_reasons"] == {
        "flat": 5, "tangle": 2, "undetermined": 1, "degraded": 1}
    # 体积控制：降级币种清单不下发（下钻走单轮接口）
    assert "htf_degraded_symbols" not in item


def test_funnels_old_round_without_key_is_null(client):
    """旧轮次 funnel_json 无 htf_degraded → null（前端按「无数据」处理，不显示 0）"""
    http, db = client
    _add_scan(db, {
        "universe": 80, "scanned": 80, "errors": 0, "candidates": 0,
        "stages": {"gate_4h": 2},
        "htf_states": {"tangle": 2},
        "gate_4h_reasons": {"flat": 2},
    })

    item = http.get("/api/scans/funnels", params={"hours": 24}).json()["items"][0]
    assert item["htf_degraded"] is None


def test_funnels_skips_watchlist_rounds(client):
    """关注列表「立即扫描」不产 funnel_json，不得混进逐轮时序（幽灵柱）"""
    http, db = client
    _add_scan(db, {"universe": 1, "stages": {}}, scan_type="watchlist")
    assert http.get("/api/scans/funnels", params={"hours": 24}).json()["count"] == 0
