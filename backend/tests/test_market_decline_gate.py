# -*- coding: utf-8 -*-
"""大盘普跌环境闸门单测（N-15，docs/16 §8.3②）

覆盖：
1. market_wide_decline_active 判定口径：阈值 0=关闭、BTC+ETH 双跌触发、
   单跌不触发（双币确认）、上涨、数据不足、查询失败降级、60s 缓存、cfg 覆盖
2. 开仓闸门集成：普跌时山寨**多单**被拦、空单放行（普跌日空头占优）、
   BTCUSDT 本身放行、无普跌穿透到后续闸门
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
    monkeypatch.setattr(settings, "MARKET_DECLINE_PAUSE_PCT", 3.0)
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 0)  # 隔离急跌闸门，只测普跌
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_cache():
    md._cache.pop("market_decline_active", None)
    yield
    md._cache.pop("market_decline_active", None)


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


def _declining_closes(drop_pct=4.0):
    """26 根：closed[-1]/closed[0]-1 = -drop_pct%（末根未收盘剔除后仍成立）"""
    end = 100.0 * (1 - drop_pct / 100)
    return [100.0] + [100.0] * 23 + [end, end]


# ── market_wide_decline_active 判定口径 ───────────────────────────────


def test_decline_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_DECLINE_PAUSE_PCT", 0)

    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("阈值 0 不应查询")

    assert md.market_wide_decline_active(_Boom()) is False


def test_decline_active_on_both_down():
    assert md.market_wide_decline_active(_KlineStub(_declining_closes(4.0))) is True


def test_decline_inactive_on_single_coin_down():
    """只有 BTC 跌、ETH 横盘 → 双币确认不成立（资金切换≠普跌）"""

    class _MixedStub:
        def get_futures_klines(self, symbol, interval, limit):
            if symbol == "BTCUSDT":
                return _KlineStub(_declining_closes(4.0)).get_futures_klines(symbol, interval, limit)
            return _KlineStub([100.0] * 26).get_futures_klines(symbol, interval, limit)

    assert md.market_wide_decline_active(_MixedStub()) is False


def test_decline_inactive_on_rise():
    closes = [96.0, 96.5, 97.0] + [100.0] * 23
    assert md.market_wide_decline_active(_KlineStub(closes)) is False


def test_decline_inactive_on_insufficient_data():
    assert md.market_wide_decline_active(_KlineStub([100.0, 99.0])) is False


def test_decline_query_error_degrades_false():
    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("网络异常")

    assert md.market_wide_decline_active(_Boom()) is False


def test_decline_result_cached():
    stub = _KlineStub(_declining_closes(4.0))
    assert md.market_wide_decline_active(stub) is True

    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("不应被调用")

    assert md.market_wide_decline_active(_Boom()) is True


def test_decline_pct_override_tightens(monkeypatch):
    """cfg 覆盖阈值 5%：4% 双跌不触发（env 默认 3% 会触发）"""
    monkeypatch.setattr(settings, "MARKET_DECLINE_PAUSE_PCT", 3.0)
    stub = _KlineStub(_declining_closes(4.0))
    assert md.market_wide_decline_active(stub) is True
    md._cache.pop("market_decline_active", None)
    assert md.market_wide_decline_active(stub, decline_pct=5.0) is False


# ── 开仓闸门集成 ──────────────────────────────────────────────────────


def test_gate_blocks_altcoin_long_on_decline(db, monkeypatch):
    monkeypatch.setattr(md, "market_wide_decline_active", lambda client, decline_pct=None: True)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" in (a.open_block_reason or "")


def test_gate_allows_altcoin_short_on_decline(db, monkeypatch):
    """普跌日空单占优（10-07 归因盈利路径均为空单）：空单放行穿透"""
    monkeypatch.setattr(md, "market_wide_decline_active", lambda client, decline_pct=None: True)
    a = _analysis(db, direction="short")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_gate_allows_btc_long_on_decline(db, monkeypatch):
    """BTCUSDT 本身不受山寨开多暂停影响"""
    monkeypatch.setattr(md, "market_wide_decline_active", lambda client, decline_pct=None: True)
    a = _analysis(db, symbol="BTCUSDT")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_gate_passes_through_without_decline(db, monkeypatch):
    monkeypatch.setattr(md, "market_wide_decline_active", lambda client, decline_pct=None: False)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "大盘普跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")
