# -*- coding: utf-8 -*-
"""关注列表「立即扫描」落库口径（docs/11 T2）：组合确认三列必须透传。

`api/watchlist.py` 手写 `ScanResult(...)`，与 `Scanner._save_results` 是同源
逻辑的两份实现——漏传会让组合确认命中在关注列表入口静默降级成单形态
（前端退回「锚信号根」），且不会报错，只能靠断言三列挡住。

桩只打在边界外（K 线源 / 信号判定 / 成交额），断言的是真实 endpoint 写进
sqlite 的行。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.scan import ScanResult, Watchlist  # noqa: E402


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


def test_refresh_persists_combo_columns(client, monkeypatch):
    """docs/11：组合确认命中经「立即扫描」落库时，三列与定时扫描同口径"""
    http, db = client
    from app.api import watchlist as wl

    ts = datetime(2026, 10, 26, 8, 0)
    # 形态根早于信号根一格（2026-10-26，docs/11 §4.1 判据：entry_lag_pct 非空
    # ⇔ 形态根 < 信号根）。T2 夹具曾让两根重合却带滞后，自相矛盾
    pattern_ts = ts - timedelta(hours=2)
    now = datetime.utcnow()
    db.add(Watchlist(symbol="REZUSDT", created_at=now, updated_at=now))
    db.commit()

    # 边界桩：K 线源（不触网）、信号判定（组合路径候选）、24h 成交额
    class _FakePool:
        def get_klines(self, symbol, interval, limit, force_refresh=False):
            return [[0, 100.0, 101.0, 99.0, 100.5, 1.0],
                    [3600000, 100.5, 101.5, 100.0, 101.0, 1.0]]

    class _FakeBinance:
        def get_24h_tickers(self):
            return [{"symbol": "REZUSDT", "quoteVolume": "12345"}]

        def close(self):
            pass

    monkeypatch.setattr(wl, "ExchangePool", _FakePool)
    monkeypatch.setattr(wl, "BinanceClient", _FakeBinance)
    monkeypatch.setattr(wl.Scanner, "_prefetch_pendings", staticmethod(lambda symbols: {}))
    monkeypatch.setattr(wl, "detect_indicator_candidate", lambda *a, **k: ({
        "signal_type": "trigger_pullback_confirm",
        "current_price": 101.0,
        "breakout_pct": 0.5,
        "pattern": "hammer",
        "golden_combo": True,
        "pattern_candle_open": pattern_ts,
        "entry_lag_pct": 1.588,
        "detected_at": ts,
    }, None))

    res = http.post("/api/watchlist/REZUSDT/refresh")
    assert res.status_code == 200, res.text

    row = db.execute(
        select(ScanResult).where(ScanResult.symbol == "REZUSDT")
    ).scalars().one()
    assert row.golden_combo is True
    assert row.pattern_candle_open == pattern_ts
    assert float(row.entry_lag_pct) == pytest.approx(1.588)
