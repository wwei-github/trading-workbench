# -*- coding: utf-8 -*-
"""中途止盈腿 _trail_profit_take（2026-10-12 拍板 B5=A/B6=A，docs/21 O8）——单测。

锁八件事：
1. 多单 1h 已收盘 K 收盘价全程浮盈（已实现+浮动）≥ 2×ATR(22,1d)×当前持仓
   → 市价减 1/3 落袋：qty 同步、realized 落账、TP_TRAIL 事件（atr_mult 口径）；
2. 空单镜像（收盘跌到 98 → BUY reduceOnly 减 1/3）；
3. 收盘口径抗插针：末根已收盘收 101.5（浮盈 1.5 < 2.0），最高价插到 103
   也不触发——影线不计入浮盈；
4. 一次性旗标 raw.trail_tp_done：二刀不再下单；
5. min_qty 守卫：减仓量低于交易所最小规则 → 跳过不落旗标（下轮重试，留给 SL）；
6. 竞态防护：交易所持仓已明显小于记录（<0.7 倍）→ 只补旗标+qty、记
   reason="already_reduced"、不下第二刀；
7. entry_atr 缺失退化 2×risk_amount（2R）：收盘 103 不动、104.5 触发，
   事件带 fallback="2R"（无 atr_mult）；
8. 巡检接线（_trail_mode_patrol）：武装后先浮盈腿再减半腿（同轮两腿独立叠加、
   各自旗标齐落）；未武装不进任何落袋腿（武装前无保本兜底）。

sqlite 内存库夹具镜像 test_tp_mode.py；trader/ExchangePool 全桩化。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.trade_engine as te  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402

FEE = 0.0005


# ── 夹具 ─────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    from app.models.system_config import SystemConfig

    Base.metadata.create_all(bind=engine, tables=[
        TradeRecord.__table__, TradeEvent.__table__, SystemConfig.__table__])
    # ExchangePool 桩：默认空 K 线（数据不可得 → 本腿跳过不落旗标）；需要数据的
    # 用例用 _patch_k1/_patch_kl 覆盖
    monkeypatch.setattr(te, "ExchangePool",
                        lambda: SimpleNamespace(get_klines=lambda *a, **k: []))
    return Session(bind=engine)


def _rec(db, **kw):
    """在跑多单：entry 100 / qty 1 / risk 2（1R=2U）；trail 单 tp1=None"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="OPENED",
        testnet=True, account_key="testnet-1",
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=None, tp2=0.0,
        realized_pnl=0.0, unrealized_pnl=0.0,
        raw={"tp_mode": "trail"},
        opened_at=datetime.utcnow(),
    )
    base.update(kw)
    rec = TradeRecord(**base)
    db.add(rec)
    db.commit()
    return rec


class StubTrader:
    """最小 trader 桩：positions / filters / round_qty / market_order /
    realized_pnl_since（+ 跟进止损用的 round_price / price / stop_market_close /
    cancel_order）"""

    def __init__(self, price=101.0, amt=1.0, min_qty=0.001):
        self.price_val = price
        self.sl_orders: list[float] = []
        self.amt = amt
        self.min_qty = min_qty
        self.account_key = "testnet-1"
        self.testnet = True
        self.market_closes: list[tuple[str, float, bool]] = []  # (side, qty, reduce_only)

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
        return ({"BTCUSDT": {"amt": self.amt, "entry_price": 100.0,
                             "initial_margin": 50.0}}
                if self.amt > 0 else {})

    def filters(self, symbol):
        return {"min_qty": self.min_qty, "min_notional": 5.0}

    def round_qty(self, symbol, q):
        return round(q, 3)

    def market_order(self, symbol, side, qty, reduce_only=False):
        self.market_closes.append((side, qty, reduce_only))
        if reduce_only:
            self.amt = max(0.0, self.amt - qty)  # 桩内模拟减仓
        return {"orderId": 900 + len(self.market_closes)}

    def realized_pnl_since(self, symbol, since_ms):
        return 1.23


def _klines_1h(closes):
    """closes → 1h K 线 6 列（ts 递增、o=前收盘、h=c+0.5、l=c-0.5；尾根为
    未收盘桩数据，函数内部剔尾后 closed[-1] 为倒数第二根——构造时直接把
    目标收盘放在倒数第二根即可）"""
    base = 1_700_000_000_000
    out = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        c = float(c)
        out.append([base + i * 3_600_000, prev, c + 0.5, c - 0.5, c, 1000.0])
        prev = c
    return out


def _patch_kl(monkeypatch, kl):
    monkeypatch.setattr(te, "ExchangePool",
                        lambda: SimpleNamespace(get_klines=lambda *a, **k: kl))


def _patch_k1(monkeypatch, closes):
    _patch_kl(monkeypatch, _klines_1h(closes))


def _tp_events(db):
    return db.query(TradeEvent).filter_by(event_type="TP_TRAIL").all()


# ── 1. 多单触发：收盘浮盈恰达落袋线 → 减 1/3 落袋 ────────────────


def test_trail_tp_long_fire(db, monkeypatch):
    """末根已收盘 102 → 浮盈 (102−100)×1=2.0 恰达落袋线 2×ATR×amt=2.0
    → 市价 SELL reduceOnly 减 1/3，qty 同步、realized 落账、TP_TRAIL 事件"""
    _patch_k1(monkeypatch, [100.0] * 10 + [102.0, 102.5])
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=102.5, amt=1.0)
    te._trail_profit_take(db, trader, rec)
    cut = round(1 / 3, 3)
    assert len(trader.market_closes) == 1
    side, q, ro = trader.market_closes[0]
    assert side == "SELL" and ro is True  # 多单减仓 = 卖出 reduceOnly
    assert q == pytest.approx(cut)
    assert float(rec.qty) == pytest.approx(1.0 - cut)
    assert rec.raw["trail_tp_done"] is True
    # 落袋腿已实现盈亏落账（income 实录口径）
    assert float(rec.realized_pnl) == pytest.approx(1.23)
    evs = _tp_events(db)
    assert len(evs) == 1
    d = evs[0].detail
    assert d["close"] == pytest.approx(102.0)
    assert d["target"] == pytest.approx(2.0)
    assert d["atr_mult"] == 2.0
    assert d["cut_qty"] == pytest.approx(cut)
    assert d["remain_qty"] == pytest.approx(1.0 - cut)
    assert "fallback" not in d


# ── 2. 空单镜像 ─────────────────────────────────────────────────


def test_trail_tp_short_mirror(db, monkeypatch):
    """空单：末根已收盘 98 → (100−98)×1=2.0 达标 → BUY reduceOnly 减 1/3"""
    _patch_k1(monkeypatch, [100.0] * 10 + [98.0, 97.5])
    rec = _rec(db, direction="short", stop_loss=102.0,
               raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=97.5, amt=1.0)
    te._trail_profit_take(db, trader, rec)
    cut = round(1 / 3, 3)
    assert len(trader.market_closes) == 1
    side, q, ro = trader.market_closes[0]
    assert side == "BUY" and ro is True
    assert q == pytest.approx(cut)
    assert float(rec.qty) == pytest.approx(1.0 - cut)
    assert rec.raw["trail_tp_done"] is True


# ── 3. 收盘口径抗插针：影线不计 ──────────────────────────────────


def test_trail_tp_wick_not_counted(db, monkeypatch):
    """末根已收盘收 101.5（浮盈 1.5 < 2.0），最高价插到 103 也不触发"""
    kl = _klines_1h([100.0] * 10 + [101.5, 101.6])
    kl[-2][2] = 103.0  # 已收盘末根的长上影（插针）
    _patch_kl(monkeypatch, kl)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=101.5, amt=1.0)
    te._trail_profit_take(db, trader, rec)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("trail_tp_done")
    assert _tp_events(db) == []


# ── 4. 一次性旗标 ───────────────────────────────────────────────


def test_trail_tp_once(db, monkeypatch):
    """首刀成交后即使浮盈继续走阔（收盘 103）也不再下第二刀"""
    _patch_k1(monkeypatch, [100.0] * 10 + [102.0, 102.5])
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=102.5, amt=1.0)
    te._trail_profit_take(db, trader, rec)
    assert len(trader.market_closes) == 1
    _patch_k1(monkeypatch, [100.0] * 10 + [103.0, 103.5])
    te._trail_profit_take(db, trader, rec)
    assert len(trader.market_closes) == 1
    assert len(_tp_events(db)) == 1


# ── 5. min_qty 守卫 ─────────────────────────────────────────────


def test_trail_tp_min_qty_skip(db, monkeypatch):
    """减仓量 0.333 < min_qty 0.4 → 跳过：不下单、不落旗标、无事件（下轮重试）"""
    _patch_k1(monkeypatch, [100.0] * 10 + [102.0, 102.5])
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=102.5, amt=1.0, min_qty=0.4)
    te._trail_profit_take(db, trader, rec)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("trail_tp_done")
    assert _tp_events(db) == []


# ── 6. 竞态防护：交易所已明显小于记录 → 只补旗标 ─────────────────


def test_trail_tp_already_reduced_race(db):
    """交易所持仓 0.5 已明显小于记录 1.0（<0.7 倍，前刀成交但旗标未落库）
    → 补旗标+qty、记 reason="already_reduced"、不下第二刀"""
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0})
    trader = StubTrader(price=102.0, amt=0.5)
    te._trail_profit_take(db, trader, rec)
    assert trader.market_closes == []
    assert rec.raw["trail_tp_done"] is True
    assert float(rec.qty) == pytest.approx(0.5)
    evs = _tp_events(db)
    assert len(evs) == 1
    assert evs[0].detail["reason"] == "already_reduced"
    assert evs[0].detail["cut_qty"] == pytest.approx(0.5)
    assert evs[0].detail["remain_qty"] == pytest.approx(0.5)


# ── 7. entry_atr 缺失退化 2R ────────────────────────────────────


def test_trail_tp_fallback_2r(db, monkeypatch):
    """无 entry_atr → 落袋线退化 2×risk_amount=4.0（2R）：收盘 103（浮盈 3.0）
    不动，收盘 104.5（4.5）触发，事件带 fallback="2R"（无 atr_mult）"""
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True})  # 无 entry_atr
    trader = StubTrader(price=103.0, amt=1.0)
    _patch_k1(monkeypatch, [100.0] * 10 + [103.0, 103.5])
    te._trail_profit_take(db, trader, rec)
    assert trader.market_closes == []
    _patch_k1(monkeypatch, [100.0] * 10 + [104.5, 105.0])
    te._trail_profit_take(db, trader, rec)
    assert len(trader.market_closes) == 1
    d = _tp_events(db)[0].detail
    assert d["fallback"] == "2R"
    assert d["target"] == pytest.approx(4.0)
    assert "atr_mult" not in d


# ── 8. 巡检接线：两腿独立叠加 / 未武装不进腿 ────────────────────


def test_patrol_both_legs_stack(db, monkeypatch):
    """已武装 + 末段收盘 102.25：既满足落袋线（浮盈 2.25 ≥ 2.0）又满足双确认
    破 EMA21（EMA21≈103.9）→ 先减 1/3、再减半，两腿独立叠加、旗标齐落；
    剩仓 ≈1/3（紧跟随后 _move_sl_trailing，不影响两刀断言）"""
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 13)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "entry_atr": 1.0},
               stop_loss=100.1)
    trader = StubTrader(price=102.25, amt=1.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    cut1 = round(1 / 3, 3)
    cut2 = round((1.0 - cut1) / 2, 3)
    assert [mc[0] for mc in trader.market_closes] == ["SELL", "SELL"]
    assert [mc[1] for mc in trader.market_closes] == pytest.approx([cut1, cut2])
    assert all(mc[2] for mc in trader.market_closes)
    assert float(rec.qty) == pytest.approx(1.0 - cut1 - cut2)
    assert rec.raw["trail_tp_done"] is True
    assert rec.raw["ema21_cut"] is True
    assert len(_tp_events(db)) == 1
    assert db.query(TradeEvent).filter_by(event_type="CUT_EMA21").count() == 1


def test_patrol_not_armed_no_legs(db, monkeypatch):
    """未武装（峰值 0.5 < 武装线 1.0）→ 不进任何落袋腿：即便收盘浮盈 10
    达标也不减仓（武装前无保本兜底，先落袋会暴露裸风险）"""
    _patch_k1(monkeypatch, [100.0] * 10 + [110.0, 110.5])
    rec = _rec(db, raw={"tp_mode": "trail", "entry_atr": 1.0}, peak_total_pnl=0.5)
    trader = StubTrader(price=110.0, amt=1.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("trail_tp_done")
    assert _tp_events(db) == []