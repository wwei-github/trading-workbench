# -*- coding: utf-8 -*-
"""配置变更可追溯（docs/23 T14a，2026-10-29）。

覆盖：
1. GET /api/scans/config 返回 updated_at 水位（新库首行写入即有值）
2. PUT 改配置 → updated_at 前进 + /api/scans/config/changes 落一条逐字段审计
   （field/old_value/new_value/source）
3. 同值重复 PUT → 不产生重复审计行、水位不前移（避免无变更也刷新时间）
4. 审计列表支持按 field 过滤，且时间倒序
"""
import sys
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


def test_get_config_exposes_updated_at(client):
    """配置行带 updated_at 水位（字段存在；新库首行写入即非空）"""
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert "updated_at" in body
    assert body["updated_at"] is not None


def test_put_config_writes_audit_and_bumps_updated_at(client):
    """PUT 改开关 → 水位刷新 + 逐字段审计（旧值→新值 + 来源）"""
    http, _db = client
    before = http.get("/api/scans/config").json()["updated_at"]

    r = http.put("/api/scans/config", json={"tp_full_exit_enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["tp_full_exit_enabled"] is True
    assert body["updated_at"] is not None
    assert body["updated_at"] >= before  # 同一事务内刷新

    changes = http.get("/api/scans/config/changes").json()
    assert changes["total"] >= 1
    rows = [i for i in changes["items"] if i["field"] == "tp_full_exit_enabled"]
    assert rows, "改开关必须落一条审计"
    row = rows[0]
    assert row["old_value"] == "关"   # 新库默认 False
    assert row["new_value"] == "开"
    assert row["source"]              # 来源 IP/客户端标识非空


def test_same_value_put_does_not_duplicate_audit(client):
    """同值重复保存 → 不新增审计行，水位不前移（无变更不刷时间）"""
    http, _db = client
    http.put("/api/scans/config", json={"tp_full_exit_enabled": True})
    first = http.get("/api/scans/config").json()["updated_at"]
    total_before = http.get("/api/scans/config/changes").json()["total"]

    r = http.put("/api/scans/config", json={"tp_full_exit_enabled": True})
    assert r.status_code == 200
    assert r.json()["updated_at"] == first
    assert http.get("/api/scans/config/changes").json()["total"] == total_before


def test_config_changes_filter_by_field(client):
    """审计列表按字段过滤，且同字段多行按时间倒序（最新在前）"""
    http, _db = client
    http.put("/api/scans/config", json={"tp_full_exit_enabled": True})
    http.put("/api/scans/config", json={"tp_full_exit_enabled": False})
    http.put("/api/scans/config", json={"tp2_enabled": True})

    filtered = http.get(
        "/api/scans/config/changes", params={"field": "tp_full_exit_enabled"}
    ).json()
    assert filtered["total"] == 2
    assert {i["field"] for i in filtered["items"]} == {"tp_full_exit_enabled"}
    newest = filtered["items"][0]
    assert newest["new_value"] == "关"  # 最后一次改成 False
    assert newest["old_value"] == "开"
    assert filtered["items"][-1]["new_value"] == "开"
