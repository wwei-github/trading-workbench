# -*- coding: utf-8 -*-
"""ladder 出场链（原 test_tp_mode.py 于 2026-10-12 策略二删除后裁剪重命名）——单测。

锁三件事：
1. _derive_exit_reason：ladder 单 OPENED（无 TP1 成交）→ sl；
2. adopt_position：有止盈位 → ladder 接管（挂 TP1、raw 记挂单 id、不再写 tp_mode 快照）；
   无止盈位（tp1=0）→ 拒绝接管（trail 纯跟踪接管随策略二退役）；
3. auto_adopt_positions：仅 SL 无 TP1 的手动单跳过不进册。

sqlite 内存库夹具镜像 test_auto_tighten.py；trader 桩化。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.trade_engine as te  # noqa: E402
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
    return Session(bind=engine)


def _rec(db, **kw):
    """在跑多单：entry 100 / SL 98 / qty 1 / risk 2 → 1R = 2U"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="OPENED",
        testnet=True, account_key="testnet-1",
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=101.5, tp2=0.0,
        realized_pnl=0.0, unrealized_pnl=0.0,
        raw={},
        opened_at=datetime.utcnow(),
    )
    base.update(kw)
    rec = TradeRecord(**base)
    db.add(rec)
    db.commit()
    return rec


class StubTrader:
    """最小 trader 桩：price / stop_market_close / cancel_order / positions /
    filters / round_qty / take_profit_reduce（接管链所需）"""

    def __init__(self, price=101.0, amt=1.0, min_qty=0.001):
        self.price_val = price
        self.sl_orders: list[float] = []
        self.tp_orders: list[float] = []
        self.tp_qtys: list[float] = []
        self.amt = amt
        self.min_qty = min_qty
        self.account_key = "testnet-1"
        self.testnet = True

    def round_price(self, symbol, p):
        return round(p, 2)

    def price(self, symbol):
        return self.price_val

    def stop_market_close(self, symbol, side, sl):
        self.sl_orders.append(sl)
        return {"algoId": 500 + len(self.sl_orders)}

    def cancel_order(self, symbol, oid):
        pass

    def positions(self):
        # adopt_position 另需 entry_price / initial_margin（杠杆与成本口径）
        return ({"BTCUSDT": {"amt": self.amt, "entry_price": 100.0,
                             "initial_margin": 50.0}}
                if self.amt > 0 else {})

    def filters(self, symbol):
        return {"min_qty": self.min_qty, "min_notional": 5.0}

    def round_qty(self, symbol, q):
        return round(q, 3)

    # adopt_position 需要的其余方法（条件单读撤/止盈挂单）
    def open_algo_orders(self, symbol):
        return []

    def cancel_all_algo(self, symbol):
        pass

    def take_profit_reduce(self, symbol, side, qty, price):
        self.tp_orders.append(price)
        self.tp_qtys.append(qty)
        return {"algoId": 600 + len(self.tp_orders)}


# ── 1. _derive_exit_reason：ladder 出场归因 ──────────────────────


def test_exit_ladder_opened_is_sl(db):
    # OPENED（无 TP1 成交）→ sl
    rec = _rec(db, tp1=101.5)
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "sl"


# ── 2. 导入持仓：有止盈位 → ladder 接管；无止盈位拒收 ────────────


def test_adopt_with_tp1_ladder_unchanged(db):
    """有止盈位 → ladder 接管：挂 TP1（qty_tp1 达 min_qty）、raw 记挂单 id；
    不再写 tp_mode 快照（2026-10-12 字段退役）"""
    trader = StubTrader(price=100.0, amt=2.0, min_qty=0.001)
    rec = te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0, tp1=104.0)
    assert rec.tp1 == 104.0
    assert rec.raw["tp1_order_id"] is not None
    assert "tp_mode" not in rec.raw
    assert len(trader.tp_orders) == 1       # TP1 挂上（qty_tp1 达 min_qty）
    assert trader.tp_orders[0] == pytest.approx(104.0)


def test_adopt_without_tp1_rejected(db):
    """无止盈位（tp1=0）→ 拒绝接管（trail 纯跟踪接管随策略二退役，2026-10-12）——
    校验在撤挂/落库之前，原条件单与记录均不动"""
    trader = StubTrader(price=100.0, amt=2.0)
    with pytest.raises(ValueError, match="止盈价必填"):
        te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0, tp1=0)
    assert trader.sl_orders == [] and trader.tp_orders == []
    assert db.query(TradeRecord).first() is None


def test_adopt_rejects_wrong_sl_side(db):
    """接管三价校验：多头 SL ≥ 现价仍拒收（止损方向校验与 trail 无关）"""
    trader = StubTrader(price=100.0, amt=2.0)
    with pytest.raises(ValueError, match="必须低于现价"):
        te.adopt_position(db, trader, "BTCUSDT", stop_loss=100.0, tp1=104.0)


def test_auto_adopt_no_tp1_skipped(db, monkeypatch):
    """自动导入：仅 SL 无 TP1 的手动单跳过、不进册（止盈位必填，2026-10-12）"""
    monkeypatch.setattr(te, "list_adoptable_positions", lambda db_, tr: [
        {"symbol": "BTCUSDT", "tracked": False,
         "prefill": {"stop_loss": 98.0, "tp1": None, "tp2": None}},
    ])
    trader = StubTrader(price=100.0, amt=1.0)
    assert te.auto_adopt_positions(db, trader) == []
    assert db.query(TradeRecord).first() is None


# ── 3. 止盈全平模式（2026-10-28）：TP 档直接平完全仓 ─────────────


def test_tp_quantities_full_exit():
    """full_exit=True：有 TP2 → TP1 p1% / TP2 剩余全部；无 TP2 → TP1 100%。
    关闭时保持分批口径（有 TP2 → p1/p2，无 TP2 → p1s）"""
    assert te._tp_quantities(100, 105, 60, 20, 80, full_exit=True) == (60, 40)
    assert te._tp_quantities(100, 0, 60, 20, 80, full_exit=True) == (100, 0)
    assert te._tp_quantities(100, 105, 60, 20, 80) == (60, 20)
    assert te._tp_quantities(100, 0, 60, 20, 80) == (80, 0)


def test_adopt_full_exit_tp2_closes_remainder(db):
    """全平模式接管有 TP2 的单：TP1 按比例减仓，TP2 数量 = 剩余全部（两档合计 = 全仓）"""
    from app.models.system_config import SystemConfig

    db.add(SystemConfig(id=1, tp_full_exit_enabled=True))
    db.commit()
    trader = StubTrader(price=100.0, amt=10.0, min_qty=0.001)
    rec = te.adopt_position(
        db, trader, "BTCUSDT", stop_loss=98.0, tp1=104.0, tp2=108.0)
    assert rec.raw["tp_full_exit"] is True
    assert trader.tp_qtys == pytest.approx([6.0, 4.0])  # 60% / 剩余 40%
    assert rec.raw["qty_tp1"] == pytest.approx(6.0)
    assert rec.raw["qty_tp2"] == pytest.approx(4.0)


def test_adopt_full_exit_without_tp2_closes_all(db):
    """全平模式接管无 TP2 的单：TP1 一次平掉 100%（忽略无 TP2 比例 80%）"""
    from app.models.system_config import SystemConfig

    db.add(SystemConfig(id=1, tp_full_exit_enabled=True))
    db.commit()
    trader = StubTrader(price=100.0, amt=10.0, min_qty=0.001)
    rec = te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0, tp1=104.0)
    assert rec.raw["tp_full_exit"] is True
    assert trader.tp_qtys == pytest.approx([10.0])
    assert rec.raw["qty_tp1"] == pytest.approx(10.0)
    assert rec.raw["qty_tp2"] == 0


def test_is_tp_full_exit_attribution():
    """零仓出场归因（2026-10-28）：全平快照 + 挂单覆盖全仓 + 对应 TP 真成交才算
    tp_full；关闭开关的单、TP1 后回踩止损（TP2 未成交）、TP2 挂单失败都不算"""
    from types import SimpleNamespace

    rec_tp2 = SimpleNamespace(qty=100.0, tp2=104.0)
    rec_no_tp2 = SimpleNamespace(qty=100.0, tp2=0)
    raw_tp2 = {"tp_full_exit": True, "qty_tp1": 60.0, "qty_tp2": 40.0}
    raw_no_tp2 = {"tp_full_exit": True, "qty_tp1": 100.0, "qty_tp2": 0}

    # 有 TP2：TP2 成交才算；只 TP1 成交（随后回踩止损）不算
    assert te._is_tp_full_exit(rec_tp2, raw_tp2, True, True) is True
    assert te._is_tp_full_exit(rec_tp2, raw_tp2, True, False) is False
    # 无 TP2：TP1 即全平
    assert te._is_tp_full_exit(rec_no_tp2, raw_no_tp2, True, False) is True
    assert te._is_tp_full_exit(rec_no_tp2, raw_no_tp2, False, False) is False
    # 全平模式下 TP2 挂单失败（只剩 TP1 60%）：仓位归零来自止损，不算全平
    raw_tp2_failed = {"tp_full_exit": True, "qty_tp1": 60.0, "qty_tp2": 0}
    assert te._is_tp_full_exit(rec_no_tp2, raw_tp2_failed, True, False) is False
    # 开关关闭的单一律不按 tp_full 记
    assert te._is_tp_full_exit(
        rec_no_tp2, {"tp_full_exit": False, "qty_tp1": 100.0, "qty_tp2": 0},
        True, False) is False
    assert te._is_tp_full_exit(rec_tp2, {}, True, True) is False

