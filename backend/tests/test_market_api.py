# -*- coding: utf-8 -*-
"""大盘环境/市场情绪接口回归（GET /api/market/overview）。

Header 大盘条数据源：聚合 get_fear_greed（恐贪）+ get_market_breadth（BTC/ETH
EMA 形态 + 24h 涨跌）+ get_structure_snapshot（每小时技术结构检测快照，2026-10-13）。
全部子项失败静默降级为 null，接口恒 200。

覆盖：
1. 全量数据：fear_greed + breadth 结构透传
2. 子项缺失：单项 null 不影响另一项
3. 全失败：fear_greed=null, breadth=null（接口不报错）
4. 结构快照：透传 / 降级
5. 结构快照映射：从「技术结构检测」任务记录 detail 解析（内存 sqlite）
"""
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models.ops import TaskRecord  # noqa: E402
from app.services import market_data  # noqa: E402


_FNG = {"value": 72, "label": "Greed"}

_BREADTH = {
    "BTCUSDT": {
        "ema_state": "bullish_align",
        "ema_label": "多头排列",
        "change_24h_pct": 2.31,
    },
    "ETHUSDT": {
        "ema_state": "mixed",
        "ema_label": "纠缠",
        "change_24h_pct": -0.85,
    },
}

_STRUCTURE = {
    "BTCUSDT": {
        "trend": "up", "trend_label": "上涨结构", "valid": True, "broken": False,
        "support": 61200.0, "support_dist_pct": -0.6,
        "resistance": 63000.0, "resistance_dist_pct": 3.5,
        "htf_label": "多头排列", "anomaly_labels": [],
        "updated_at": "2026-10-13T06:00:12Z",
    },
    "ETHUSDT": {
        "trend": "down", "trend_label": "下降结构", "valid": False, "broken": True,
        "support": 2400.0, "support_dist_pct": -8.2,
        "resistance": 2600.0, "resistance_dist_pct": -0.4,
        "htf_label": "空头排列", "anomaly_labels": ["结构破位"],
        "updated_at": "2026-10-13T06:00:12Z",
    },
}


def _client() -> TestClient:
    return TestClient(app)


def test_overview_full(monkeypatch):
    """两数据源均可用 → 结构原样透传"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: _FNG)

    class _FakePool:
        def get_klines(self, *a, **k):
            return []

    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: _BREADTH)
    monkeypatch.setattr(market_data, "get_structure_snapshot", lambda: None)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] == _FNG
    assert body["breadth"] == _BREADTH
    assert body["breadth"]["BTCUSDT"]["ema_label"] == "多头排列"
    assert body["breadth"]["ETHUSDT"]["change_24h_pct"] == -0.85


def test_overview_partial_missing(monkeypatch):
    """恐贪失败只降级该子项，大盘广度照常返回"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: None)
    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: _BREADTH)
    monkeypatch.setattr(market_data, "get_structure_snapshot", lambda: None)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] == _BREADTH


def test_overview_all_failure(monkeypatch):
    """全失败 → 两子项均 null，接口仍 200 不报错"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: None)
    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: None)
    monkeypatch.setattr(market_data, "get_structure_snapshot", lambda: None)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] is None
    assert body["structure"] is None


def test_overview_source_exception_degrades(monkeypatch):
    """数据源抛异常 → 静默降级为 null（不触发全局 500）"""
    def _boom(*a, **k):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(market_data, "get_fear_greed", _boom)
    monkeypatch.setattr(market_data, "get_market_breadth", _boom)
    monkeypatch.setattr(market_data, "get_structure_snapshot", _boom)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] is None
    assert body["structure"] is None


# ── 结构快照（2026-10-13：Header 大盘条结构 token 数据源）──


def test_overview_structure_passthrough(monkeypatch):
    """结构快照可用 → 原样透传（不与其他子项联动）"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: _FNG)
    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: _BREADTH)
    monkeypatch.setattr(market_data, "get_structure_snapshot", lambda: _STRUCTURE)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["structure"] == _STRUCTURE
    assert body["structure"]["ETHUSDT"]["broken"] is True
    assert body["structure"]["BTCUSDT"]["trend_label"] == "上涨结构"


def _sqlite_maker():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_structure_snapshot_from_record(monkeypatch):
    """记录映射：detail.symbols → 展示字段（trend_label/broken/距离/htf/时间）；
    无记录 → None"""
    maker = _sqlite_maker()
    monkeypatch.setattr(market_data, "SessionLocal", maker)

    assert market_data.get_structure_snapshot() is None  # 空库

    s = maker()
    s.add(TaskRecord(
        id=uuid4(), task_type="review", task_name="技术结构检测", trigger="scheduled",
        status="completed", summary="x",
        detail={"symbols": {
            "BTCUSDT": {
                "structure": {"trend": "up", "last_h": 63000.0, "last_l": 61200.0,
                              "close": 61500.0, "valid": True, "anchor": 61200.0,
                              "label_high": "HH", "label_low": "HL", "note": ""},
                "key_levels": {"resistance": 63000.0, "resistance_dist_pct": 2.44,
                               "support": 61200.0, "support_dist_pct": -0.49},
                "htf": {"state": "aligned_bull", "state_label": "多头排列",
                        "dir": 1, "age_bars": 12, "closed_bars": 299},
                "integrity": {"k1h_gaps": 0, "k4h_gaps": 0, "k1h_closed": 499, "k4h_closed": 299},
                "anomalies": [], "error": None,
            },
            "ETHUSDT": {
                "structure": {"trend": "down", "valid": False, "close": 2500.0},
                "key_levels": {"support": 2400.0, "support_dist_pct": -4.0,
                               "resistance": 2650.0, "resistance_dist_pct": 6.0},
                "htf": {"state": "bear_rally", "state_label": "空头反弹", "dir": -1},
                "integrity": {}, "anomalies": ["break_up", "gap_4h"], "error": None,
            },
        }},
        finished_at=datetime(2026, 10, 13, 6, 0, 12),
    ))
    s.commit()
    s.close()

    snap = market_data.get_structure_snapshot()
    assert set(snap) == {"BTCUSDT", "ETHUSDT"}
    btc = snap["BTCUSDT"]
    assert btc["trend_label"] == "上涨结构" and btc["broken"] is False
    assert btc["support"] == 61200.0 and btc["support_dist_pct"] == -0.49
    assert btc["resistance"] == 63000.0 and btc["resistance_dist_pct"] == 2.44
    assert btc["htf_label"] == "多头排列" and btc["anomaly_labels"] == []
    assert btc["updated_at"] == "2026-10-13T06:00:12Z"
    eth = snap["ETHUSDT"]
    assert eth["trend_label"] == "下降结构" and eth["broken"] is True
    assert eth["valid"] is False and eth["htf_label"] == "空头反弹"
    assert eth["anomaly_labels"] == ["结构破位", "4h缺口"]  # 未知码原样透出
