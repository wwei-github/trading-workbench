# -*- coding: utf-8 -*-
"""虚拟账本扫描挂点单测（docs/16 P-07 步骤2）。

锁三件事：
1. _save_results 对带 ledger 快照的 hits 开虚拟单（live 行，关联 scan_result_id）；
2. 同 (symbol, signal_type, entry_bar_open) 重复落库 → 唯一约束去重仍 1 行；
3. 账本失败/无快照绝不拖累 ScanResult（影子同款铁律）。
sqlite 内存库夹具镜像 test_pending_ops_db.py。
"""
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.scanner as scanner_mod  # noqa: E402
from app.models.ledger import VirtualLedger  # noqa: E402
from app.models.scan import ScanResult  # noqa: E402
from app.services.scanner import Scanner  # noqa: E402

T0_MS = 1_791_000_000_000

SNAP = {
    "entry": 100.0, "stop": 99.25, "tp1": 101.125, "tp2": 102.5, "r_unit": 0.75,
    "direction": "long", "entry_bar_open_ms": T0_MS,
}
HIT = {
    "symbol": "BTC/USDT",
    "signal_type": "trigger_pullback_confirm",
    "current_price": 100.0,
    "breakout_pct": 0.5,
    "pattern": "bullish_engulfing",
    "position": "support",
    "touch_ref": "ema21",
    "ema_state": "bullish",
    "quality_score": 72,
    "votes": {"net": 3, "voted": 7},
    "ledger": dict(SNAP),
}


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(
        bind=engine, tables=[ScanResult.__table__, VirtualLedger.__table__])
    monkeypatch.setattr(
        scanner_mod, "SessionLocal", lambda: Session(bind=engine))
    return Session(bind=engine)


def _rows(db):
    return db.query(VirtualLedger).all()


def test_open_entry_creates_live_row(db):
    result_ids = Scanner._save_results(uuid.uuid4(), [HIT], scan_type="scheduled")
    rows = _rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert r.source == "live" and r.status == "OPEN"
    assert r.scan_result_id == result_ids["BTC/USDT"]
    assert r.entry_price == 100.0 and r.direction == "long"
    assert r.signal_type == "trigger_pullback_confirm"
    assert r.quality_score == 72
    assert r.votes_json == {"net": 3, "voted": 7}
    assert r.outcome is None and r.r is None  # 未决不入分母
    assert r.rule_version  # 口径快照落行
    from datetime import datetime
    assert r.entry_bar_open == datetime.utcfromtimestamp(T0_MS / 1000.0)


def test_duplicate_candle_dedup(db):
    """同根K线重复落库（手动+定时同评一根）→ 唯一键去重仍 1 行"""
    Scanner._save_results(uuid.uuid4(), [HIT])
    Scanner._save_results(uuid.uuid4(), [dict(HIT)])
    assert len(_rows(db)) == 1
    assert db.query(ScanResult).count() == 2  # ScanResult 不受去重影响


def test_no_snapshot_no_row(db):
    hit = {k: v for k, v in HIT.items() if k != "ledger"}
    Scanner._save_results(uuid.uuid4(), [hit])
    assert len(_rows(db)) == 0
    assert db.query(ScanResult).count() == 1


def test_ledger_failure_does_not_hurt_results(db, monkeypatch):
    """开单抛异常 → 只告警回滚账本，ScanResult 照常落库"""
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr("app.services.ledger.open_entry", boom)
    Scanner._save_results(uuid.uuid4(), [HIT])
    assert db.query(ScanResult).count() == 1
    assert len(_rows(db)) == 0


def test_attach_ai_updates_row(db):
    Scanner._save_results(uuid.uuid4(), [HIT])
    row = _rows(db)[0]
    from app.services.ledger import attach_ai
    n = attach_ai(row.scan_result_id, "suggest", 75.0, db=db)
    assert n == 1
    db.refresh(row)
    assert row.ai_decision == "suggest"
    assert row.ai_recommendation == 75.0
