import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  确保全部模型注册进 Base.metadata
from app.database import Base
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import PendingEntryOrder, ProtectionIncident, TradeRecord
from app.services.protection_recovery import (
    create_incident_after_rollback,
    has_active_incident,
    recover_due_incidents,
)
from app.services.trade_engine import (
    _attempt_open,
    materialize_pending_entry,
    try_open_for_analysis,
)


@pytest.fixture(autouse=True)
def _stub_alert(monkeypatch):
    """单测不触达真实飞书 webhook；需要断言告警的用例自行覆盖该桩。"""
    monkeypatch.setattr("app.services.protection_recovery._alert", lambda row: None)


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[TradeRecord.__table__, ProtectionIncident.__table__])
    return lambda: Session(bind=engine)


def incident(db, **kw):
    values = dict(account_key="testnet-1", testnet=True, symbol="XUSDT",
                  direction="long", reason="open_protection_failed", status="pending",
                  qty=2, created_at=datetime.utcnow(), next_retry_at=datetime.utcnow())
    values.update(kw)
    row = ProtectionIncident(**values)
    db.add(row)
    db.commit()
    return row


def test_incident_is_saved_in_fresh_transaction_after_rollback(db_factory, monkeypatch):
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", db_factory)
    db = db_factory()
    db.add(TradeRecord(id=uuid.uuid4(), symbol="discarded", direction="long", status="OPENED"))
    db.rollback()
    row = create_incident_after_rollback(
        db, account_key="testnet-1", testnet=True, symbol="XUSDT", direction="long",
        reason="open_protection_failed", qty=2, trade_record_id=None,
        entry_order_id="e-1", error="close timed out",
    )
    assert row.id is not None
    assert has_active_incident(db, "testnet-1", True)
    db.close()


def test_recovery_does_not_close_position_with_ambiguous_generation(db_factory):
    db = db_factory()
    item = incident(db)
    calls = []
    trader = SimpleNamespace(positions=lambda: {"XUSDT": {"amt": 2}},
                             market_order=lambda *a, **k: calls.append((a, k)))
    recover_due_incidents(db, trader, "testnet-1", True,
                          generation_matches=lambda row, position: False)
    db.refresh(item)
    assert calls == []
    assert item.status == "manual_review"
    assert has_active_incident(db, "testnet-1", True)
    db.close()


def test_recovery_closes_only_confirmed_incident_position(db_factory):
    db = db_factory()
    item = incident(db)
    calls = []
    trader = SimpleNamespace(positions=lambda: {"XUSDT": {"amt": 2}},
                             market_order=lambda *a, **k: calls.append((a, k)) or {"orderId": 9})
    recover_due_incidents(db, trader, "testnet-1", True,
                          generation_matches=lambda row, position: True)
    db.refresh(item)
    assert calls == [(('XUSDT', 'SELL', 2), {'reduce_only': True})]
    assert item.status == "awaiting_reconcile"
    assert has_active_incident(db, "testnet-1", True)
    recover_due_incidents(db, trader, "testnet-1", True,
                          generation_matches=lambda row, position: True,
                          now=datetime.utcnow() + timedelta(minutes=2))
    assert len(calls) == 1
    assert item.status == "manual_review"
    assert has_active_incident(db, "testnet-1", True)
    db.close()


def test_uncertain_close_is_never_resent(db_factory):
    db = db_factory()
    item = incident(db, status="awaiting_reconcile", next_retry_at=datetime.utcnow(),
                    attempt_count=1)
    calls = []
    trader = SimpleNamespace(positions=lambda: {"XUSDT": {"amt": 2}},
                             market_order=lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(TimeoutError("ack lost")))
    recover_due_incidents(db, trader, "testnet-1", True,
                          generation_matches=lambda row, position: True)
    db.refresh(item)
    assert calls == []
    assert item.status == "manual_review"
    assert has_active_incident(db, "testnet-1", True)
    db.close()


def test_position_read_failure_keeps_pending_retryable(db_factory):
    db = db_factory()
    item = incident(db)
    def fail_positions():
        raise OSError("temporary REST read failure")
    trader = SimpleNamespace(positions=fail_positions)
    recover_due_incidents(db, trader, "testnet-1", True,
                          now=datetime.utcnow() + timedelta(minutes=1))
    db.refresh(item)
    assert item.status == "pending"
    assert "可重试" in item.last_error
    assert has_active_incident(db, "testnet-1", True)
    db.close()


def test_explicit_reduce_only_rejection_can_retry(db_factory):
    db = db_factory()
    item = incident(db)
    calls = []
    class Trader:
        def positions(self):
            return {"XUSDT": {"amt": 2}}
        def market_order(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("-2022 ReduceOnly Order is rejected")
    trader = Trader()
    matcher = lambda row, pos: True
    recover_due_incidents(db, trader, "testnet-1", True, generation_matches=matcher)
    db.refresh(item)
    assert item.status == "pending"
    assert len(calls) == 1
    recover_due_incidents(db, trader, "testnet-1", True, generation_matches=matcher,
                          now=datetime.utcnow() + timedelta(minutes=2))
    assert len(calls) == 2
    db.close()


def test_manual_review_is_released_when_position_is_gone(db_factory):
    """Manual review must still reconcile: a human/exchange close releases the block."""
    db = db_factory()
    item = incident(db, status="manual_review", attempt_count=3,
                    next_retry_at=datetime.utcnow() + timedelta(hours=1))
    trader = SimpleNamespace(positions=lambda: {})
    recover_due_incidents(db, trader, "testnet-1", True,
                          now=datetime.utcnow() + timedelta(hours=2))
    db.refresh(item)
    assert item.status == "resolved"
    assert not has_active_incident(db, "testnet-1", True)
    db.close()


def test_flat_position_releases_before_hourly_backoff(db_factory):
    """空仓解除不受 manual_review 退避影响：下一轮巡检立即解封。"""
    db = db_factory()
    item = incident(db, status="manual_review", attempt_count=3,
                    next_retry_at=datetime.utcnow() + timedelta(hours=1))
    trader = SimpleNamespace(positions=lambda: {})
    recover_due_incidents(db, trader, "testnet-1", True, now=datetime.utcnow())
    db.refresh(item)
    assert item.status == "resolved"
    assert item.resolved_at is not None
    assert not has_active_incident(db, "testnet-1", True)
    db.close()


def test_generation_falls_back_to_trade_record_entry_price(db_factory):
    """事故行缺 entry_price 时用关联交易记录兜底，避免永久 manual_review。"""
    db = db_factory()
    rec = TradeRecord(id=uuid.uuid4(), symbol="XUSDT", direction="long",
                      status="OPENED", entry_price=100, qty=2)
    db.add(rec)
    db.commit()
    item = incident(db, entry_price=None, trade_record_id=rec.id)
    calls = []
    trader = SimpleNamespace(
        positions=lambda: {"XUSDT": {"amt": 2, "entry_price": 100}},
        market_order=lambda *a, **k: calls.append((a, k)) or {"orderId": 9},
    )
    recover_due_incidents(db, trader, "testnet-1", True)
    db.refresh(item)
    assert calls == [(("XUSDT", "SELL", 2), {"reduce_only": True})]
    assert item.status == "awaiting_reconcile"
    db.close()


def test_generation_mismatch_alerts_once_and_backs_off_hourly(
    db_factory, monkeypatch,
):
    """世代不匹配仅在首次升级时告警，之后每小时静默复查，不形成告警风暴。"""
    db = db_factory()
    alerts = []
    monkeypatch.setattr("app.services.protection_recovery._alert", alerts.append)
    due = datetime.utcnow()
    item = incident(db, next_retry_at=due)
    trader = SimpleNamespace(
        positions=lambda: {"XUSDT": {"amt": 2, "entry_price": 105}},
    )
    recover_due_incidents(db, trader, "testnet-1", True, now=due)
    db.refresh(item)
    assert item.status == "manual_review"
    assert len(alerts) == 1
    assert item.next_retry_at > due + timedelta(minutes=30)

    recover_due_incidents(db, trader, "testnet-1", True,
                          now=due + timedelta(minutes=2))
    db.refresh(item)
    assert len(alerts) == 1
    assert item.status == "manual_review"
    db.close()


def test_manual_review_backs_off_without_alert_storm(db_factory, monkeypatch):
    """Still-open manual review is polled hourly, not rewritten every minute."""
    db = db_factory()
    due = datetime.utcnow()
    item = incident(db, status="manual_review", attempt_count=3, next_retry_at=due)
    alerts = []
    monkeypatch.setattr("app.services.protection_recovery._alert", alerts.append)
    trader = SimpleNamespace(positions=lambda: {"XUSDT": {"amt": 2}})
    recover_due_incidents(db, trader, "testnet-1", True,
                          generation_matches=lambda row, pos: True, now=due)
    db.refresh(item)
    assert item.status == "manual_review"
    assert item.attempt_count == 3
    assert item.next_retry_at > due + timedelta(minutes=30)
    assert alerts == []
    db.close()


def test_close_error_classification_is_conservative():
    from app.services.protection_recovery import status_for_close_error

    assert status_for_close_error("-2022 ReduceOnly Order is rejected") == "pending"
    assert status_for_close_error("read timed out") == "manual_review"
    assert status_for_close_error("-1000 unknown error") == "manual_review"
    assert status_for_close_error(None) == "manual_review"


def test_active_incident_is_account_scoped(db_factory):
    db = db_factory()
    incident(db, account_key="testnet-1", testnet=True)
    assert has_active_incident(db, "testnet-1", True)
    assert not has_active_incident(db, "testnet-2", True)
    assert not has_active_incident(db, "testnet-1", False)
    db.close()


def _protected_trader(positions=None, orders=None):
    calls = []

    class Trader:
        def positions(self):
            return positions if positions is not None else {"XUSDT": {"amt": 2, "entry_price": 100}}

        def price(self, symbol):
            return 100.0

        def open_algo_orders(self, symbol):
            return orders or []

        def market_order(self, *args, **kwargs):
            calls.append((args, kwargs))

    return Trader(), calls


def test_recovery_resolves_when_stop_is_restored(db_factory):
    """后续补挂/人工单恢复保护后，事故解除且不再重复平仓。"""
    db = db_factory()
    item = incident(db, entry_price=100)
    trader, calls = _protected_trader(orders=[{
        "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": 95,
        "closePosition": "true",
    }])
    recover_due_incidents(db, trader, "testnet-1", True)
    db.refresh(item)
    assert calls == []
    assert item.status == "resolved"
    assert item.resolved_at is not None
    assert not has_active_incident(db, "testnet-1", True)
    db.close()


def test_wrong_side_stop_does_not_count_as_protection(db_factory):
    """触发价在现价错误侧（会立即触发/不起保护作用）不能解除事故。"""
    db = db_factory()
    item = incident(db, entry_price=100)
    trader, calls = _protected_trader(
        positions={"XUSDT": {"amt": 2, "entry_price": 100}},
        orders=[{"side": "SELL", "orderType": "STOP_MARKET",
                 "triggerPrice": 105, "closePosition": "true"}],
    )
    recover_due_incidents(db, trader, "testnet-1", True)
    db.refresh(item)
    assert len(calls) == 1
    assert item.status == "awaiting_reconcile"
    db.close()


# ---------- 故障注入：开仓成交后保护失败 ----------


@pytest.fixture
def file_session(tmp_path):
    """独立连接的文件库：恢复服务用新会话落库，与失败事务真正隔离。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'protection.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    yield maker
    engine.dispose()


@pytest.fixture
def opening_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "TRADING_RISK_PCT", 3.0)
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.7)
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "AI_RR_TOL", 0.05)
    monkeypatch.setattr(settings, "TRADING_LEVERAGE", 20)
    monkeypatch.setattr(settings, "TRADING_LEVERAGE_MIN", 3)
    monkeypatch.setattr(settings, "SPREAD_MAX_PCT", 0.003)
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", False)
    monkeypatch.setattr(settings, "TAKER_FEE_RATE", 0.0005)


class _OpeningTrader:
    """开仓链路替身：放行闸门，SL 挂单必败；可配置紧急平仓结果。"""

    account_key = "testnet-1"
    testnet = True

    def __init__(self, close_error=None):
        self.close_error = close_error
        self.cancel_algo_calls = 0

    def listed_symbols(self):
        return {"XUSDT"}

    def positions(self):
        return {}

    def price(self, symbol):
        return 100.0

    def bracket_mmr(self, symbol, notional=None):
        return 0.005

    def bracket_info(self, symbol, notional):
        return None

    def filters(self, symbol):
        return {"step": 0.001, "tick": 0.01, "min_qty": 0.001,
                "min_notional": 5.0, "max_qty": 1e6, "market_max_qty": 1e6}

    def round_qty(self, symbol, qty):
        return round(qty, 3)

    def round_price(self, symbol, price):
        return round(price, 2)

    def market_slippage_estimate(self, symbol, side, qty):
        return None

    def book_ticker(self, symbol):
        return 99.99, 100.01

    def setup_leverage(self, symbol, leverage):
        return None

    def market_order(self, symbol, side, qty, reduce_only=False):
        if reduce_only:
            if self.close_error is not None:
                raise self.close_error
            return {"orderId": 2}
        return {"orderId": 1}

    def order(self, symbol, order_id):
        return {"executedQty": 15.0, "avgPrice": 100.0}

    def stop_market_close(self, symbol, side, stop_price):
        raise RuntimeError("STOP_MARKET rejected -4130")

    def cancel_all_algo(self, symbol):
        self.cancel_algo_calls += 1


def _open_fixture(file_session):
    db = file_session()
    cfg = SystemConfig(id=1)
    db.add(cfg)
    a = AIAnalysis(symbol="XUSDT", trade_decision="suggest", direction="long",
                   entry_price=100, stop_loss=98, take_profit_1=104,
                   recommendation=70, created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return db, cfg, a


def test_open_protection_failure_persists_incident_and_blocks_new_entries(
    file_session, opening_settings, monkeypatch,
):
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _OpeningTrader(close_error=RuntimeError("-2022 ReduceOnly Order is rejected"))

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    rows = db.query(ProtectionIncident).all()
    assert len(rows) == 1
    assert rows[0].reason == "open_protection_failed"
    assert rows[0].status == "pending"  # 明确拒单才允许自动重试
    assert trader.cancel_algo_calls == 0  # 平仓前不撤既有保护单

    db.expire_all()
    assert try_open_for_analysis(db, trader, a.id) == "blocked"
    db.refresh(a)
    assert "保护事故" in (a.open_block_reason or "")
    db.close()


def test_open_protection_failure_close_ack_waits_for_reconcile(
    file_session, opening_settings, monkeypatch,
):
    """平仓请求已 ACK 仍不代表归零：事故置 awaiting_reconcile，禁止重发。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _OpeningTrader(close_error=None)

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.status == "awaiting_reconcile"
    db.close()


# ---------- 故障注入：入场单已提交但响应丢失（审计 7.3） ----------


class _EntryAckLostTrader(_OpeningTrader):
    """市价入场单提交抛超时（订单可能已送达），reduceOnly 平仓走基类逻辑。"""

    def market_order(self, symbol, side, qty, reduce_only=False):
        if not reduce_only:
            raise TimeoutError("entry order ack lost")
        return super().market_order(symbol, side, qty, reduce_only=True)


def test_entry_submit_ack_lost_records_reconcile_incident(
    file_session, opening_settings, monkeypatch,
):
    """响应丢失：立即按最坏情况平仓并落事故，ACCEPT 后等对账、绝不重发。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _EntryAckLostTrader()

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.reason == "open_entry_submit_unknown"
    assert row.status == "awaiting_reconcile"  # ACK 不等于空仓，巡检对账后才释放
    assert row.entry_order_id is None  # 响应丢失时没有 orderId
    assert "ack lost" in (row.last_error or "")
    db.close()


def test_entry_submit_ack_lost_close_rejected_is_retryable(
    file_session, opening_settings, monkeypatch,
):
    """平仓被明确拒绝（无仓/不可减仓）→ pending，巡检确认无仓后自动解除。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _EntryAckLostTrader(
        close_error=RuntimeError("-2022 ReduceOnly Order is rejected"))

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.status == "pending"
    db.close()


def test_entry_ack_lost_uses_live_entry_price_for_generation(
    file_session, opening_settings, monkeypatch,
):
    """响应丢失但有仓：事故记录交易所实际入场价，世代校验才可能绑定成功。"""

    class _LostWithPositionTrader(_EntryAckLostTrader):
        def positions(self):
            return {"XUSDT": {"amt": 15.0, "entry_price": 101.0}}

    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _LostWithPositionTrader()

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.reason == "open_entry_submit_unknown"
    assert row.entry_price == 101.0  # 不是参考价 100
    db.close()


def test_entry_submit_explicit_rejection_does_not_create_incident(
    file_session, opening_settings, monkeypatch,
):
    """交易所明确拒单（含错误码）说明未送达，不建事故、不误封锁账号。"""

    class _RejectedTrader(_OpeningTrader):
        def market_order(self, symbol, side, qty, reduce_only=False):
            raise RuntimeError("Margin is insufficient (-2019)")

    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _RejectedTrader()

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"
    assert db.query(ProtectionIncident).count() == 0
    db.close()


# ---------- 故障注入：限价入场单已提交但响应丢失（审计 7.3） ----------


@pytest.fixture
def limit_entry_settings(opening_settings, monkeypatch):
    """进入挂单入场分支：现价 RR 退化到 ai_rr_min-容差以下，且挂单开关开启。"""
    from app.config import settings
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", True)


class _LimitAckLostTrader(_OpeningTrader):
    """限价入场单提交抛超时（可能已挂出/已成交），可配置撤单结果与残余仓位。"""

    def __init__(self, cancel_error=None, position=None, close_error=None):
        super().__init__(close_error=close_error)
        self.cancel_error = cancel_error
        self.position = position
        self.cancel_all_calls = 0

    def price(self, symbol):
        return 100.5  # rr_now = 1.4 < 1.5-0.05，进入挂单分支

    def limit_order(self, symbol, side, qty, price, time_in_force="GTC"):
        raise TimeoutError("limit order ack lost")

    def cancel_all_open_orders(self, symbol):
        self.cancel_all_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error

    def positions(self):
        return {"XUSDT": dict(self.position)} if self.position else {}


def test_limit_entry_ack_lost_cancels_stray_order(
    file_session, limit_entry_settings, monkeypatch,
):
    """未成交：撤掉可能挂出的限价单即可，不建事故、不封锁账号。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _LimitAckLostTrader()

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    assert trader.cancel_all_calls == 1
    assert db.query(ProtectionIncident).count() == 0
    db.close()


def test_limit_entry_ack_lost_filled_position_is_flattened(
    file_session, limit_entry_settings, monkeypatch,
):
    """已成交：立即 reduceOnly 平仓并落事故对账，不留无保护仓位。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _LimitAckLostTrader(position={"amt": 2, "entry_price": 100.0})

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.reason == "open_limit_entry_submit_unknown"
    assert row.status == "awaiting_reconcile"
    assert row.qty == 2
    assert row.entry_price == 100
    assert row.entry_order_id is None
    assert "close_ack" in (row.last_error or "")
    assert trader.cancel_all_calls == 1
    db.close()


def test_limit_entry_ack_lost_cancel_failure_escalates(
    file_session, limit_entry_settings, monkeypatch,
):
    """撤单结果未知：无法排除悬挂单，交人工复核并由巡检重试撤单。"""
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _LimitAckLostTrader(
        cancel_error=TimeoutError("cancel ack lost"),
        position={"amt": 2, "entry_price": 101.0},
    )

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"

    row = db.query(ProtectionIncident).one()
    assert row.reason == "open_limit_entry_submit_unknown"
    assert row.status == "manual_review"
    assert row.entry_price == 101.0  # 实际成交价，不是限价 100
    assert "cancel ack lost" in (row.last_error or "")
    assert trader.cancel_all_calls == 1
    db.close()


def test_limit_entry_explicit_rejection_does_not_cancel(
    file_session, limit_entry_settings, monkeypatch,
):
    """明确拒单（含错误码）说明限价单未送达，不撤单、不建事故。"""

    class _RejectedLimitTrader(_LimitAckLostTrader):
        def limit_order(self, symbol, side, qty, price, time_in_force="GTC"):
            raise RuntimeError("Order would immediately trigger (-2021)")

    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db, cfg, a = _open_fixture(file_session)
    trader = _RejectedLimitTrader()

    assert _attempt_open(db, trader, cfg, a, 1000.0, {}) == "blocked"
    assert trader.cancel_all_calls == 0
    assert db.query(ProtectionIncident).count() == 0
    db.close()


def test_limit_unknown_incident_waits_for_cancel_confirmation(db_factory):
    """空仓也不等于闭环：撤单确认成功后才解除封锁。"""
    db = db_factory()
    item = incident(db, reason="open_limit_entry_submit_unknown")
    calls = []

    def fail_cancel(symbol):
        raise TimeoutError("cancel ack lost")

    trader = SimpleNamespace(positions=lambda: {},
                             cancel_all_open_orders=fail_cancel)
    recover_due_incidents(db, trader, "testnet-1", True)
    db.refresh(item)
    assert item.status == "manual_review"
    assert has_active_incident(db, "testnet-1", True)

    trader.cancel_all_open_orders = lambda symbol: calls.append(symbol)
    recover_due_incidents(db, trader, "testnet-1", True,
                          now=datetime.utcnow() + timedelta(hours=2))
    db.refresh(item)
    assert calls == ["XUSDT"]
    assert item.status == "resolved"
    assert not has_active_incident(db, "testnet-1", True)
    db.close()


class _MaterializeTrader:
    """挂单落地替身：成交确认后 SL 挂失败，紧急平仓超时。"""

    account_key = "testnet-1"
    testnet = True

    def cancel_regular_order(self, symbol, order_id):
        return None

    def order(self, symbol, order_id):
        # 实际成交价与挂单限价（100）不同：事故必须记录实际均价而非委托价
        return {"executedQty": 2.0, "avgPrice": 101.0}

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
        raise RuntimeError("STOP_MARKET rejected -4130")

    def market_order(self, symbol, side, qty, reduce_only=False):
        raise TimeoutError("read timed out")


def test_pending_materialize_protection_failure_is_durably_recorded(
    file_session, monkeypatch,
):
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db = file_session()
    cfg = SystemConfig(id=1)
    db.add(cfg)
    pe = PendingEntryOrder(
        symbol="XUSDT", direction="long", account_key="testnet-1", testnet=True,
        entry_price=100, stop_loss=98, tp1=104, tp2=0, qty=2, leverage=10,
        margin_used=20, risk_amount=4, limit_order_id="11", status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(pe)
    db.commit()

    assert materialize_pending_entry(db, _MaterializeTrader(), pe, cfg) is None
    db.expire_all()
    assert db.get(PendingEntryOrder, pe.id).status == "failed"
    row = db.query(ProtectionIncident).one()
    assert row.reason == "pending_materialize_protection_failed"
    assert row.status == "manual_review"  # 超时可能已送达，绝不自动重发
    assert row.entry_price == 101.0  # 实际成交均价，不是挂单限价
    assert has_active_incident(db, "testnet-1", True)
    db.close()


class _MaterializeAckTrader(_MaterializeTrader):
    """挂单落地替身：SL 挂失败，紧急平仓请求返回 ACK（不代表已空仓）。"""

    def market_order(self, symbol, side, qty, reduce_only=False):
        assert reduce_only is True
        return {"orderId": 2}


def test_pending_materialize_close_ack_waits_for_reconcile(
    file_session, monkeypatch,
):
    import app.services.protection_recovery as recovery
    monkeypatch.setattr(recovery, "SessionLocal", file_session)
    db = file_session()
    cfg = SystemConfig(id=1)
    db.add(cfg)
    pe = PendingEntryOrder(
        symbol="XUSDT", direction="long", account_key="testnet-1", testnet=True,
        entry_price=100, stop_loss=98, tp1=104, tp2=0, qty=2, leverage=10,
        margin_used=20, risk_amount=4, limit_order_id="11", status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(pe)
    db.commit()

    assert materialize_pending_entry(db, _MaterializeAckTrader(), pe, cfg) is None
    db.expire_all()
    assert db.get(PendingEntryOrder, pe.id).status == "failed"
    row = db.query(ProtectionIncident).one()
    assert row.reason == "pending_materialize_protection_failed"
    assert row.status == "awaiting_reconcile"  # ACK 不等于空仓，交巡检对账
    assert has_active_incident(db, "testnet-1", True)
    db.close()
