# -*- coding: utf-8 -*-
"""BTC 急跌普跌防护闸门单测（docs/24 A4②）

覆盖：
1. btc_crash_active 判定口径：阈值 0=关闭、累计跌幅超阈值、上涨、数据不足、查询失败
2. 开仓闸门集成：急跌时山寨被拦（BTCUSDT 本身放行）、无急跌穿透到后续闸门
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
from app.services import binance_trader as bt_mod  # noqa: E402
from app.services.trade_engine import try_open_for_analysis  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from datetime import datetime  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 1.0)
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_cache():
    md._cache.pop("btc_crash_active", None)
    yield
    md._cache.pop("btc_crash_active", None)


@pytest.fixture(autouse=True)
def _stub_listed(monkeypatch):
    class _StubListed:
        def listed_symbols(self):
            return set()

    monkeypatch.setattr(bt_mod, "BinanceTrader", _StubListed)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


def _analysis(db, symbol="TESTUSDT", rec=90):
    a = AIAnalysis(
        symbol=symbol, trade_decision="suggest", direction="long",
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


# ── btc_crash_active 判定口径 ─────────────────────────────────────────


def test_crash_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 0)
    assert md.btc_crash_active(_KlineStub([100, 99, 98, 97, 96])) is False


def test_crash_active_on_cumulative_drop(monkeypatch):
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 1.0)
    stub = _KlineStub([100.0] * 20 + [99.5, 99.0, 98.0, 98.0, 98.0])
    # 已收盘口径：剔除最后一根，近3根 = 99.0→98.0 三根累计（99/98-1 ≈ -1.01%）
    assert md.btc_crash_active(stub) is True


def test_crash_inactive_on_rise():
    stub = _KlineStub([98.0, 98.5, 99.0, 99.5, 100.0] + [100.0] * 20)
    assert md.btc_crash_active(stub) is False


def test_crash_inactive_on_insufficient_data():
    assert md.btc_crash_active(_KlineStub([100.0, 99.0])) is False


def test_crash_query_error_degrades_false():
    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("网络异常")

    assert md.btc_crash_active(_Boom()) is False


def test_crash_result_cached():
    stub = _KlineStub([100.0] * 20 + [99.5, 99.0, 98.0, 98.0, 98.0])
    assert md.btc_crash_active(stub) is True
    # 第二次走缓存：即使替换成抛错客户端也不触网
    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("不应被调用")

    assert md.btc_crash_active(_Boom()) is True


# ── 阈值页面覆盖（2026-10-02 system_config.btc_crash_pause_pct 注入）──


def test_crash_pct_override_tightens(monkeypatch):
    """cfg 覆盖阈值 3%：1% 跌幅不触发（env 默认 1% 会触发）"""
    stub = _KlineStub([100.0] * 20 + [99.5, 99.0, 98.0, 98.0, 98.0])
    assert md.btc_crash_active(stub) is True  # env 默认 1%
    md._cache.pop("btc_crash_active", None)
    assert md.btc_crash_active(stub, crash_pct=3.0) is False


def test_crash_pct_override_zero_disables(monkeypatch):
    """cfg 覆盖阈值 0=关闭（恒 False，不查 K 线）"""
    class _Boom:
        def get_futures_klines(self, symbol, interval, limit):
            raise RuntimeError("阈值 0 不应查询")

    assert md.btc_crash_active(_Boom(), crash_pct=0) is False


# ── 开仓闸门集成 ──────────────────────────────────────────────────────


def test_gate_blocks_altcoin_on_crash(db, monkeypatch):
    # 2026-10-02 起开仓闸门传 crash_pct=（cfg 覆盖），替身需接受该关键字
    monkeypatch.setattr(md, "btc_crash_active", lambda client, crash_pct=None: True)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "BTC 1h 短线急跌" in (a.open_block_reason or "")


def test_gate_allows_btc_itself_on_crash(db, monkeypatch):
    """BTCUSDT 本身不受山寨暂停影响（穿透到 _attempt_open）"""
    monkeypatch.setattr(md, "btc_crash_active", lambda client, crash_pct=None: True)
    a = _analysis(db, symbol="BTCUSDT")
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "BTC 1h 短线急跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_gate_passes_through_without_crash(db, monkeypatch):
    monkeypatch.setattr(md, "btc_crash_active", lambda client, crash_pct=None: False)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "BTC 1h 短线急跌" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")
