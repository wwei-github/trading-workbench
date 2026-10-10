# -*- coding: utf-8 -*-
"""止盈模式 tp_mode（2026-10-10，docs/19 §3.4 两派哲学落地）——单测。

锁五件事：
1. normalize_raw：trail/ladder 保真，非法值（含空串/未声明）归 None；
2. validate_decision：suggest 落库 trail 保真 / 缺省回退 ladder / skip 恒 None；
3. ai_agent._handle_submit：trail 硬清零 TP2（即使 AI 报了 tp2 锚点）、
   非法 tp_mode 归 None 后落库回退 ladder；
4. _derive_exit_reason trail 三态：未武装=sl / SL 贴保本=breakeven_sl /
   越过保本=trail_sl（多空对称）；
5. _trail_mode_patrol：未达标不武装 / 达标武装保本+落旗标+SL_MOVE /
   entry_atr 缺失退化 1R 武装线 / 挂上即触发不落旗标下轮重试 / 已武装只跟进。

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

    Base.metadata.create_all(bind=engine, tables=[TradeRecord.__table__, TradeEvent.__table__])
    # ExchangePool 桩：跟进止损的 K 线腿拿不到数据 → 摆动/ATR 腿全跳过
    # （聚焦武装逻辑；已武装路径走到「无候选不动 SL」即视为跟进调用成功）
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
    """最小 trader 桩：round_price / price / stop_market_close / cancel_order"""

    def __init__(self, price=101.0):
        self.price_val = price
        self.sl_orders: list[float] = []

    def round_price(self, symbol, p):
        return round(p, 2)

    def price(self, symbol):
        return self.price_val

    def stop_market_close(self, symbol, side, sl):
        self.sl_orders.append(sl)
        return {"algoId": 500 + len(self.sl_orders)}

    def cancel_order(self, symbol, oid):
        pass


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
