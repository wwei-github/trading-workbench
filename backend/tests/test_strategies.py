# -*- coding: utf-8 -*-
"""多策略可插拔（docs/20 S0/S1）单测：registry + ema_trend 插件 + 影子信号 API。

- registry：NULL=仅内置 golden_k_ai；未知 id 拒绝；clamp；golden_k 不可禁用；
- ema_trend：合成 K 线构造 4H 闸门通过 + 1H 结构成立的多/空回踩信号，
  走平拒绝；K 线格式 [ts, o, h, l, c, v]（与 ExchangePool 一致）；
- API：/api/strategies 清单、配置回写规范化、影子信号查询。
sqlite 内存库 + TestClient，不触网不触 Celery。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.strategy import StrategyShadowSignal  # noqa: E402
from app.services.strategies.registry import (  # noqa: E402
    get_strategy,
    load_registry,
)
from app.services.strategies.ema_trend import EmaTrendStrategy, ema_series  # noqa: E402


# ── K 线构造 ──────────────────────────────────────────────────────────────

TS0 = 1_791_000_000_000
H1, H4 = 3_600_000, 14_400_000


def _bars(closes, ts_step, volumes=None):
    """closes → K 线（h=c+0.5, l=c-0.5, o=前收盘, v 默认 1000）。"""
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append([TS0 + i * ts_step, c - 0.0, c + 0.5, c - 0.5, c,
                    volumes[i] if volumes else 1000.0])
    return out


def _trend_closes(n, step=0.05, start=100.0):
    return [start + i * step for i in range(n)]


def _pullback_klines_1h(direction: str):
    """1h 上升趋势 + 倒数第二根（末根已收盘）回踩 EMA21 企稳（多头）/反抽（空头），
    再追加一根尾随未收盘 K 线（插件 _closed 会剔除，只作输入形态仿真）。

    触点设计：以 299 根收盘的 EMA21 末值作影线端点——EMA(300) 仅前移
    ~0.07（a×Δclose），落容差带 ±0.3×ATR(=1.0) 内。"""
    n = 300
    step = 0.05 if direction == "long" else -0.05
    closes = _trend_closes(n - 1, step)
    e21 = ema_series(np.array(closes, dtype=float), 21)
    touch = float(e21[-1])
    last_c = closes[-1]
    if direction == "long":
        sig_bar = [TS0 + (n - 1) * H1, last_c, last_c + 0.8, touch, last_c + 0.3, 1100.0]
        tail = [TS0 + n * H1, last_c + 0.3, last_c + 0.9, last_c + 0.1, last_c + 0.5, 900.0]
    else:
        sig_bar = [TS0 + (n - 1) * H1, last_c, touch, last_c - 0.8, last_c - 0.3, 1100.0]
        tail = [TS0 + n * H1, last_c - 0.3, last_c - 0.1, last_c - 0.9, last_c - 0.5, 900.0]
    return _bars(closes, H1) + [sig_bar, tail]


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


# ── registry ──────────────────────────────────────────────────────────────


def test_registry_null_only_golden_k():
    entries = load_registry(None)
    assert [e.id for e in entries] == ["golden_k_ai"]
    assert entries[0].enabled is True


def test_registry_unknown_id_rejected_and_clamp():
    entries = load_registry([
        {"id": "bogus", "enabled": True},
        {"id": "ema_trend", "enabled": True, "max_positions": 99,
         "risk_weight": 5.0, "config": {"adx_min": 25}, "junk": 1},
        {"id": "golden_k_ai", "enabled": False},  # 内置不可禁用
    ])
    ids = [e.id for e in entries]
    assert ids[0] == "golden_k_ai" and "bogus" not in ids
    ema = next(e for e in entries if e.id == "ema_trend")
    assert ema.enabled is True
    assert ema.max_positions == 10   # clamp 1~10
    assert ema.risk_weight == 1.0    # clamp 0.1~1.0
    assert ema.config == {"adx_min": 25}  # 未知键丢弃
    golden = entries[0]
    assert golden.enabled is True    # 恒启用


def test_get_strategy_injects_config():
    raw = [{"id": "ema_trend", "enabled": True, "config": {"adx_min": 25}}]
    s = get_strategy("ema_trend", raw)
    assert s.cfg.adx_min == 25
    assert s.id == "ema_trend"


# ── ema_trend 插件 ────────────────────────────────────────────────────────


def test_ema_trend_long_pullback_signal():
    s = EmaTrendStrategy({})
    k1 = _pullback_klines_1h("long")
    k4 = _bars(_trend_closes(300, 0.05), H4)
    sig = s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4)
    assert sig is not None
    assert sig.strategy_id == "ema_trend"
    assert sig.direction == "long"
    assert sig.signal_type == "pullback"
    assert sig.stop < sig.entry                      # 多头止损在下方
    assert sig.raw["gate_4h"]["direction"] == "up"
    assert sig.raw["structure_1h"]["aligned"] is True
    assert sig.raw["touched"] in ("EMA21", "EMA55")
    # 前高距入场不足 min_rr → TP1=0（不设固定目标）
    assert sig.tp1 == 0.0 and sig.tp2 == 0.0


def test_ema_trend_short_pullback_signal():
    s = EmaTrendStrategy({})
    k1 = _pullback_klines_1h("short")
    k4 = _bars(_trend_closes(300, -0.05), H4)
    sig = s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4)
    assert sig is not None and sig.direction == "short"
    assert sig.stop > sig.entry                      # 空头止损在上方
    assert sig.raw["gate_4h"]["direction"] == "down"


def test_ema_trend_flat_market_rejected():
    s = EmaTrendStrategy({})
    k1 = _bars([100.0] * 300, H1)
    k4 = _bars([100.0] * 300, H4)
    assert s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4) is None


def test_ema_trend_insufficient_bars_rejected():
    s = EmaTrendStrategy({})
    k1 = _bars(_trend_closes(100), H1)
    k4 = _bars(_trend_closes(100), H4)
    assert s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4) is None


# ── API ───────────────────────────────────────────────────────────────────


def test_api_list_strategies_default_golden_k_only(client):
    http, _db = client
    r = http.get("/api/strategies")
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body] == ["golden_k_ai"]
    assert body[0]["kind"] == "native"


def test_api_config_registry_roundtrip_and_normalize(client):
    http, _db = client
    r = http.put("/api/scans/config", json={
        "strategy_registry": [
            {"id": "ema_trend", "enabled": True, "max_positions": 2,
             "risk_weight": 0.5, "config": {"adx_min": 25}},
            {"id": "bogus", "enabled": True},
        ],
    })
    assert r.status_code == 200
    assert r.json()["strategy_registry"] == [
        {"id": "ema_trend", "enabled": True, "max_positions": 2,
         "risk_weight": 0.5, "config": {"adx_min": 25}},
    ]
    # 清单读出 ema_trend enabled
    lst = http.get("/api/strategies").json()
    ids = {s["id"] for s in lst}
    assert "ema_trend" in ids
    assert [s for s in lst if s["id"] == "ema_trend"][0]["enabled"] is True
    # 显式 null = 清除定制
    r2 = http.put("/api/scans/config", json={"strategy_registry": None})
    assert r2.status_code == 200 and r2.json()["strategy_registry"] is None


def test_api_signals_empty_and_after_insert(client):
    http, session = client
    assert http.get("/api/strategies/signals").json() == []
    session.add(StrategyShadowSignal(
        strategy_id="ema_trend", symbol="TESTUSDT", direction="long",
        entry=100.0, stop=99.0, tp1=0.0, tp2=0.0, signal_type="pullback",
        reason="单测", confidence=0.6, raw={"touched": "EMA21"},
    ))
    session.commit()
    rows = http.get("/api/strategies/signals?strategy_id=ema_trend").json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TESTUSDT" and rows[0]["direction"] == "long"
    assert rows[0]["raw"]["touched"] == "EMA21"
    bad = http.get("/api/strategies/signals?strategy_id=bogus")
    assert bad.status_code == 400
