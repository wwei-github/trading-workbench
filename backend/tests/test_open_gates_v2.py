# -*- coding: utf-8 -*-
"""开仓闸门 v2 单测（docs/24 第 2 批）

覆盖：
1. A3 连损降速：_consecutive_losses_today 计数口径（0/2/3 档、盈利截断、
   账号隔离、日界）+ try_open_for_analysis 评分门槛提升拦截 + 开关关闭不拦截
2. A3' 组合热度上限 / 同向持仓上限：在跑单（OPENED/TP1_HIT/TP2_HIT）构造拦截
3. A4' 盘口价差预检：spread 超限拦截 / bookTicker 查询异常放行（mock trader）
4. A1'' 保本价含往返费用缓冲：多单/空单 buffer 方向 + 幂等 + 出场原因适配

sqlite 内存库 create_all 建表（SQLAlchemy 2.1 下 PG UUID/JSONB 可编译），
BinanceTrader 以替身注入，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import binance_trader as bt_mod  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _attempt_open,
    _breakeven_stop_price,
    _consecutive_losses_today,
    _derive_exit_reason,
    _move_sl_breakeven,
    try_open_for_analysis,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    """固定配置基线，免疫本地 .env 的 pydantic-settings 覆盖"""
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", True)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0.15)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 6)
    monkeypatch.setattr(settings, "SPREAD_MAX_PCT", 0.003)
    monkeypatch.setattr(settings, "TAKER_FEE_RATE", 0.0005)
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "TRADING_RISK_PCT", 3.0)
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_PCT", 9.0)
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.7)


@pytest.fixture(autouse=True)
def _stub_listed(monkeypatch):
    """_attempt_open 内 local import BinanceTrader() 查上架集合：替身避免真实
    构造（DB 账号解析 / HTTP 客户端）与网络调用；返回空集合=放行（同真实
    「拉取失败放行」语义），后续由替身 trader 的 price 抛错终止流程"""
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


def _ts(min_ago: float) -> datetime:
    return datetime.utcnow() - timedelta(minutes=min_ago)


def _closed(db, pnl, closed_at, account_key="testnet-1", testnet=True):
    """当日已平仓记录（连损计数数据源）"""
    rec = TradeRecord(
        symbol="AAAUSDT", direction="long", status="CLOSED",
        account_key=account_key, testnet=testnet,
        opened_at=closed_at - timedelta(hours=1), closed_at=closed_at,
        realized_pnl=pnl, risk_amount=10.0,
    )
    db.add(rec)
    db.commit()
    return rec


def _running(db, direction="long", risk=40.0, status="OPENED", symbol=None):
    """在跑单记录（热度/同向计数数据源）"""
    rec = TradeRecord(
        symbol=symbol or f"BBB{uuid.uuid4().hex[:6]}USDT",
        direction=direction, status=status,
        account_key="testnet-1", testnet=True,
        opened_at=datetime.utcnow(), risk_amount=risk,
    )
    db.add(rec)
    db.commit()
    return rec


def _make_analysis(db, rec=65, direction="long"):
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction=direction,
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=rec, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _FakeTrader:
    """try_open 闸门链替身：positions/wallet 可用；price 抛错——用于验证
    「闸门链已放行、流程穿透到 _attempt_open」（落「开仓失败」而非前置拦截）"""
    account_key = "testnet-1"
    testnet = True

    def __init__(self, wallet=1000.0, positions=None):
        self._wallet = wallet
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def wallet_balance(self):
        return {"wallet": self._wallet, "available": self._wallet * 0.9}

    def price(self, symbol):
        raise RuntimeError("替身不提供价格（前置闸门应在此前拦截）")


class _SpreadTrader:
    """_attempt_open 盘口价差预检替身：闸门链各环节给最小可用值，
    setup_leverage 抛错终止下单序列（本组测试只验证价差闸门）"""
    account_key = "testnet-1"
    testnet = True

    def __init__(self, bid=99.95, ask=100.05, book_error=False):
        self._bid, self._ask, self._book_error = bid, ask, book_error
        self.setup_calls = []

    def price(self, symbol):
        return 100.0

    def bracket_mmr(self, symbol, notional=None):
        return 0.005

    def bracket_info(self, symbol, notional):
        return None

    def filters(self, symbol):
        return {"step": 0.001, "tick": 0.01, "min_qty": 0.001,
                "min_notional": 5.0, "max_qty": 1e18, "market_max_qty": 1e18}

    def round_qty(self, symbol, qty):
        return round(qty, 3)

    def round_price(self, symbol, price):
        return round(price, 2)

    def market_slippage_estimate(self, symbol, side, qty):
        return None

    def book_ticker(self, symbol):
        if self._book_error:
            raise RuntimeError("bookTicker 不可用")
        return self._bid, self._ask

    def setup_leverage(self, symbol, leverage):
        self.setup_calls.append((symbol, leverage))
        raise RuntimeError("替身在 setup_leverage 终止（本测试只验价差闸门）")


# ── A3：_consecutive_losses_today 计数口径 ────────────────────────────


def test_consecutive_losses_empty(db):
    assert _consecutive_losses_today(db, "testnet-1", True) == 0


def test_consecutive_losses_counting_and_truncation(db):
    """按 closed_at 倒序数连亏：亏3(最近)/亏2/亏1/盈 → 3；最近一笔盈利 → 0"""
    _closed(db, 5.0, _ts(180))
    _closed(db, -1.0, _ts(120))
    _closed(db, -2.0, _ts(60))
    assert _consecutive_losses_today(db, "testnet-1", True) == 2  # 中途盈利截断
    _closed(db, -3.0, _ts(10))
    assert _consecutive_losses_today(db, "testnet-1", True) == 3
    _closed(db, 1.5, _ts(5))
    assert _consecutive_losses_today(db, "testnet-1", True) == 0  # 最近一笔盈利即停


def test_consecutive_losses_scope(db):
    """账号隔离（NULL 按环境回落）+ UTC 日界 + pnl=0 截断"""
    _closed(db, -1.0, _ts(60), account_key="live-1", testnet=False)
    _closed(db, -1.0, _ts(50), account_key="live-1", testnet=False)
    _closed(db, -9.0, _ts(40))
    assert _consecutive_losses_today(db, "testnet-1", True) == 1
    assert _consecutive_losses_today(db, "live-1", False) == 2
    # 昨日亏损不计入当日
    _closed(db, -5.0, datetime.utcnow() - timedelta(days=1))
    assert _consecutive_losses_today(db, "testnet-1", True) == 1
    # pnl=0 不算亏、同样截断
    _closed(db, 0.0, _ts(20))
    assert _consecutive_losses_today(db, "testnet-1", True) == 0


# ── A3：连损降速评分门槛 ──────────────────────────────────────────────


def test_derate_gate_blocks_at_2_losses(db):
    """连损 2 笔：评分 65 ≥ 基础门槛 60 但 < 提升档 75 → _mark_open_block 拦截"""
    _closed(db, -5.0, _ts(30))
    _closed(db, -5.0, _ts(20))
    a = _make_analysis(db, rec=65)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "连损2笔降速" in (a.open_block_reason or "")
    assert "75" in (a.open_block_reason or "")


def test_derate_gate_blocks_at_3_losses(db):
    """连损 ≥3 笔：评分 80 ≥ 75 但 < 提升档 85 → 拦截"""
    for i in range(3):
        _closed(db, -5.0, _ts(30 - i * 5))
    a = _make_analysis(db, rec=80)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "连损3笔降速" in (a.open_block_reason or "")
    assert "85" in (a.open_block_reason or "")


def test_derate_gate_pass_at_2_losses_with_high_score(db):
    """连损 2 笔但评分 80 ≥ 75 → 门槛放行（流程穿透到 _attempt_open 后由替身终止）"""
    _closed(db, -5.0, _ts(30))
    _closed(db, -5.0, _ts(20))
    a = _make_analysis(db, rec=80)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "连损" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_derate_gate_switch_off(db, monkeypatch):
    """开关关闭（False=关闭）：连损 3 笔也不再提升门槛，直接走后续闸门链"""
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    for i in range(3):
        _closed(db, -5.0, _ts(30 - i * 5))
    a = _make_analysis(db, rec=65)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "连损" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


# ── A3'：组合热度上限 / 同向持仓上限 ──────────────────────────────────


def test_heat_cap_blocks(db):
    """在跑单止损合计 160 + 本单预估 30（钱包 1000×3%）= 190 > 1000×15% → 拦截"""
    for i in range(4):
        _running(db, risk=40.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) is False
    assert "组合热度超限" in (a.open_block_reason or "")


def test_heat_cap_within_limit_passes(db):
    """在跑合计 2 + 本单预估 30 = 32 ≤ 150 → 热度放行（穿透到 _attempt_open）"""
    _running(db, risk=1.0)
    _running(db, risk=1.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) is False
    assert "组合热度" not in (a.open_block_reason or "")


def test_direction_cap_blocks(db, monkeypatch):
    """同向（long）在跑 2 单 ≥ 上限 2 → 拦截；反向（short）不受影响"""
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, direction="long", risk=1.0)
    _running(db, direction="long", risk=1.0)
    a = _make_analysis(db, rec=90, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) is False
    assert "同向持仓达上限" in (a.open_block_reason or "")
    # 反方向计数为 0 → 同向闸门放行（穿透到 _attempt_open）
    a2 = _make_analysis(db, rec=90, direction="short")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a2.id) is False
    assert "同向持仓达上限" not in (a2.open_block_reason or "")


def test_direction_cap_counts_all_running_statuses(db, monkeypatch):
    """同向计数含全部在跑状态：TP1_HIT / TP2_HIT 同样计入（区别于单数名额）"""
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, status="TP1_HIT", direction="long", risk=1.0)
    _running(db, status="TP2_HIT", direction="long", risk=1.0)
    a = _make_analysis(db, rec=90, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) is False
    assert "同向持仓达上限" in (a.open_block_reason or "")


# ── A4'：盘口价差预检 ─────────────────────────────────────────────────


def _attempt(db, trader, a):
    return _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {})


def test_spread_too_wide_blocks(db):
    """bid=99 / ask=101 → 点差 2% > 0.3% → 放弃开单，未进入下单序列"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(bid=99.0, ask=101.0)
    assert _attempt(db, trader, a) is False
    assert "盘口价差过宽" in (a.open_block_reason or "")
    assert trader.setup_calls == []


def test_spread_within_limit_reaches_order_sequence(db):
    """点差 0.1% ≤ 0.3% → 放行，流程走到 setup_leverage（替身在此终止）"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(bid=99.95, ask=100.05)
    assert _attempt(db, trader, a) is False
    assert len(trader.setup_calls) == 1
    assert "盘口价差" not in (a.open_block_reason or "")


def test_spread_query_error_passes_through(db):
    """bookTicker 查询异常 → 不拦截放行（走原有滑点实测兜底），未落价差拦截原因"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(book_error=True)
    assert _attempt(db, trader, a) is False
    assert len(trader.setup_calls) == 1
    assert "盘口价差" not in (a.open_block_reason or "")


# ── A1''：保本价含往返费用缓冲 ────────────────────────────────────────


class _BreakevenTrader:
    """保本止损替身：价格取整到 0.01（模拟 tick），现价与挂单回报可配"""

    def __init__(self, price):
        self._price = price
        self.sl_orders = []

    def round_price(self, symbol, price):
        return round(price, 2)

    def price(self, symbol):
        return self._price

    def stop_market_close(self, symbol, side, stop_price):
        self.sl_orders.append((symbol, side, stop_price))
        return {"algoId": 999}


def test_breakeven_price_direction():
    """buffer = entry × 2 × TAKER_FEE_RATE：多单上移到成本价上方、空单下方"""
    long_rec = SimpleNamespace(entry_price=100.0, direction="long")
    short_rec = SimpleNamespace(entry_price=100.0, direction="short")
    assert _breakeven_stop_price(long_rec) == pytest.approx(100.1)
    assert _breakeven_stop_price(short_rec) == pytest.approx(99.9)


def _breakeven_rec(db, direction, stop_loss):
    rec = TradeRecord(
        symbol="BEUSDT", direction=direction, status="TP1_HIT",
        account_key="testnet-1", testnet=True,
        opened_at=datetime.utcnow(), entry_price=100.0, qty=1.0,
        stop_loss=stop_loss, tp1=104.0 if direction == "long" else 96.0,
        risk_amount=5.0,
    )
    db.add(rec)
    db.commit()
    return rec


def test_breakeven_long_moves_above_entry(db):
    """多单：SL 95 → 成本价 100 上方 100.1（含往返费缓冲），挂单方向为 SELL"""
    rec = _breakeven_rec(db, "long", 95.0)
    trader = _BreakevenTrader(price=105.0)
    assert _move_sl_breakeven(db, trader, rec, set()) is True
    assert rec.stop_loss == pytest.approx(100.0 * (1 + 2 * settings.TAKER_FEE_RATE))
    assert rec.stop_loss > float(rec.entry_price)  # 缓冲在成本价上方
    symbol, side, stop = trader.sl_orders[0]
    assert side == "SELL" and stop == pytest.approx(100.1)


def test_breakeven_short_moves_below_entry(db):
    """空单：SL 105 → 成本价 100 下方 99.9（含往返费缓冲），挂单方向为 BUY"""
    rec = _breakeven_rec(db, "short", 105.0)
    trader = _BreakevenTrader(price=95.0)
    assert _move_sl_breakeven(db, trader, rec, set()) is True
    assert rec.stop_loss == pytest.approx(100.0 * (1 - 2 * settings.TAKER_FEE_RATE))
    assert rec.stop_loss < float(rec.entry_price)  # 缓冲在成本价下方
    symbol, side, stop = trader.sl_orders[0]
    assert side == "BUY" and stop == pytest.approx(99.9)


def test_breakeven_idempotent_when_already_beyond_buffer(db):
    """幂等：SL 已不差于缓冲保本价（多单 100.5 > 100.1）时不动作"""
    rec = _breakeven_rec(db, "long", 100.5)
    trader = _BreakevenTrader(price=105.0)
    assert _move_sl_breakeven(db, trader, rec, set()) is False
    assert trader.sl_orders == []
    assert rec.stop_loss == pytest.approx(100.5)


def test_exit_reason_adapts_to_buffered_breakeven(db):
    """出场原因适配（docs/24 A1''）：缓冲保本价（成本价±费缓冲）不再被误判为
    tp1_trail，仍归 breakeven_sl；越过缓冲价才算跟踪止损出场"""
    long_be = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                              stop_loss=100.1, direction="long")
    short_be = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                               stop_loss=99.9, direction="short")
    assert _derive_exit_reason(long_be) == "breakeven_sl"
    assert _derive_exit_reason(short_be) == "breakeven_sl"
    long_trail = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                                 stop_loss=101.0, direction="long")
    short_trail = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                                  stop_loss=99.0, direction="short")
    assert _derive_exit_reason(long_trail) == "tp1_trail"
    assert _derive_exit_reason(short_trail) == "tp1_trail"
