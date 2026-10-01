# -*- coding: utf-8 -*-
"""大盘环境/市场情绪接口回归（GET /api/market/overview）。

Header 大盘条数据源：聚合 get_fear_greed（恐贪）+ get_market_breadth（BTC/ETH
EMA 形态 + 24h 涨跌）。全部子项失败静默降级为 null，接口恒 200。

覆盖：
1. 全量数据：fear_greed + breadth 结构透传
2. 子项缺失：单项 null 不影响另一项
3. 全失败：fear_greed=null, breadth=null（接口不报错）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
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


def _client() -> TestClient:
    return TestClient(app)


def test_overview_full(monkeypatch):
    """两数据源均可用 → 结构原样透传"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: _FNG)

    class _FakePool:
        def get_klines(self, *a, **k):
            return []

    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: _BREADTH)
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
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] == _BREADTH


def test_overview_all_failure(monkeypatch):
    """全失败 → 两子项均 null，接口仍 200 不报错"""
    monkeypatch.setattr(market_data, "get_fear_greed", lambda: None)
    monkeypatch.setattr(market_data, "get_market_breadth", lambda pool: None)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] is None


def test_overview_source_exception_degrades(monkeypatch):
    """数据源抛异常 → 静默降级为 null（不触发全局 500）"""
    def _boom(*a, **k):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(market_data, "get_fear_greed", _boom)
    monkeypatch.setattr(market_data, "get_market_breadth", _boom)
    resp = _client().get("/api/market/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fear_greed"] is None
    assert body["breadth"] is None
