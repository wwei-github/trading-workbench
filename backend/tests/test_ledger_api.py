# -*- coding: utf-8 -*-
"""虚拟账本 API 回归（/api/ledger，docs/16 P-07）。

sqlite 内存库 + dependency_overrides 注入会话，不触真库不触网（backfill 的
.delay 单独 monkeypatch 掉）。

覆盖：
1. /stats decided 口径：OPEN/EXPIRED 不进分母（win_rate/exp_r 只算 DONE），
   source 过滤、四维分桶（signal_type/direction/quality 分位/rule_version）
2. /entries 筛选（symbol/direction/status/outcome/source/signal_type/日期窗）
   与分页排序
3. /backfill 校验 422（days 越界）、2h 内运行中同名任务 409、正常触发透传参数
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
from app.models.ledger import VirtualLedger  # noqa: E402
from app.models.ops import TaskRecord  # noqa: E402


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


def _row(**over) -> VirtualLedger:
    """最小合法 OPEN 行（终局字段按 over 覆盖）"""
    now = datetime.utcnow()
    base = dict(
        symbol="AAAUSDT", direction="long", source="live",
        signal_type="trigger_pullback_confirm",
        entry_price=100.0, stop_price=99.0, tp1_price=101.5, tp2_price=103.0,
        r_unit=1.0, entry_bar_open=now - timedelta(hours=12),
    )
    base.update(over)
    return VirtualLedger(**base)


def _seed(db):
    """5 行：3 DONE（2 胜 1 负）+ 1 OPEN + 1 EXPIRED，跨 live/backfill"""
    now = datetime.utcnow()
    rows = [
        # decided 主样本：win_tp1 / loss / win_tp2
        _row(symbol="AAAUSDT", status="DONE", outcome="win_tp1", r=0.5, bars=6,
             quality_score=75, rule_version="v1", touch_ref="swing",
             decided_at=now - timedelta(hours=10),
             entry_bar_open=now - timedelta(hours=36)),
        _row(symbol="BBBUSDT", direction="short", source="live",
             signal_type="trigger_breakout", status="DONE", outcome="loss",
             r=-1.13, bars=3, quality_score=55, rule_version="v1",
             decided_at=now - timedelta(hours=70),
             entry_bar_open=now - timedelta(hours=72)),
        _row(symbol="CCCUSDT", source="backfill", status="DONE",
             outcome="win_tp2", r=3.0, bars=20, quality_score=85,
             rule_version="v2", decided_at=now - timedelta(hours=90),
             entry_bar_open=now - timedelta(hours=96)),
        # 未决：OPEN / EXPIRED（outcome/r 均为 NULL，不进分母）
        _row(symbol="DDDUSDT", status="OPEN",
             entry_bar_open=now - timedelta(hours=12)),
        _row(symbol="EEEUSDT", source="backfill", status="EXPIRED",
             entry_bar_open=now - timedelta(hours=240)),
    ]
    for r in rows:
        db.add(r)
    db.commit()


def test_stats_decided_denominator_and_buckets(client):
    """P-04 口径：open/expired 单列不进 decided 分母；四维分桶正确"""
    http, db = client
    _seed(db)
    body = http.get("/api/ledger/stats").json()

    ov = body["overall"]
    assert ov["decided"] == 3 and ov["open"] == 1 and ov["expired"] == 1
    assert ov["wins"] == 2 and ov["losses"] == 1 and ov["be_exits"] == 0
    assert ov["win_rate"] == round(2 / 3, 4)
    assert ov["sum_r"] == round(0.5 - 1.13 + 3.0, 2)
    assert ov["exp_r"] == round((0.5 - 1.13 + 3.0) / 3, 4)

    # 分桶只含 decided 行
    st = {b["key"]: b for b in body["by_signal_type"]}
    assert set(st) == {"trigger_pullback_confirm", "trigger_breakout"}
    assert st["trigger_pullback_confirm"]["n"] == 2
    assert st["trigger_pullback_confirm"]["win_rate"] == 1.0
    assert st["trigger_pullback_confirm"]["sum_r"] == 3.5
    assert st["trigger_breakout"]["win_rate"] == 0.0

    dr = {b["key"]: b for b in body["by_direction"]}
    assert dr["long"]["n"] == 2 and dr["short"]["n"] == 1

    qb = {b["key"]: b for b in body["by_quality"]}
    assert set(qb) == {"70-79", "<60", "80+"}  # 未知/60-69 无行不出现
    assert qb["<60"]["n"] == 1 and qb["80+"]["n"] == 1

    rv = {b["key"]: b for b in body["by_rule_version"]}
    assert rv["v1"]["n"] == 2 and rv["v2"]["n"] == 1  # 跨版本不混算


def test_stats_source_filter(client):
    """source 过滤：live 只统计 live 行，OPEN 归属正确"""
    http, db = client
    _seed(db)
    ov = http.get("/api/ledger/stats", params={"source": "live"}).json()["overall"]
    assert ov["decided"] == 2 and ov["wins"] == 1
    assert ov["win_rate"] == 0.5
    assert ov["sum_r"] == round(0.5 - 1.13, 2)
    assert ov["open"] == 1 and ov["expired"] == 0


def test_entries_filters_and_pagination(client):
    http, db = client
    _seed(db)
    # 默认按 entry_bar_open DESC：D(12h) A(36h) B(72h) C(96h) E(240h)
    r = http.get("/api/ledger/entries").json()
    assert r["total"] == 5
    assert [it["symbol"] for it in r["items"]] == [
        "DDDUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "EEEUSDT"]

    # 各筛选维度
    assert http.get("/api/ledger/entries",
                    params={"status": "DONE"}).json()["total"] == 3
    assert http.get("/api/ledger/entries",
                    params={"outcome": "loss"}).json()["total"] == 1
    assert http.get("/api/ledger/entries",
                    params={"symbol": "bbbusdt"}).json()["total"] == 1  # 大小写归一
    assert http.get("/api/ledger/entries",
                    params={"source": "live"}).json()["total"] == 3
    assert http.get("/api/ledger/entries",
                    params={"signal_type": "trigger_pullback_confirm"}).json()["total"] == 4
    assert http.get("/api/ledger/entries",
                    params={"signal_type": "trigger_breakout"}).json()["total"] == 1
    assert http.get("/api/ledger/entries",
                    params={"direction": "short"}).json()["total"] == 1

    # 日期窗强制：days=2 只留 12h/36h 两行
    assert http.get("/api/ledger/entries", params={"days": 2}).json()["total"] == 2

    # 分页
    p1 = http.get("/api/ledger/entries", params={"page": 1, "page_size": 2}).json()
    p2 = http.get("/api/ledger/entries", params={"page": 2, "page_size": 2}).json()
    assert p1["total"] == 5 and len(p1["items"]) == 2
    assert len(p2["items"]) == 2
    assert p2["items"][0]["symbol"] == "BBBUSDT"

    # 序列化关键字段（三价/R/AI 标注）
    a = next(it for it in r["items"] if it["symbol"] == "AAAUSDT")
    assert a["entry"] == 100.0 and a["stop"] == 99.0 and a["tp1"] == 101.5
    assert a["r"] == 0.5 and a["outcome"] == "win_tp1"
    assert a["ai_decision"] is None and a["scan_result_id"] is None


def test_backfill_validation_and_guard(client, monkeypatch):
    http, db = client
    # days 越界（1~90）→ 422
    assert http.post("/api/ledger/backfill", json={"days": 0}).status_code == 422
    assert http.post("/api/ledger/backfill", json={"days": 91}).status_code == 422

    # 2h 内同名运行中任务 → 409（守卫在 .delay 之前，无需 mock）
    db.add(TaskRecord(task_type="ledger_backfill", task_name="账本回填",
                      trigger="manual", status="running"))
    db.commit()
    r = http.post("/api/ledger/backfill", json={"days": 30})
    assert r.status_code == 409 and "回填" in r.json()["detail"]

    # 超 2h 的旧 running 记录不再拦截
    db.query(TaskRecord).update({"created_at": datetime.utcnow() - timedelta(hours=3)})
    db.commit()

    # 正常触发：mock delay 防触 broker，参数透传
    import app.tasks.ledger_tasks as lt
    called = {}

    def _fake_delay(days, symbols=None):
        called["args"] = (days, symbols)

    monkeypatch.setattr(lt.run_ledger_backfill_task, "delay", _fake_delay)
    r = http.post("/api/ledger/backfill",
                  json={"days": 7, "symbols": ["AAAUSDT"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called["args"] == (7, ["AAAUSDT"])

    # symbols 缺省 → None（任务侧取扫描池同源口径）
    r2 = http.post("/api/ledger/backfill", json={"days": 30})
    assert r2.status_code == 200
    assert called["args"] == (30, None)
