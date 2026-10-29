# -*- coding: utf-8 -*-
"""观察期任务（docs/23 §3）：登记 / 到期扫描 / 决策 / 卡片格式。

只测服务与接口口径，不触网（飞书未配置时静默跳过）。
"""
import sys
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
from app.services import observations as svc  # noqa: E402


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
        yield session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), session
    app.dependency_overrides.pop(get_db, None)
    session.close()


@pytest.fixture(autouse=True)
def _no_feishu(monkeypatch):
    """单测不推飞书（卡片格式单独断言，不依赖网络/凭证）"""
    monkeypatch.setattr(svc.feishu_bot, "configured", lambda: False)


def test_create_and_list(client):
    http, _db = client
    body = {
        "name": "走平门按 ATR 归一（影子）",
        "days": 7,
        "config_key": "slope_flat_atr_mult",
        "decision_rule": "差集 avgR ≥ +0.1R 且 SPY/XAUT/MSFT 未被误放",
        "metrics": {"flat_abs_n": 304, "diff_avgR": 0.06},
    }
    r = http.post("/api/observations", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "pending"
    assert created["name"] == body["name"]

    r = http.get("/api/observations")
    assert r.status_code == 200
    data = r.json()
    assert data["counts"]["pending"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["decision_rule"] == body["decision_rule"]


def test_due_scan_marks_due_once(client):
    """到期扫描：pending 且 due_at<=now → due；重复扫描不再返回（幂等）"""
    _http, db = client
    t = svc.create(db, name="腿门分档", days=7,
                   decision_rule="0.3 档 avgR ≥ 0.1R")
    # 手动把到期时间拨到过去
    t.due_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()

    first = svc.check_due(db)
    assert [x.id for x in first] == [t.id]
    db.refresh(t)
    assert t.status == "due"

    second = svc.check_due(db)
    assert second == []


def test_decide_moves_to_decided(client):
    http, db = client
    t = svc.create(db, name="数据缺失 fail-open", days=14,
                   decision_rule="降级占比 >1% 的轮次 ≥ 3")
    r = http.post(f"/api/observations/{t.id}/decide",
                  json={"decision": "保持关闭：样本不足", "note": "仅 1 轮降级"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "decided"
    assert r.json()["decision"].startswith("保持关闭")

    # 默认列表只含未决策项
    assert http.get("/api/observations").json()["items"] == []
    # 显式查 decided
    r = http.get("/api/observations", params={"status": "decided"})
    assert len(r.json()["items"]) == 1


def test_decide_unknown_id_404(client):
    http, _db = client
    r = http.post("/api/observations/not-a-uuid/decide",
                  json={"decision": "保持现状：样本不足"})
    assert r.status_code == 404


def test_due_card_format(client):
    _http, db = client
    t = svc.create(db, name="走平门归一", days=7, config_key="slope_flat_atr_mult",
                   decision_rule="差集 avgR ≥ +0.1R",
                   metrics={"flat_abs_n": 304, "flat_norm_n": 155})
    card = svc.format_due_card([t])
    text = str(card)
    assert "观察期到期" in text
    assert "走平门归一" in text
    assert "差集 avgR" in text
    assert "flat_abs_n=304" in text
