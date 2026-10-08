# -*- coding: utf-8 -*-
"""大盘单边环境闸门单测（N-15 双向，docs/16 §8.3②）

覆盖：
1. market_wide_regime 判定口径：阈值 0=关闭、BTC+ETH 双跌→"down"、
   双涨→"up"、单边单币不触发（双币确认）、区间内/数据不足/查询失败→None、
   60s 缓存（含 None 缓存）、cfg 覆盖
2. 开仓闸门集成（双向逆势拦截）：普跌拦山寨**多单**、普涨拦山寨**空单**、
   顺势方向放行（普跌空单/普涨多单穿透）、BTCUSDT 本身双向放行、
   无单边行情穿透到后续闸门
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.services import market_data as md  # noqa: E402
from app.services.trade_engine import try_open_for_analysis  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from datetime import datetime  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_WIDE_PAUSE_PCT", 3.0)
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 0)  # 隔离急跌闸门，只测单边闸门
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_cache():
    md._cache.pop("market_wide_regime", None)
    yield
    md._cache.pop("market_wide_regime", None)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


def _analysis(db, symbol="TESTUSDT", rec=90, direction="long"):
    a = AIAnalysis(
        symbol=symbol, trade_decision="suggest", direction=direction,
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=rec, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _FakeTrader:
    """闸门链穿透替身：price 抛错表示已过前置闸门进入 _attempt_open"""

    account_key = "testnet-1"
    testnet = True

    def listed_symbols(self):
        return set()

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": 1000.0, "available": 900.0}

    def price(self, symbol):
        raise RuntimeError("替身不提供价格")


class _KlineStub:
    """get_futures_klines 替身：按收盘价序列构造 1h K 线（[ts,o,h,l,c,v]）"""

    def __init__(self, closes):
        self._closes = closes

    def get_futures_klines(self, symbol, interval, limit):
        return [
            [i * 3600_000, c, c, c, c, 1.0] for i, c in enumerate(self._closes)
        ]


def _moving_closes(pct: float):
    """26 根：closed[-1]/closed[0]-1 = pct%（正涨负跌；末根未收盘剔除后仍成立）"""
    end = 100.0 * (1 + pct / 100)
    return [100.0] + [100.0] * 23 + [end, end]


# ── market_wide_regime 判定口径 ───────────────────────────────────────


def test_regime_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_WIDE_PAUSE_PCT", 0)

    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("阈值 0 不应查询")

    assert md.market_wide_regime(_Boom()) is None


def test_regime_down_on_both_declining():
    assert md.market_wide_regime(_KlineStub(_moving_closes(-4.0))) == "down"


def test_regime_up_on_both_rising():
    assert md.market_wide_regime(_KlineStub(_moving_closes(4.0))) == "up"


def test_regime_none_on_single_coin_move():
    """只有 BTC 单边、ETH 横盘 → 双币确认不成立（资金切换≠普涨普跌）"""

    class _MixedStub:
        def get_futures_klines(self, symbol, interval, limit):
            closes = _moving_closes(-4.0) if symbol == "BTCUSDT" else [100.0] * 26
            return _KlineStub(closes).get_futures_klines(symbol, interval, limit)

    assert md.market_wide_regime(_MixedStub()) is None


def test_regime_none_within_threshold():
    """±2% 双向变动在默认 3% 阈值内 → None（两侧都要验）"""
    assert md.market_wide_regime(_KlineStub(_moving_closes(-2.0))) is None
    md._cache.pop("market_wide_regime", None)
    assert md.market_wide_regime(_KlineStub(_moving_closes(2.0))) is None


def test_regime_none_on_insufficient_data():
    assert md.market_wide_regime(_KlineStub([100.0, 99.0])) is None


def test_regime_query_error_degrades_none():
    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("网络异常")

    assert md.market_wide_regime(_Boom()) is None


def test_regime_result_cached_including_none():
    """"down" 与 None 都进缓存：None 命中缓存时不再触网"""
    assert md.market_wide_regime(_KlineStub(_moving_closes(-4.0))) == "down"

    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("不应被调用")

    assert md.market_wide_regime(_Boom()) == "down"
    md._cache.pop("market_wide_regime", None)
    # None 缓存：横盘行情缓存后，抛错客户端也直接返回 None（60s 内不重试）
    assert md.market_wide_regime(_KlineStub([100.0] * 26)) is None
    assert md.market_wide_regime(_Boom()) is None


def test_regime_pct_override_tightens(monkeypatch):
    """cfg 覆盖阈值 5%：±4% 双边均不触发（env 默认 3% 会触发）"""
    monkeypatch.setattr(settings, "MARKET_WIDE_PAUSE_PCT", 3.0)
    down = _KlineStub(_moving_closes(-4.0))
    assert md.market_wide_regime(down) == "down"
    md._cache.pop("market_wide_regime", None)
    assert md.market_wide_regime(down, pause_pct=5.0) is None
    md._cache.pop("market_wide_regime", None)
    up = _KlineStub(_moving_closes(4.0))
    assert md.market_wide_regime(up) == "up"
    md._cache.pop("market_wide_regime", None)
    assert md.market_wide_regime(up, pause_pct=5.0) is None


# ── 开仓闸门集成（双向逆势拦截）──────────────────────────────────────


def test_gate_blocks_altcoin_long_on_down(db, monkeypatch):
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: "down")
    a = _analysis(db, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" in (a.open_block_reason or "")
    assert "开多" in (a.open_block_reason or "")


def test_gate_blocks_altcoin_short_on_up(db, monkeypatch):
    """普涨日逆势空单同样拦截（双向对称）"""
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: "up")
    a = _analysis(db, direction="short")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普涨" in (a.open_block_reason or "")
    assert "开空" in (a.open_block_reason or "")


def test_gate_allows_trend_side_on_down(db, monkeypatch):
    """普跌日顺势空单放行（10-07 归因盈利路径均为普跌日空单）"""
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: "down")
    a = _analysis(db, direction="short")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_gate_allows_trend_side_on_up(db, monkeypatch):
    """普涨日顺势多单放行"""
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: "up")
    a = _analysis(db, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普涨" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_gate_allows_btc_on_both_sides(db, monkeypatch):
    """BTCUSDT 本身不受山寨单边暂停影响（双向豁免）"""
    for regime in ("down", "up"):
        md._cache.pop("market_wide_regime", None)
        monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None, _r=regime: _r)
        a = _analysis(db, symbol="BTCUSDT", direction="short" if regime == "down" else "long")
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
        assert "大盘普" not in (a.open_block_reason or "")


def test_gate_passes_through_without_regime(db, monkeypatch):
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: None)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")
