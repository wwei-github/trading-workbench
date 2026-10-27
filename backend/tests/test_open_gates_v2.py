# -*- coding: utf-8 -*-
"""开仓闸门 v2 单测（docs/24 第 2 批）

覆盖：
1. A3 连损降速：_consecutive_losses_today 计数口径（0/2/3 档、盈利截断、
   账号隔离、日界）+ try_open_for_analysis 评分门槛提升拦截 + 开关关闭不拦截
2. A3' 组合热度上限 / 同向持仓上限：在跑单（OPENED/TP1_HIT/TP2_HIT）构造拦截
3. A4' 盘口价差预检：spread 超限拦截 / bookTicker 查询异常放行（mock trader）
4. A1'' 保本价含往返费用缓冲：多单/空单 buffer 方向 + 幂等 + 出场原因适配
5. 当日亏损熔断闸门（2026-09-30 开关化）：达阈值拦截 + 开关关闭放行

sqlite 内存库 create_all 建表（SQLAlchemy 2.1 下 PG UUID/JSONB 可编译），
BinanceTrader 以替身注入，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import PendingEntryOrder, TradeEvent, TradeRecord  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _adaptive_leverage,
    _attempt_open,
    _breakeven_stop_price,
    _capital_base,
    _consecutive_losses_today,
    _cost_adjusted_stop_pct,
    _daily_loss_cap,
    _derive_exit_reason,
    _move_sl_breakeven,
    materialize_pending_entry,
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
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_ENABLED", True)
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.7)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


def _ts(min_ago: float) -> datetime:
    """N 分钟前。日期炸弹修复（2026-10-07）：UTC 0点后 3h 内运行时
    「180分钟前」落到昨日，_consecutive_losses_today 的当日断言全挂——
    钳制基准到当日 03:00（相对顺序不变，closed_at 恒为今日）。"""
    now = datetime.utcnow()
    floor = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now < floor:
        return floor - timedelta(minutes=min_ago)
    return now - timedelta(minutes=min_ago)


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


def _running(db, direction="long", risk=40.0, status="OPENED", symbol=None,
             remaining=None, beta=None):
    """在跑单记录（热度/同向计数数据源）

    remaining=None 表示旧行（未迁移/测试未显式给出）——热度回落 risk_amount；
    remaining 显式给出用于验证 T4「按剩余风险而非开仓风险」口径。
    """
    rec = TradeRecord(
        symbol=symbol or f"BBB{uuid.uuid4().hex[:6]}USDT",
        direction=direction, status=status,
        account_key="testnet-1", testnet=True,
        opened_at=datetime.utcnow(), risk_amount=risk,
        initial_risk=risk, remaining_risk=remaining, beta=beta,
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

    def listed_symbols(self):
        # 本单账号 trader 直接调用（2026-10-03）：空集合=放行（同「拉取失败放行」语义）
        return set()

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

    def listed_symbols(self):
        # 本单账号 trader 直接调用（2026-10-03）：空集合=放行（同「拉取失败放行」语义）
        return set()

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
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "连损2笔降速" in (a.open_block_reason or "")
    assert "75" in (a.open_block_reason or "")


def test_derate_gate_blocks_at_3_losses(db):
    """连损 ≥3 笔：评分 80 ≥ 75 但 < 提升档 85 → 拦截"""
    for i in range(3):
        _closed(db, -5.0, _ts(30 - i * 5))
    a = _make_analysis(db, rec=80)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "连损3笔降速" in (a.open_block_reason or "")
    assert "85" in (a.open_block_reason or "")


def test_derate_gate_pass_at_2_losses_with_high_score(db):
    """连损 2 笔但评分 80 ≥ 75 → 门槛放行（流程穿透到 _attempt_open 后由替身终止）"""
    _closed(db, -5.0, _ts(30))
    _closed(db, -5.0, _ts(20))
    a = _make_analysis(db, rec=80)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "连损" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


def test_derate_gate_switch_off(db, monkeypatch):
    """开关关闭（False=关闭）：连损 3 笔也不再提升门槛，直接走后续闸门链"""
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    for i in range(3):
        _closed(db, -5.0, _ts(30 - i * 5))
    a = _make_analysis(db, rec=65)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "连损" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


# ── A3'：组合热度上限 / 同向持仓上限 ──────────────────────────────────


def test_heat_cap_blocks(db):
    """在跑单止损合计 160 + 本单预估 30（钱包 1000×3%）= 190 > 1000×15% → 拦截"""
    for i in range(4):
        _running(db, risk=40.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" in (a.open_block_reason or "")


def test_heat_cap_within_limit_passes(db):
    """在跑合计 2 + 本单预估 30 = 32 ≤ 150 → 热度放行（穿透到 _attempt_open）"""
    _running(db, risk=1.0)
    _running(db, risk=1.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" not in (a.open_block_reason or "")


def test_heat_uses_remaining_risk_after_partial_tp(db):
    """T4：TP 后剩余风险缩小 → 不再按开仓时 risk_amount 误拦。

    4 笔开仓风险各 40（旧口径 160+30 > 150 必拦），剩余风险已缩到 1
    （新口径 4+30 = 34 ≤ 150）→ 热度闸门放行。
    """
    for _ in range(4):
        _running(db, risk=40.0, status="TP1_HIT", remaining=1.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" not in (a.open_block_reason or "")


def test_heat_btc_exposure_applies_beta(db):
    """T4：BTC 因子暴露 = 剩余风险 × β；β=2 的 70 → 140 + 本单 30 > 150 拦截"""
    _running(db, risk=70.0, remaining=70.0, beta=2.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" in (a.open_block_reason or "")


def test_heat_btc_exposure_beta_missing_falls_back_to_one(db):
    """T4：β 缺失按 1.0（70 + 30 = 100 ≤ 150 放行），不得因 None 崩溃或放大"""
    _running(db, risk=70.0, remaining=70.0, beta=None)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" not in (a.open_block_reason or "")


def test_direction_cap_blocks(db, monkeypatch):
    """同向（long）在跑 2 单 ≥ 上限 2 → 拦截；反向（short）不受影响"""
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, direction="long", risk=1.0)
    _running(db, direction="long", risk=1.0)
    a = _make_analysis(db, rec=90, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "同向持仓达上限" in (a.open_block_reason or "")
    # 反方向计数为 0 → 同向闸门放行（穿透到 _attempt_open）
    a2 = _make_analysis(db, rec=90, direction="short")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a2.id) == "blocked"
    assert "同向持仓达上限" not in (a2.open_block_reason or "")


def test_direction_cap_counts_all_running_statuses(db, monkeypatch):
    """同向计数含全部在跑状态：TP1_HIT / TP2_HIT 同样计入（区别于单数名额）"""
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, status="TP1_HIT", direction="long", risk=1.0)
    _running(db, status="TP2_HIT", direction="long", risk=1.0)
    a = _make_analysis(db, rec=90, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "同向持仓达上限" in (a.open_block_reason or "")


# ── 当日亏损熔断闸门（2026-09-30 开关化） ────────────────────────────


def test_daily_loss_stop_blocks(db):
    """当日已平仓净亏 -100 ≤ -阈值 90（基数 1000 × 9%）→ 硬熔断拦截停开仓

    平仓时间锚定 utcnow（而非 _ts 回推）——回推记录在 UTC 日界后一段时间内
    会落到昨日、被日界口径排除，测试会偶发误失败。
    """
    _closed(db, -50.0, datetime.utcnow())
    _closed(db, -50.0, datetime.utcnow())
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "当日亏损熔断" in (a.open_block_reason or "")
    assert "-90" in (a.open_block_reason or "")  # 阈值随账户缩放：1000 × 9%


def test_daily_loss_stop_switch_off(db, monkeypatch):
    """开关关闭（DAILY_LOSS_STOP_ENABLED=False）：同样净亏 -100 不再拦截，
    穿透到 _attempt_open（由替身终止）"""
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_ENABLED", False)
    _closed(db, -50.0, datetime.utcnow())
    _closed(db, -50.0, datetime.utcnow())
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "熔断" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


# ── A4'：盘口价差预检 ─────────────────────────────────────────────────


def _attempt(db, trader, a):
    return _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {})


def test_spread_too_wide_blocks(db):
    """bid=99 / ask=101 → 点差 2% > 0.3% → 放弃开单，未进入下单序列"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(bid=99.0, ask=101.0)
    assert _attempt(db, trader, a) == "blocked"
    assert "盘口价差过宽" in (a.open_block_reason or "")
    assert trader.setup_calls == []


def test_spread_within_limit_reaches_order_sequence(db):
    """点差 0.1% ≤ 0.3% → 放行，流程走到 setup_leverage（替身在此终止）"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(bid=99.95, ask=100.05)
    assert _attempt(db, trader, a) == "blocked"
    assert len(trader.setup_calls) == 1
    assert "盘口价差" not in (a.open_block_reason or "")


def test_spread_query_error_passes_through(db):
    """bookTicker 查询异常 → 不拦截放行（走原有滑点实测兜底），未落价差拦截原因"""
    a = _make_analysis(db, rec=90)
    trader = _SpreadTrader(book_error=True)
    assert _attempt(db, trader, a) == "blocked"
    assert len(trader.setup_calls) == 1
    assert "盘口价差" not in (a.open_block_reason or "")


# ── 可用保证金闸门：计入开仓 taker 手续费（2026-10-19 -2019 加固）───────────


def test_open_margin_required_includes_taker_fee():
    """保证金需求 = 初始保证金 + 开仓 taker 手续费：
    20×100/20=100（初始保证金）+ 2000×0.0005=1（手续费）→ 101"""
    from app.services.trade_engine import _open_margin_required
    assert _open_margin_required(20.0, 100.0, 20) == pytest.approx(101.0)


def _fee_gate_fixture(db, monkeypatch):
    """固定杠杆（打桩 _adaptive_leverage=20）+ 固定金额风险 41.98 →
    有效止损距离 0.02099（2% + 双边费）下名义 2000、数量 20，
    保证金需求恒为 101（初始 100 + 手续费 1），便于按 available 精确切边界"""
    import app.services.trade_engine as te
    monkeypatch.setattr(te, "_adaptive_leverage", lambda *a, **k: 20)
    a = _make_analysis(db, rec=90)
    cfg = db.get(SystemConfig, 1)
    cfg.risk_budget_mode = "fixed"
    cfg.risk_fixed_amount = 41.98
    db.commit()
    return a, cfg


def test_margin_gate_blocks_when_fee_tips_over_available(db, monkeypatch):
    """可用 100.4 ≥ 初始保证金 100 但 < 需求 101（+手续费）→ 拦为「可用保证金不足」，
    不进入下单序列——此前只比初始保证金会放行，随后被交易所按「+手续费」拒单 -2019"""
    a, cfg = _fee_gate_fixture(db, monkeypatch)
    trader = _SpreadTrader()
    assert _attempt_open(db, trader, cfg, a, 100.4, {}) == "blocked"
    assert "可用保证金不足" in (a.open_block_reason or "")
    assert trader.setup_calls == []


def test_margin_gate_passes_with_available_over_needed(db, monkeypatch):
    """可用 101 ≥ 需求 101 → 放行进入下单序列（替身在 setup_leverage 终止）"""
    a, cfg = _fee_gate_fixture(db, monkeypatch)
    trader = _SpreadTrader()
    assert _attempt_open(db, trader, cfg, a, 101.0, {}) == "blocked"
    assert len(trader.setup_calls) == 1
    assert "可用保证金不足" not in (a.open_block_reason or "")


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
    # raw=None（2026-10-12 起 _derive_exit_reason 不再分流 trail 单）
    long_be = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                              stop_loss=100.1, direction="long", raw=None)
    short_be = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                               stop_loss=99.9, direction="short", raw=None)
    assert _derive_exit_reason(long_be) == "breakeven_sl"
    assert _derive_exit_reason(short_be) == "breakeven_sl"
    long_trail = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                                 stop_loss=101.0, direction="long", raw=None)
    short_trail = SimpleNamespace(status="TP1_HIT", entry_price=100.0,
                                  stop_loss=99.0, direction="short", raw=None)
    assert _derive_exit_reason(long_trail) == "tp1_trail"
    assert _derive_exit_reason(short_trail) == "tp1_trail"


# ── ladder 开仓全流程（KAIAUSDT 案例回归 + 对照） ─────────────────────
# 回归锁：_attempt_open 的 tp1_order=None 守卫——raw 快照落 .get 时不得崩
# 'NoneType' object has no attribute 'get'


class _FullOpenTrader:
    """完整下单序列替身：走通 市价开仓→挂SL→挂TP→落库 全链"""
    account_key = "testnet-1"
    testnet = True

    def __init__(self):
        self.tp_calls = []

    def listed_symbols(self):
        return set()

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
        return 99.95, 100.05

    def setup_leverage(self, symbol, leverage):
        pass

    def market_order(self, symbol, side, qty, reduce_only=False):
        return {"orderId": 11}

    def order(self, symbol, order_id):
        return {"avgPrice": "100.0", "executedQty": "15.0"}

    def stop_market_close(self, symbol, side, stop_price):
        return {"algoId": 22}

    def take_profit_reduce(self, symbol, side, qty, price):
        self.tp_calls.append((symbol, side, qty, price))
        return {"algoId": 33}

    def cancel_all_algo(self, symbol):
        pass


def test_ladder_open_records_tp_orders(db):
    """ladder 路径：开仓成功、挂 TP1 单、raw 记挂单 id（tp1_order=None 守卫不改变原有行为）"""
    a = _make_analysis(db, rec=90)
    trader = _FullOpenTrader()
    assert _attempt(db, trader, a) == "opened"
    assert len(trader.tp_calls) == 1  # TP1 挂单
    rec = db.query(TradeRecord).filter(TradeRecord.ai_analysis_id == a.id).one()
    assert rec.raw["tp1_order_id"] == 33


class _PendingTrader(_FullOpenTrader):
    """挂单入场替身（docs/27）：现价对 AI 入场价有利漂移 → 写单不满足盈亏比，
    改在入场价挂 GTC 限价单。price=102 使多头 rr_now=(104-102)/(102-98)=0.5 < 1.5"""

    def price(self, symbol):
        return 102.0

    def limit_order(self, symbol, side, qty, price, time_in_force="GTC"):
        return {"orderId": 77}


def test_rr_shortfall_places_limit_pending(db):
    """现价 RR 不足 → 返回 pending：不建 TradeRecord，落一条 pending_entry_orders，
    限价单在 AI 入场价、状态 pending（docs/27）"""
    from app.models.trade import PendingEntryOrder
    a = _make_analysis(db, rec=90)
    trader = _PendingTrader()
    cfg = db.get(SystemConfig, 1)
    assert _attempt_open(db, trader, cfg, a, 1000.0, {}, force_market=True) == "pending"
    assert db.query(TradeRecord).filter(TradeRecord.ai_analysis_id == a.id).count() == 0
    pe = db.query(PendingEntryOrder).filter(PendingEntryOrder.ai_analysis_id == a.id).one()
    assert pe.status == "pending"
    assert pe.entry_price == pytest.approx(100.0)
    assert pe.limit_order_id == "77"
    assert "挂单" in (a.open_block_reason or "")


# ── F1：成本感知仓位计价（2026-10-27） ───────────────────────────────


def test_capital_base_never_exceeds_wallet():
    """F2：风险基数不超过本金——50 → 50（旧口径 100）、99.99 → 99.99，
    ≥100 的档位行为不变（100→100、999.99→900、4800→4000、5200→5000）"""
    assert _capital_base(50.0) == pytest.approx(50.0)
    assert _capital_base(99.99) == pytest.approx(99.99)
    assert _capital_base(100.0) == pytest.approx(100.0)
    assert _capital_base(999.99) == pytest.approx(900.0)
    assert _capital_base(4800.0) == pytest.approx(4000.0)
    assert _capital_base(5200.0) == pytest.approx(5000.0)
    assert _daily_loss_cap(50.0, pct=9.0) == pytest.approx(4.5)


def test_cost_adjusted_stop_pct_includes_round_trip_fee():
    """F1：有效止损距离 = 价格距离 + fee×(ref+sl)，多空同式"""
    eff = _cost_adjusted_stop_pct(100.0, 99.5, fee_rate=0.0005)
    assert eff == pytest.approx((0.5 + 0.0005 * 199.5) / 100.0)
    # 空单（止损在上方）距离相同，费用基数 ref+sl 略大（200.5 vs 199.5）
    short_eff = _cost_adjusted_stop_pct(100.0, 100.5, fee_rate=0.0005)
    assert short_eff == pytest.approx((0.5 + 0.0005 * 200.5) / 100.0)
    # 多空差异仅来自费用基数（ref+sl 相差一个止损距离，约 5e-6）
    assert short_eff == pytest.approx(eff, abs=1e-5)
    assert _cost_adjusted_stop_pct(0.0, 99.0) == 0.0


def test_pending_sizing_uses_cost_adjusted_distance(db):
    """F1：挂单仓位按含费距离计算（30/0.02099/100≈14.29，旧口径 15），
    且强平闸门计入滑点余量 → 杠杆低于上限 20×"""
    a = _make_analysis(db, rec=90)
    trader = _PendingTrader()
    cfg = db.get(SystemConfig, 1)
    assert _attempt_open(db, trader, cfg, a, 1000.0, {}, force_market=True) == "pending"
    pe = db.query(PendingEntryOrder).filter(
        PendingEntryOrder.ai_analysis_id == a.id).one()
    stop_eff = _cost_adjusted_stop_pct(100.0, 98.0)
    assert float(pe.qty) == pytest.approx(round(30.0 / stop_eff / 100.0, 3))
    assert float(pe.qty) < 15.0  # 旧口径 30/2%/100 = 15
    # 杠杆闸门按「含费距离 + 最大滑点」判强平距离：3.8% 止损在 20×（闸门 4.0%）内放行，
    # 叠加 0.3% 滑点余量后 4.1% 越线 → 降到 19×（不含滑点会误判为 20× 安全）
    assert _adaptive_leverage(0.038, 0.005, 0.005, 20) == 20
    assert _adaptive_leverage(0.038 + 0.003, 0.005, 0.005, 20) == 19


# ── F3：活跃挂单纳入组合闸门 + 成交落地复核（2026-10-27） ─────────────


def _pending_row(db, risk=40.0, direction="long", beta=None,
                 ai_analysis_id=None, symbol=None):
    pe = PendingEntryOrder(
        symbol=symbol or f"PPP{uuid.uuid4().hex[:6]}USDT",
        direction=direction, account_key="testnet-1", testnet=True,
        entry_price=100.0, stop_loss=98.0, tp1=104.0, tp2=0.0,
        qty=2.0, leverage=10, margin_used=20.0, risk_amount=risk,
        limit_order_id="11", status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        raw={"beta": beta},
    )
    db.add(pe)
    db.commit()
    return pe


def test_heat_cap_counts_active_pending(db):
    """在跑 100 + 挂单 60 + 本单预估 30 = 190 > 1000×15% → 热度拦截。
    只算在跑时 100+30=130 ≤ 150 会放行。"""
    _running(db, risk=100.0)
    _pending_row(db, risk=60.0)
    a = _make_analysis(db, rec=90)
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "组合热度超限" in (a.open_block_reason or "")


def test_direction_cap_counts_active_pending(db, monkeypatch):
    """在跑 long 1 + 挂单 long 1 = 2 ≥ 同向上限 2 → 拦截"""
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, direction="long", risk=1.0)
    _pending_row(db, direction="long", risk=1.0)
    a = _make_analysis(db, rec=90, direction="long")
    assert try_open_for_analysis(db, _FakeTrader(wallet=1000.0), a.id) == "blocked"
    assert "同向持仓达上限" in (a.open_block_reason or "")


class _GateTrader:
    """挂单成交落地替身：成交/保护单全成功，用于验证 GATE_BREACH 留痕"""

    account_key = "testnet-1"
    testnet = True

    def cancel_regular_order(self, symbol, order_id):
        return None

    def order(self, symbol, order_id):
        return {"executedQty": 2.0, "avgPrice": 100.0}

    def setup_leverage(self, symbol, leverage):
        return None

    def filters(self, symbol):
        return {"step": 0.001, "tick": 0.01, "min_qty": 0.001,
                "min_notional": 5.0, "max_qty": 1e6, "market_max_qty": 1e6}

    def round_qty(self, symbol, qty):
        return round(qty, 3)

    def round_price(self, symbol, price):
        return round(price, 2)

    def stop_market_close(self, symbol, side, stop_price):
        return {"algoId": 1}

    def take_profit_reduce(self, symbol, side, qty, price):
        return {"algoId": 2}

    def market_order(self, symbol, side, qty, reduce_only=False):
        return {"orderId": 3}


def test_pending_materialize_records_gate_breach(db):
    """挂单成交时组合热度已越线 → 保护单照挂，落 GATE_BREACH 事件供归因"""
    from app.models.system_config import SystemConfig as _SC
    cfg = db.get(_SC, 1)
    _running(db, risk=200.0)  # 200 + 本单 4 = 204 > 1000×15% = 150
    pe = _pending_row(db, risk=8.0)
    pe.raw = {"wallet": 1000.0, "beta": 1.0}
    db.commit()
    rid = materialize_pending_entry(db, _GateTrader(), pe, cfg)
    assert rid is not None
    ev = db.query(TradeEvent).filter(
        TradeEvent.trade_record_id == rid,
        TradeEvent.event_type == "GATE_BREACH").one()
    assert "组合热度超限" in ev.detail["reasons"][0]
    assert db.get(TradeRecord, rid).status == "OPENED"


def test_pending_materialize_within_caps_writes_no_breach(db):
    """复核通过（10 万钱包、无在跑单）→ 正常落地且不写 GATE_BREACH"""
    cfg = db.get(SystemConfig, 1)
    pe = _pending_row(db, risk=8.0)
    pe.raw = {"wallet": 100000.0, "beta": 1.0}
    db.commit()
    rid = materialize_pending_entry(db, _GateTrader(), pe, cfg)
    assert rid is not None
    assert db.get(TradeRecord, rid).status == "OPENED"
    assert db.query(TradeEvent).filter(
        TradeEvent.trade_record_id == rid,
        TradeEvent.event_type == "GATE_BREACH").count() == 0


def test_pending_gate_review_failure_does_not_block_protection(db, monkeypatch):
    """复核查询报错（如未跑迁移）由 SAVEPOINT 隔离：仓位照常落地、可提交、
    不写越线事件——不得因复核失败触发外层紧急平仓"""
    import app.services.trade_engine as te
    cfg = db.get(SystemConfig, 1)
    pe = _pending_row(db, risk=8.0)
    pe.raw = {"wallet": 1000.0, "beta": 1.0}
    db.commit()

    def _boom(*args, **kwargs):
        db.execute(text("SELECT * FROM table_that_does_not_exist"))

    monkeypatch.setattr(te, "_pending_gate_breaches", _boom)
    rid = materialize_pending_entry(db, _GateTrader(), pe, cfg)
    assert rid is not None
    assert db.get(TradeRecord, rid).status == "OPENED"
    assert db.query(TradeEvent).filter(
        TradeEvent.trade_record_id == rid,
        TradeEvent.event_type == "GATE_BREACH").count() == 0


def test_pending_gate_review_applies_week_derate(db, monkeypatch):
    """周减速生效时同向上限减半：在跑 long 1 + 本单 1 = 2 > 1 → 越线留痕"""
    now = datetime.utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    _closed(db, -200.0, week_start + (now - week_start) / 2)  # 周净亏 ≥ 1000×15%
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 2)
    _running(db, direction="long", risk=1.0)
    cfg = db.get(SystemConfig, 1)
    pe = _pending_row(db, risk=4.0, direction="long")
    pe.raw = {"wallet": 1000.0, "beta": 1.0}
    db.commit()
    rid = materialize_pending_entry(db, _GateTrader(), pe, cfg)
    ev = db.query(TradeEvent).filter(
        TradeEvent.trade_record_id == rid,
        TradeEvent.event_type == "GATE_BREACH").one()
    assert any("同向持仓达上限" in r and "2 单 > 1" in r for r in ev.detail["reasons"])
