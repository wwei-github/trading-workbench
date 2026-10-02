# -*- coding: utf-8 -*-
"""交易复盘K线 API 回归（/trades/{id}/klines，2026-10-02 KSMUSDT 案例）。

接管单 OPEN 事件 detail.price 是接管时刻现价、真实持仓均价在 detail.entry_price；
复盘图入场价若直接取 detail.price 会画偏。K 线源 monkeypatch 为固定序列，
sqlite 内存库不触网。

覆盖：
1. 接管单：入场价取 detail.entry_price（非 detail.price 接管时点现价）
2. 普通单：入场价取 detail.price（实际成交价）——历史行为不变
3. 无 OPEN 事件的老记录：回退 rec.entry_price
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.api.trades as trades_api  # noqa: E402
import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402


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
    session.add(SystemConfig(id=1))
    session.commit()

    def _override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override

    # K 线源桩：返回 3 根固定 1h K 线（时间落在开仓前后），绕开交易所 IO
    def _fake_klines_range(self, symbol, interval, bars, end_ms):
        base = int(datetime(2026, 10, 1, 0, 0).timestamp() * 1000)
        return [
            [base + i * 3_600_000, 10.0, 10.2, 9.8, 10.0, 1.0]
            for i in range(3)
        ]

    orig = trades_api.ExchangePool.get_klines_range
    trades_api.ExchangePool.get_klines_range = _fake_klines_range
    yield TestClient(app), session
    trades_api.ExchangePool.get_klines_range = orig
    app.dependency_overrides.pop(get_db, None)
    session.close()


def _trade(db, **kw):
    base = dict(
        id=uuid.uuid4(),
        symbol="KSMUSDT", direction="long", status="OPENED",
        testnet=False, account_key="live-1",
        entry_price=11.5, qty=10.0, risk_amount=15.0,
        stop_loss=10.0, tp1=13.0, tp2=0.0,
        opened_at=datetime(2026, 10, 1, 0, 30),
    )
    base.update(kw)
    rec = TradeRecord(**base)
    db.add(rec)
    db.flush()
    return rec


def _open_event(db, rec, detail, at=datetime(2026, 10, 1, 0, 30)):
    db.add(TradeEvent(
        trade_record_id=rec.id, event_type="OPEN", detail=detail, created_at=at,
    ))
    db.commit()


def test_adopted_entry_price_from_detail(client):
    """接管单：detail.price=接管时点现价 ≠ 持仓均价，入场价必须取 detail.entry_price"""
    http, db = client
    rec = _trade(db)
    _open_event(db, rec, {
        "adopted": True, "price": 12.34, "entry_price": 11.5, "qty": 10.0,
        "stop_loss": 10.0, "tp1": 13.0,
    })
    body = http.get(f"/api/trades/{rec.id}/klines").json()
    assert body["entry_price"] == 11.5


def test_normal_entry_price_from_fill(client):
    """普通单：detail.price=实际成交价，入场价取之（历史行为不变）"""
    http, db = client
    rec = _trade(db)
    _open_event(db, rec, {
        "price": 100.05, "qty": 1.0, "stop_loss": 98.0, "tp1": 102.0,
        "fill_price": 100.05,
    })
    body = http.get(f"/api/trades/{rec.id}/klines").json()
    assert body["entry_price"] == 100.05


def test_legacy_fallback_rec_entry(client):
    """老记录（无 OPEN 事件）：回退 rec.entry_price"""
    http, db = client
    rec = _trade(db, entry_price=9.9)
    body = http.get(f"/api/trades/{rec.id}/klines").json()
    assert body["entry_price"] == 9.9
