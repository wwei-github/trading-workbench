# -*- coding: utf-8 -*-
"""战法 CRUD 接口回归（/api/tactics，2026-10-02）。

sqlite 内存库 + dependency_overrides 注入会话，不触真库不触网。

覆盖：
1. 创建/列表（trade_count 聚合）/ 更新（改名查重）
2. 重名 400、非法 direction/scope 422、description 超长 422
3. 启停 toggle；被引用战法删除 409（提示停用）、无引用可删
"""
import sys
import uuid
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
from app.models.scan import AIAnalysis, ScanRecord, ScanResult  # noqa: E402
from app.models.tactic import Tactic  # noqa: E402


@pytest.fixture()
def client():
    # TestClient 在工作线程发请求：check_same_thread=False + StaticPool 共享内存库
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


def _payload(**over) -> dict:
    base = {
        "name": "EMA20回踩做多",
        "description": "多头排列回踩 EMA20 企稳收阳，止损放 EMA20 下方",
        "direction": "long",
        "scope": "testnet",
    }
    base.update(over)
    return base


def test_create_list_update(client):
    http, db = client
    r = http.post("/api/tactics", json=_payload())
    assert r.status_code == 200
    tid = r.json()["id"]
    assert r.json()["enabled"] is False  # 新建默认停用
    assert r.json()["trade_count"] == 0

    # 列表 + trade_count 聚合（造一条 AIAnalysis 引用）
    db.add(ScanRecord(scan_type="scheduled"))
    db.flush()
    sr = ScanResult(
        scan_record_id=db.query(ScanRecord).first().id,
        symbol="TESTUSDT", signal_type="trigger_pullback_confirm",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.0,
    )
    db.add(sr)
    db.flush()
    db.add(AIAnalysis(
        scan_result_id=sr.id, symbol="TESTUSDT",
        trade_decision="suggest", tactic_name="EMA20回踩做多",
    ))
    db.commit()
    lst = http.get("/api/tactics").json()
    assert len(lst) == 1 and lst[0]["trade_count"] == 1

    # 更新：改名 + 改环境
    r2 = http.put(f"/api/tactics/{tid}", json=_payload(name="EMA55深回撤", scope="both"))
    assert r2.status_code == 200
    assert r2.json()["name"] == "EMA55深回撤" and r2.json()["scope"] == "both"


def test_create_duplicate_name_and_validation(client):
    http, _ = client
    assert http.post("/api/tactics", json=_payload()).status_code == 200
    # 重名 400
    r = http.post("/api/tactics", json=_payload())
    assert r.status_code == 400 and "已存在" in r.json()["detail"]
    # 非法 direction/scope 422
    assert http.post("/api/tactics", json=_payload(direction="up")).status_code == 422
    assert http.post("/api/tactics", json=_payload(scope="mainnet")).status_code == 422
    # description 超长 422（>2000）
    long_desc = "多" * 2001
    assert http.post("/api/tactics", json=_payload(name="B", description=long_desc)).status_code == 422


def test_toggle_and_delete_guards(client):
    http, db = client
    tid = http.post("/api/tactics", json=_payload()).json()["id"]
    # 启停
    r = http.patch(f"/api/tactics/{tid}/enabled", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # 造引用后删除 → 409
    db.add(ScanRecord(scan_type="scheduled"))
    db.flush()
    sr = ScanResult(
        scan_record_id=db.query(ScanRecord).first().id,
        symbol="TESTUSDT", signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.0,
    )
    db.add(sr)
    db.flush()
    db.add(AIAnalysis(
        scan_result_id=sr.id, symbol="TESTUSDT",
        trade_decision="suggest", tactic_name="EMA20回踩做多",
    ))
    db.commit()
    r2 = http.delete(f"/api/tactics/{tid}")
    assert r2.status_code == 409 and "停用" in r2.json()["detail"]
    # 无引用战法可删
    tid2 = http.post("/api/tactics", json=_payload(name="未引用战法")).json()["id"]
    assert http.delete(f"/api/tactics/{tid2}").status_code == 200
    # 不存在 404
    assert http.delete(f"/api/tactics/{uuid.uuid4()}").status_code == 404
