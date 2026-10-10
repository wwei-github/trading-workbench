# -*- coding: utf-8 -*-
"""止盈模式 tp_mode（2026-10-10，docs/19 §3.4 两派哲学落地）——单测。

锁七件事：
1. normalize_raw：trail/ladder 保真，非法值（含空串/未声明）归 None；
2. validate_decision：suggest 落库 trail 保真 / 缺省回退 ladder / skip 恒 None；
3. ai_agent._handle_submit：trail 硬清零 TP2（即使 AI 报了 tp2 锚点）、
   非法 tp_mode 归 None 后落库回退 ladder；
4. _derive_exit_reason trail 三态：未武装=sl / SL 贴保本=breakeven_sl /
   越过保本=trail_sl（多空对称）；
5. _trail_mode_patrol：未达标不武装 / 达标武装保本+落旗标+SL_MOVE /
   entry_atr 缺失退化 1R 武装线 / 挂上即触发不落旗标下轮重试 / 已武装只跟进；
6. _ema21_half_cut：破线减半（多空镜像）/ 一次性旗标 / min_qty 跳过 /
   双刀竞态防护（交易所已半仓不再下第二刀）；
7. adopt_position 无止盈位 → trail 接管（不挂 TP、raw 快照、仅校验 SL），
   有 TP1 → ladder 原行为；auto_adopt 对 SL-only 单不再跳过。

sqlite 内存库夹具镜像 test_auto_tighten.py；trader/ExchangePool 全桩化。
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
from app.services.ai_agent import _handle_submit  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    TradeDecision,
    normalize_raw,
    validate_decision,
)

FEE = 0.0005  # 保本价 = 成本 ± 往返费缓冲：entry 100 → be = 100.1


# ── 夹具 ─────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    from app.models.system_config import SystemConfig

    Base.metadata.create_all(bind=engine, tables=[
        TradeRecord.__table__, TradeEvent.__table__, SystemConfig.__table__])
    # ExchangePool 桩：跟进止损的 K 线腿拿不到数据 → 摆动/ATR 腿全跳过
    # （聚焦武装逻辑；已武装路径走到「无候选不动 SL」即视为跟进调用成功）
    # adopt_position 的 entry_atr 快照同样拿空 K 线 → 异常被捕 → None（退化 1R）
    monkeypatch.setattr(te, "ExchangePool",
                        lambda: SimpleNamespace(get_klines=lambda *a, **k: []))
    return Session(bind=engine)


def _rec(db, **kw):
    """在跑多单：entry 100 / SL 98 / qty 1 / risk 2 → 1R = 2U；trail 单 tp1=None"""
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
    """最小 trader 桩：round_price / price / stop_market_close / cancel_order；
    EMA21 减半腿另需 positions / filters / round_qty / market_order"""

    def __init__(self, price=101.0, amt=1.0, min_qty=0.001):
        self.price_val = price
        self.sl_orders: list[float] = []
        self.tp_orders: list[float] = []
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
        # adopt_position 另需 entry_price / initial_margin（杠杆与成本口径）
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

    # adopt_position 需要的其余方法（条件单读撤/止盈挂单）
    def open_algo_orders(self, symbol):
        return []

    def cancel_all_algo(self, symbol):
        pass

    def take_profit_reduce(self, symbol, side, qty, price):
        self.tp_orders.append(price)
        return {"algoId": 600 + len(self.tp_orders)}


# ── 1. normalize_raw：tp_mode 归一化 ─────────────────────────────


def test_normalize_tp_mode_kept():
    assert normalize_raw({"trade_decision": "suggest", "tp_mode": "trail"}).tp_mode == "trail"
    assert normalize_raw({"trade_decision": "suggest", "tp_mode": "ladder"}).tp_mode == "ladder"


def test_normalize_tp_mode_illegal_to_none():
    for bad in ("xyz", "", None):
        d = normalize_raw({"trade_decision": "suggest", "tp_mode": bad})
        assert d.tp_mode is None, bad
    assert normalize_raw({"trade_decision": "suggest"}).tp_mode is None


# ── 2. validate_decision：落库 tp_mode 三态 ──────────────────────


def _suggest_d(tp_mode=None):
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        tp_mode=tp_mode, entry_price=100.0, stop_loss=98.0,
        take_profit_1=104.0, take_profit_2=0.0, recommendation=80,
    )


def test_validate_keeps_trail():
    ok, _, fixed = validate_decision(_suggest_d("trail"), {}, [])
    assert ok and fixed["tp_mode"] == "trail"


def test_validate_missing_falls_back_ladder():
    ok, _, fixed = validate_decision(_suggest_d(None), {}, [])
    assert ok and fixed["tp_mode"] == "ladder"


def test_validate_skip_tp_mode_none():
    d = TradeDecision(trade_decision="skip", skip_reason="1. 无信号", recommendation=20)
    ok, _, fixed = validate_decision(d, {}, [])
    assert ok and fixed["tp_mode"] is None


# ── 3. _handle_submit：trail 清零 TP2 ────────────────────────────


def _submit_args(tp_mode):
    return {
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow", "tp_mode": tp_mode,
        "entry_ref": "market", "entry_offset_pct": 0,
        "stop_ref": "market", "stop_offset_pct": -2,
        "tp1_ref": "market", "tp1_offset_pct": 4,
        "tp2_ref": "market", "tp2_offset_pct": 8,  # trail 时应被无视
        "recommendation": 80,
    }


def test_submit_trail_zeroes_tp2():
    done, _, fixed = _handle_submit(_submit_args("trail"), {"current_price": 100.0}, [])
    assert done
    assert fixed["tp_mode"] == "trail"
    assert fixed["take_profit_2"] == 0  # trail 硬清零，tp2_ref 被无视


def test_submit_ladder_keeps_tp2():
    done, _, fixed = _handle_submit(_submit_args("ladder"), {"current_price": 100.0}, [])
    assert done
    assert fixed["tp_mode"] == "ladder"
    assert fixed["take_profit_2"] == pytest.approx(108.0)  # market +8%


def test_submit_illegal_mode_falls_back_ladder():
    done, _, fixed = _handle_submit(_submit_args("xyz"), {"current_price": 100.0}, [])
    assert done
    assert fixed["tp_mode"] == "ladder"


# ── 4. _derive_exit_reason：trail 出场归因 ───────────────────────


def test_exit_trail_not_armed(db):
    rec = _rec(db, raw={"tp_mode": "trail"})
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "sl"


def test_exit_trail_breakeven_long(db):
    # armed 后 SL 仍在保本价（100.1）→ 保本出场
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=100.1)
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "breakeven_sl"


def test_exit_trail_trailing_long(db):
    # SL 已收紧越过保本价 → 跟进止损出场
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=101.0)
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "trail_sl"


def test_exit_trail_short_symmetry(db):
    # 空单对称：be = 100 − 0.1 = 99.9；SL=99.9 贴保本 / 99.0 越保本
    rec = _rec(db, direction="short", stop_loss=99.9,
               raw={"tp_mode": "trail", "trail_armed": True})
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "breakeven_sl"
    rec2 = _rec(db, direction="short", stop_loss=99.0,
                raw={"tp_mode": "trail", "trail_armed": True})
    assert te._derive_exit_reason(rec2, fee_rate=FEE) == "trail_sl"


def test_exit_ladder_opened_is_sl(db):
    # 非 trail 单走原逻辑：OPENED（无 TP1 成交）→ sl
    rec = _rec(db, raw={"tp_mode": "ladder"}, tp1=101.5)
    assert te._derive_exit_reason(rec, fee_rate=FEE) == "sl"


# ── 5. _trail_mode_patrol：武装与跟进 ────────────────────────────


def test_patrol_below_threshold_no_arm(db):
    # 峰值 2.9 < 武装线 3×1=3.0 → 不武装不动 SL
    rec = _rec(db, raw={"tp_mode": "trail", "entry_atr": 3.0}, peak_total_pnl=2.9)
    trader = StubTrader()
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert rec.stop_loss == 98.0
    assert not (rec.raw or {}).get("trail_armed")
    assert trader.sl_orders == []


def test_patrol_arms_breakeven(db):
    # 峰值 3.0 ≥ 1×ATR×qty=3.0 → 武武装：SL 移保本价 100.1 + 落旗标 + SL_MOVE 事件
    rec = _rec(db, raw={"tp_mode": "trail", "entry_atr": 3.0}, peak_total_pnl=3.0)
    trader = StubTrader(price=101.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert rec.stop_loss == pytest.approx(100.1)
    assert rec.raw["trail_armed"] is True
    assert trader.sl_orders == [pytest.approx(100.1)]
    evs = db.query(TradeEvent).filter_by(event_type="SL_MOVE").all()
    assert len(evs) == 1


def test_patrol_falls_back_1r(db):
    # entry_atr 缺失（开仓快照失败/旧行）→ 武装线退化 1×risk_amount=2.0，峰值 2.0 恰达标
    rec = _rec(db, raw={"tp_mode": "trail"}, peak_total_pnl=2.0)
    trader = StubTrader(price=101.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert rec.raw["trail_armed"] is True
    assert rec.stop_loss == pytest.approx(100.1)


def test_patrol_immediate_trigger_retries(db):
    # 现价 99 低于保本价 100.1：挂上即触发 → 不动 SL、不落旗标，下轮巡检重试
    rec = _rec(db, raw={"tp_mode": "trail", "entry_atr": 3.0}, peak_total_pnl=3.0)
    trader = StubTrader(price=99.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert rec.stop_loss == 98.0
    assert not (rec.raw or {}).get("trail_armed")
    assert trader.sl_orders == []


def test_patrol_already_armed_trails_only(db):
    # 已武装：跳过武装块直接跟进止损（K 线腿无数据 → 无候选不动 SL，幂等）
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=100.1)
    trader = StubTrader(price=101.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert trader.sl_orders == []
    assert float(rec.stop_loss) == pytest.approx(100.1)


# ── 6. _ema21_half_cut：武装后破 EMA21 市价减半仓 ────────────────


def _klines_1h(closes):
    """closes → 1h K 线 6 列（ts 递增、o=前收盘、h=c+0.5、l=c-0.5；尾根为
    未收盘桩数据，函数内部剔尾后 closed[-1] 为倒数第二根——构造时直接把
    破线收盘放在倒数第二根即可）"""
    base = 1_700_000_000_000
    out = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        c = float(c)
        out.append([base + i * 3_600_000, prev, c + 0.5, c - 0.5, c, 1000.0])
        prev = c
    return out


def _patch_k1(monkeypatch, closes):
    kl = _klines_1h(closes)
    monkeypatch.setattr(te, "ExchangePool",
                        lambda: SimpleNamespace(get_klines=lambda *a, **k: kl))


def test_ema21_cut_long_breach(db, monkeypatch):
    """武装多单 + 1h 收盘跌破 EMA21 → 市价减半、qty 同步、旗标落、事件写"""
    # 40 根横盘 105（EMA21≈105）→ 末 20 根缓跌，倒数第 2 根（已收盘最后一根）100 < EMA21
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=100.1)
    trader = StubTrader(price=100.5, amt=1.0, min_qty=0.001)
    te._ema21_half_cut(db, trader, rec)
    assert len(trader.market_closes) == 1
    side, q, ro = trader.market_closes[0]
    assert side == "SELL" and ro is True  # 多单减仓 = 卖出 reduceOnly
    assert q == pytest.approx(0.5)
    assert float(rec.qty) == pytest.approx(0.5)
    assert rec.raw["ema21_cut"] is True
    # 减半腿已实现盈亏落账（income 实录口径）
    assert float(rec.realized_pnl) == pytest.approx(1.23)
    evs = db.query(TradeEvent).filter_by(event_type="CUT_EMA21").all()
    assert len(evs) == 1
    assert evs[0].detail["cut_qty"] == pytest.approx(0.5)


def test_ema21_cut_short_mirror(db, monkeypatch):
    """空单镜像：1h 收盘升破 EMA21 → 买入 reduceOnly 减半"""
    closes = [95.0] * 40 + [95.0 + 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, direction="short", stop_loss=99.9,
               raw={"tp_mode": "trail", "trail_armed": True})
    trader = StubTrader(price=99.5, amt=1.0)
    te._ema21_half_cut(db, trader, rec)
    assert len(trader.market_closes) == 1
    side, q, ro = trader.market_closes[0]
    assert side == "BUY" and ro is True
    assert float(rec.qty) == pytest.approx(0.5)


def test_ema21_cut_no_breach_no_action(db, monkeypatch):
    """收盘仍在 EMA21 上方（多单）→ 不减仓不落旗标"""
    _patch_k1(monkeypatch, [105.0] * 60)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=100.1)
    trader = StubTrader(price=105.5, amt=1.0)
    te._ema21_half_cut(db, trader, rec)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("ema21_cut")


def test_ema21_cut_not_armed_skipped(db, monkeypatch):
    """未武装（未开始移动止损）→ 巡检不进减半腿，市价单零调用"""
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, raw={"tp_mode": "trail", "entry_atr": 3.0}, peak_total_pnl=0.5)
    trader = StubTrader(price=100.5, amt=1.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("ema21_cut")


def test_ema21_cut_once_only(db, monkeypatch):
    """已减过（ema21_cut 旗标在）→ 永不再查，即使再次破线"""
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True, "ema21_cut": True},
               qty=0.5, stop_loss=100.1)
    trader = StubTrader(price=100.5, amt=0.5)
    te._ema21_half_cut(db, trader, rec)
    assert trader.market_closes == []


def test_ema21_cut_min_qty_skip(db, monkeypatch):
    """减半量或剩余量低于交易所 min_qty → 不切不落旗标（留给 SL 全平）"""
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, qty=0.001, raw={"tp_mode": "trail", "trail_armed": True},
               stop_loss=100.1)
    trader = StubTrader(price=100.5, amt=0.001, min_qty=0.001)
    te._ema21_half_cut(db, trader, rec)
    assert trader.market_closes == []
    assert not (rec.raw or {}).get("ema21_cut")


def test_ema21_cut_double_cut_guard(db, monkeypatch):
    """双刀竞态防护：市价单已成交但旗标未落库（commit 回滚/响应超时）→
    交易所 amt 已 ≈ 记录 qty 的一半，视为已减过——补旗标同步数量，不再下第二刀"""
    closes = [105.0] * 40 + [105.0 - 0.25 * i for i in range(1, 21)]
    _patch_k1(monkeypatch, closes)
    rec = _rec(db, raw={"tp_mode": "trail", "trail_armed": True}, stop_loss=100.1)
    trader = StubTrader(price=100.5, amt=0.5)  # 交易所已半仓，DB 仍记 1.0
    te._ema21_half_cut(db, trader, rec)
    assert trader.market_closes == []
    assert rec.raw["ema21_cut"] is True
    assert float(rec.qty) == pytest.approx(0.5)


# ── 7. 导入持仓：无止盈位 → trail 纯跟踪接管（2026-10-10）────────


def test_adopt_without_tp1_trail(db):
    """无止盈位（tp1=0）→ trail 接管：不挂 TP 档、tp1 落 None、
    raw 带 tp_mode/entry_atr（K线桩空 → None 退化 1R）、OPEN 事件标注纯跟踪"""
    trader = StubTrader(price=100.0, amt=2.0)
    rec = te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0, tp1=0)
    assert rec.status == "OPENED"
    assert rec.tp1 is None and rec.tp2 == 0
    assert rec.raw["tp_mode"] == "trail"
    assert rec.raw["entry_atr"] is None
    assert rec.raw["tp1_order_id"] is None
    assert trader.tp_orders == []           # 未挂任何止盈单
    assert trader.sl_orders == [98.0]       # SL 照挂
    assert rec.risk_amount == pytest.approx(abs(100.0 - 98.0) * 2.0)  # 1R 武装线分母
    ev = db.query(TradeEvent).filter_by(event_type="OPEN").first()
    assert ev.detail["tp_mode"] == "trail"


def test_adopt_with_tp1_ladder_unchanged(db):
    """有止盈位 → 现行 ladder 接管不变：挂 TP1、raw.tp_mode=ladder"""
    trader = StubTrader(price=100.0, amt=2.0, min_qty=0.001)
    rec = te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0, tp1=104.0)
    assert rec.tp1 == 104.0
    assert rec.raw["tp_mode"] == "ladder"
    assert len(trader.tp_orders) == 1       # TP1 挂上（qty_tp1 达 min_qty）
    assert trader.tp_orders[0] == pytest.approx(104.0)


def test_adopt_trail_rejects_wrong_sl_side(db):
    """trail 接管仅校验 SL 方向：多头 SL ≥ 现价仍拒收"""
    trader = StubTrader(price=100.0, amt=2.0)
    with pytest.raises(ValueError, match="必须低于现价"):
        te.adopt_position(db, trader, "BTCUSDT", stop_loss=100.0, tp1=0)


def test_adopt_trail_ignores_tp2(db):
    """tp1=0 时即使误传 tp2 也走 trail（tp2 无效，不参与校验/挂单）"""
    trader = StubTrader(price=100.0, amt=2.0)
    rec = te.adopt_position(db, trader, "BTCUSDT", stop_loss=98.0,
                            tp1=0, tp2=110.0)
    assert rec.raw["tp_mode"] == "trail"
    assert trader.tp_orders == []
    assert rec.tp2 == 0


def test_auto_adopt_no_tp1_imports(db, monkeypatch):
    """自动导入：仅 SL 无 TP1 的 App 手动单不再跳过 → 按 trail 接管"""
    monkeypatch.setattr(te, "list_adoptable_positions", lambda db_, tr: [
        {"symbol": "BTCUSDT", "tracked": False,
         "prefill": {"stop_loss": 98.0, "tp1": None, "tp2": None}},
    ])
    trader = StubTrader(price=100.0, amt=1.0)
    out = te.auto_adopt_positions(db, trader)
    assert out == ["BTCUSDT"]
    rec = db.query(TradeRecord).first()
    assert rec is not None and rec.raw["tp_mode"] == "trail"


def test_adopt_trail_deep_profit_arms_first_patrol(db):
    """接管时已深浮盈（峰值 ≥ 武装线）→ 首轮巡检立即武装保本：
    adopted 单 peak 从接管时刻起累计（含接管前涨幅的当前浮盈快照），
    entry_atr 快照缺失（K线桩空）→ 退化 1R=risk_amount=2.0 武装线"""
    rec = _rec(db, raw={"tp_mode": "trail", "adopted": True},
               peak_total_pnl=3.0, stop_loss=98.0)
    trader = StubTrader(price=103.0, amt=1.0)
    te._trail_mode_patrol(db, trader, rec, "1h", set(), FEE, atr_k=0)
    assert rec.raw["trail_armed"] is True          # 已落旗标
    assert trader.sl_orders == [100.1]             # 保本价 100 + 0.1 费缓冲
    evs = db.query(TradeEvent).filter_by(event_type="SL_MOVE").all()
    assert len(evs) == 1 and evs[0].detail["to"] == pytest.approx(100.1)
