# -*- coding: utf-8 -*-
"""交易复盘K线 API 回归（/trades/{id}/klines，2026-10-02 KSMUSDT 案例）。

接管单 OPEN 事件 detail.price 是接管时刻现价、真实持仓均价在 detail.entry_price；
复盘图入场价若直接取 detail.price 会画偏。K 线源 monkeypatch 为固定序列，
sqlite 内存库不触网。

覆盖：
1. 接管单：入场价取 detail.entry_price（非 detail.price 接管时点现价）
2. 普通单：入场价取 detail.price（实际成交价）——历史行为不变
3. 无 OPEN 事件的老记录：回退 rec.entry_price
4. 平仓后走势窗（2026-10-05）：已平仓请求区间延至平仓后 5 根，进行中单不变
"""
import math
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


# ── 4. 平仓后走势窗（已平仓 +5 根，进行中不变）────────────────────────


def _kline_capture_fixture(monkeypatch):
    """独立内存库 + get_klines_range 参数捕获桩（不复用上方 client fixture，
    因其桩不记录请求参数）。返回 (请求函数, captured)。
    _get(trade, params=None)：params 透传查询串（周期切换测试用）。"""
    captured = []

    def _fake(self, symbol, interval, bars, end_ms):
        captured.append({"interval": interval, "bars": bars, "end_ms": end_ms})
        base = int(datetime(2026, 10, 1, 0, 0).timestamp() * 1000)
        return [[base + i * 3_600_000, 10.0, 10.2, 9.8, 10.0, 1.0] for i in range(3)]

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
        yield session

    app.dependency_overrides[get_db] = _override
    monkeypatch.setattr(trades_api.ExchangePool, "get_klines_range", _fake)

    def _get(trade: TradeRecord, params=None):
        return TestClient(app).get(f"/api/trades/{trade.id}/klines", params=params)

    return _get, session, captured


def test_closed_trade_requests_5_bars_after_close(monkeypatch):
    """已平仓：end_ms = closed_at(UTC) + 5×interval − 1ms，bars 含 5 根后置窗"""
    from datetime import timedelta, timezone

    _get, db, captured = _kline_capture_fixture(monkeypatch)
    closed = datetime.utcnow() - timedelta(hours=6)  # 平仓已久，5 根窗全部可得
    rec = _trade(db, status="CLOSED", exit_reason="SL_FILL",
                 opened_at=closed - timedelta(hours=2, minutes=10))
    rec.closed_at = closed
    db.commit()

    assert _get(rec).status_code == 200
    assert len(captured) == 1
    interval_ms = 3_600_000  # SystemConfig 默认 1h
    # 端点内部：end_ms = closed_at 按 UTC 的毫秒 − 1ms + 5×interval
    expected_end = int(closed.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1 + 5 * interval_ms
    assert captured[0]["end_ms"] == expected_end
    assert captured[0]["bars"] == 100 + math.ceil((130 * 60 * 1000) / interval_ms) + 5  # held=3


def test_running_trade_no_after_window(monkeypatch):
    """进行中：区间止于当前时刻（无平仓后窗），bars 不含后置 5 根"""
    from datetime import timedelta, timezone

    _get, db, captured = _kline_capture_fixture(monkeypatch)
    rec = _trade(db, opened_at=datetime.utcnow() - timedelta(hours=2))

    assert _get(rec).status_code == 200
    assert len(captured) == 1
    interval_ms = 3_600_000
    now_ms = captured[0]["end_ms"] + 1  # 端点原式 end_ms = now_ms − 1
    # DB 存 UTC naive——期望值必须按 UTC 解释（replace(tzinfo=None) 会按本地时区，差 8h）
    opened_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    held = math.ceil((now_ms - opened_ms) / interval_ms)
    assert captured[0]["bars"] == 100 + held
    # end_ms = 端点执行时刻的 UTC epoch − 1ms，必然落在真实当前时间附近（±1h 内）
    assert abs(captured[0]["end_ms"] - datetime.now(timezone.utc).timestamp() * 1000) < 3_600_000


# ── 5. 周期切换（2026-10-08 弹窗 1h/4h 复盘）──────────────────────────


def test_klines_interval_param_4h(monkeypatch):
    """interval=4h：透传交易所区间请求按 4h 口径计算，响应 interval 回显 4h"""
    from datetime import timedelta, timezone

    _get, db, captured = _kline_capture_fixture(monkeypatch)
    rec = _trade(db, opened_at=datetime.utcnow() - timedelta(hours=10))

    r = _get(rec, params={"interval": "4h"})
    assert r.status_code == 200
    assert r.json()["interval"] == "4h"
    assert len(captured) == 1
    assert captured[0]["interval"] == "4h"
    # 10h 持仓按 4h 一根 = 3 根（ceil），bars = 100 + 3（无平仓后窗）
    assert captured[0]["bars"] == 103


def test_klines_interval_param_default_config(monkeypatch):
    """不带 interval：回落 cfg.kline_interval（默认 1h），历史行为不变"""
    from datetime import timedelta

    _get, db, captured = _kline_capture_fixture(monkeypatch)
    rec = _trade(db, opened_at=datetime.utcnow() - timedelta(hours=10))

    r = _get(rec)
    assert r.status_code == 200
    assert r.json()["interval"] == "1h"
    assert captured[0]["interval"] == "1h"


def test_klines_interval_param_invalid(monkeypatch):
    """interval=7x：400，不触交易所"""
    _get, db, captured = _kline_capture_fixture(monkeypatch)
    rec = _trade(db)

    r = _get(rec, params={"interval": "7x"})
    assert r.status_code == 400
    assert "不支持的周期" in r.json()["detail"]
    assert captured == []
