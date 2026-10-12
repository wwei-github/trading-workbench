# -*- coding: utf-8 -*-
"""存量 trail 单转 ladder 转换脚本单测（设计 .trae/documents/存量trail单转ladder设计.md §九）。

sqlite 内存库夹具镜像 test_ladder_exits.py；trader 桩化（不连交易所）。
锁五件事：R 基准与 1.5R 价计算、Case 分类边界（含 last==level）、
Case A 挂止盈+落库、Case B 置 TP1_HIT+立即保本、跳过与幂等。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import convert_trail_to_ladder as ctl  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402

FEE = 0.0005  # 保本价 = 成本 ± 往返费缓冲：entry 100 → be = 100.1


# ── 夹具 ─────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    from app.models.system_config import SystemConfig

    Base.metadata.create_all(bind=engine, tables=[
        TradeRecord.__table__, TradeEvent.__table__, SystemConfig.__table__])
    session = Session(bind=engine)
    yield session
    session.close()


def _trail_rec(db, **kw):
    """存量 trail 在跑多单：entry 100 / 原始 SL 98（OPEN 事件快照）/ 1R=2 → 1.5R 档=103；
    rec.stop_loss 现值故意设 99（与原始 98 区分），验证 R 基准取 OPEN 快照而非现值"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="OPENED",
        testnet=True, account_key="testnet-1",
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=99.0, tp1=None, tp2=0.0,
        raw={"tp_mode": "trail", "entry_atr": 1.5, "realized_bookmark": 0,
             "sl_order_id": 111},
        opened_at=datetime.utcnow(),
    )
    base.update(kw)
    rec = TradeRecord(**base)
    db.add(rec)
    db.add(TradeEvent(trade_record_id=rec.id, event_type="OPEN", detail={
        "fill_price": 100.0, "qty": 1.0, "stop_loss": 98.0,
        "risk_amount": 2.0, "tp_mode": "trail"}))
    db.commit()
    return rec


class StubTrader:
    """最小 trader 桩：转换链所需（行情/持仓/精度/挂单/书签/在挂单查询）"""

    def __init__(self, price=101.0, amt=1.0, min_qty=0.001, bookmark=0.0):
        self.price_val = price
        self.amt = amt
        self.min_qty = min_qty
        self.bookmark = bookmark
        self.tp_orders: list[tuple[float, float]] = []   # (qty, price)
        self.sl_orders: list[float] = []
        self.cancelled: list[int] = []

    def price(self, symbol):
        return self.price_val

    def positions(self):
        return {"BTCUSDT": {"amt": self.amt}} if self.amt > 0 else {}

    def filters(self, symbol):
        return {"min_qty": self.min_qty, "min_notional": 5.0}

    def round_qty(self, symbol, q):
        return round(q, 3)

    def round_price(self, symbol, p):
        return round(p, 2)

    def take_profit_reduce(self, symbol, side, qty, price):
        self.tp_orders.append((qty, price))
        return {"algoId": 700 + len(self.tp_orders)}

    def stop_market_close(self, symbol, side, sl):
        self.sl_orders.append(sl)
        return {"algoId": 800 + len(self.sl_orders)}

    def cancel_order(self, symbol, oid):
        self.cancelled.append(oid)

    def open_order_ids(self, symbol):
        return {111}  # 旧 SL 单在挂（_replace_sl 撤销路径可断言）

    def realized_price_pnl_since(self, symbol, since_ms):
        return self.bookmark


# ── 1. 纯函数：R 基准 / 1.5R 价 / 分类边界 ──────────────────────


def test_r_basis_from_open_event():
    rec = TradeRecord(symbol="X", direction="long", entry_price=100.0,
                      risk_amount=2.0, qty=1.0, stop_loss=99.0, raw={})
    entry, dist, source = ctl._r_basis(rec, {"stop_loss": 98.0, "fill_price": 100.0})
    assert (entry, dist, source) == (100.0, 2.0, "open_event")  # 现值 SL=99 不参与


def test_r_basis_fallback_risk_over_qty():
    rec = TradeRecord(symbol="X", direction="long", entry_price=100.0,
                      risk_amount=2.0, qty=1.0, stop_loss=99.0, raw={})
    entry, dist, source = ctl._r_basis(rec, {})
    assert source == "risk_amount/qty" and dist == pytest.approx(2.0)


def test_r_basis_unavailable_returns_none():
    rec = TradeRecord(symbol="X", direction="long", entry_price=100.0,
                      risk_amount=None, qty=1.0, stop_loss=99.0, raw={})
    assert ctl._r_basis(rec, {}) is None


def test_level_and_classify_boundary():
    assert ctl._level_1p5r(100.0, 2.0, "long") == 103.0
    assert ctl._level_1p5r(100.0, 2.0, "short") == 97.0
    # 边界即 B：last == level → 已越过
    assert ctl._tp_triggered(103.0, 103.0, "long") is True
    assert ctl._tp_triggered(102.99, 103.0, "long") is False
    assert ctl._tp_triggered(97.0, 97.0, "short") is True
    assert ctl._tp_triggered(97.01, 97.0, "short") is False


# ── 2. Case A：挂 1.5R 止盈 + 落库 ──────────────────────────────


def test_case_a_places_tp_and_updates_raw(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=101.0, bookmark=0.5)  # < 103 → Case A
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "converted_A"
    assert trader.tp_orders == [(0.8, 103.0)]       # 80% × 1.0，档价 = 1.5R
    assert float(rec.tp1) == 103.0
    assert rec.raw["tp1_order_id"] == 701
    assert rec.raw["qty_tp1"] == 0.8
    assert rec.raw["tp_mode"] == "ladder"
    assert rec.raw["trail_convert"]["mode"] == "A"
    assert rec.raw["realized_bookmark"] == 0.5      # 书签推进到当前价差累计
    ev = db.query(TradeEvent).filter_by(event_type="CONVERT").one()
    assert ev.detail["mode"] == "A" and ev.detail["level"] == 103.0


def test_case_a_dry_run_no_side_effects(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=101.0)
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=False)
    assert row["result"] == "dry-A" and row["qty_tp1"] == 0.8
    assert trader.tp_orders == [] and rec.tp1 is None
    assert rec.raw.get("trail_convert") is None
    assert db.query(TradeEvent).filter_by(event_type="CONVERT").count() == 0


def test_case_a_min_qty_skip(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=101.0, amt=0.01, min_qty=0.05)  # 80%×0.01=0.008 < 0.05
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "skip" and "min_qty" in row["reason"]
    assert trader.tp_orders == []
    assert db.query(TradeEvent).filter_by(event_type="CONVERT").count() == 0


def test_qty_sync_when_mismatch(db):
    rec = _trail_rec(db, qty=1.0)
    trader = StubTrader(price=101.0, amt=0.6)  # 偏差 >1% → 以交易所持仓为准
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "converted_A"
    assert float(rec.qty) == pytest.approx(0.6)
    assert rec.raw["trail_convert"]["prev_qty"] == 1.0
    assert trader.tp_orders == [(0.48, 103.0)]      # 80% × 0.6


# ── 3. Case B：置 TP1_HIT + 立即保本 ────────────────────────────


def test_case_b_sets_status_and_moves_breakeven(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=105.0)                # ≥ 103 → Case B
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "converted_B" and row["breakeven"] is True
    assert rec.status == "TP1_HIT"
    assert rec.raw["qty_tp1"] == 0                  # remaining_qty 口径保全仓
    assert rec.raw["trail_convert"]["mode"] == "B"
    assert trader.tp_orders == []                   # 不挂止盈
    assert 111 in trader.cancelled                  # 旧 SL 单撤销
    assert trader.sl_orders[-1] == 100.1            # 新 SL = 保本价 100×1.001
    assert float(rec.stop_loss) == 100.1
    evs = db.query(TradeEvent).filter_by(event_type="CONVERT").all()
    assert len(evs) == 1 and evs[0].detail["mode"] == "B"


def test_case_b_dry_run(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=105.0)
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=False)
    assert row["result"] == "dry-B"
    assert rec.status == "OPENED" and trader.sl_orders == []
    assert rec.raw.get("trail_convert") is None


# ── 4. 跳过与幂等 ────────────────────────────────────────────────


def test_idempotent_skip_after_convert(db):
    rec = _trail_rec(db, raw={"tp_mode": "ladder",
                              "trail_convert": {"mode": "A", "at": "x"}})
    trader = StubTrader(price=101.0)
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "skip" and "已转换" in row["reason"]
    assert trader.tp_orders == [] and trader.sl_orders == []


def test_no_position_skip(db):
    rec = _trail_rec(db)
    trader = StubTrader(price=101.0, amt=0.0)
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "skip" and "为空" in row["reason"]
    assert trader.tp_orders == []


def test_r_basis_missing_skip(db):
    rec = _trail_rec(db, entry_price=None, risk_amount=None, raw={"tp_mode": "trail"})
    # 抹掉 OPEN 事件（模拟旧行缺快照）
    db.query(TradeEvent).filter_by(event_type="OPEN").delete()
    db.commit()
    trader = StubTrader(price=101.0)
    row = ctl.convert_one(db, trader, rec, cfg=None, fee_rate=FEE, apply=True)
    assert row["result"] == "skip" and "R 基准" in row["reason"]
    assert trader.tp_orders == []