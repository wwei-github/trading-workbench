# -*- coding: utf-8 -*-
"""逐笔 R 泄漏对账单测（docs/24 A1）

不开真实 HTTP/DB：TradeRecord/TradeEvent 直接 ORM 构造，db 桩为
SimpleNamespace（execute 只需返回事件列表），trader 桩记录调用参数并
返回固定 income/分档盈亏。

覆盖：理论 R 计算、泄漏各项 USDT/R 换算（多空方向符号）、交易所回补
（income / fill_tranche_pnls）、缺失数据 null 与 gaps 提示、数量不符、
端点 404 与转发。
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.trade_recon as recon_mod  # noqa: E402
from app.api.trades import trade_reconciliation  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402
from app.services.trade_recon import reconcile_trade  # noqa: E402


def _rec(**kw):
    """标准 tp1_then_sl 多单：计划 100/98/101.5、risk 2、实际成交 100.1"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="CLOSED",
        exit_reason="tp1_then_sl", testnet=True, account_key="testnet-1",
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=101.5, tp2=0.0,
        fee_total=-0.5, funding_total=-0.2, realized_pnl=0.08,
        ai_snapshot={"entry_price": 100.0, "stop_loss": 98.0,
                     "take_profit_1": 101.5, "take_profit_2": 0},
        raw={"qty_tp1": 0.75, "qty_tp2": 0.0, "sl_order_id": 1},
        opened_at=datetime(2026, 9, 28, 4, 0, 0),
    )
    base.update(kw)
    return TradeRecord(**base)


def _evt(rec, etype, detail, at=None):
    return TradeEvent(trade_record_id=rec.id, event_type=etype, detail=detail,
                      created_at=at or datetime(2026, 9, 28, 5, 0, 0))


def _db(events):
    """会话桩：reconcile_trade 只用 execute(...).scalars().all() 取事件历史"""
    return SimpleNamespace(execute=lambda q: SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: events)))


class _FakeTrader:
    """交易所桩：记录调用参数；fail=True 时 income/tranche 均抛错"""

    def __init__(self, income=None, tranche=None, fail=False):
        self.income = income or []
        self.tranche = tranche
        self.fail = fail
        self.calls = []

    def income_rows(self, symbol, since_ms):
        self.calls.append(("income", symbol, since_ms))
        if self.fail:
            raise RuntimeError("网络失败")
        return self.income

    def fill_tranche_pnls(self, symbol, since_ms, qty_tp1, qty_tp2):
        self.calls.append(("tranche", symbol, since_ms, qty_tp1, qty_tp2))
        if self.fail or self.tranche is None:
            raise ValueError("CLOSE 成交量对不上")
        return self.tranche


def test_full_reconciliation_long():
    """数据齐全：计划三价取 AI 快照，五项泄漏与理论/实际 R 全部量化"""
    rec = _rec()
    events = [
        _evt(rec, "OPEN", {"price": 100.05, "qty": 1.0, "fill_price": 100.1,
                           "slippage_pct": 0.1, "actual_risk": 2.1}),
        _evt(rec, "TP1_FILL", {"qty": 0.75, "pnl": 1.1, "cum_pnl": 1.1}),
        _evt(rec, "SL_FILL", {"trigger_price": 100.0, "price": 99.7, "qty": 0.25}),
    ]
    out = reconcile_trade(_db(events), rec)

    assert out["trade_id"] == str(rec.id)
    plan = out["plan"]
    assert plan["source"] == "ai_snapshot"
    assert (plan["entry"], plan["sl"], plan["tp1"]) == (100.0, 98.0, 101.5)
    assert plan["qty_tp1"] == 0.75 and plan["qty_total"] == 1.0
    assert plan["rr1"] == pytest.approx(0.75)

    actual = out["actual"]
    assert actual["entry_price"] == 100.1 and actual["slippage_pct"] == 0.1
    assert actual["tp1_fill"]["pnl"] == 1.1
    assert actual["sl_fill"]["trigger_price"] == 100.0
    assert actual["fee_total"] == -0.5 and actual["funding_total"] == -0.2

    lk = out["leakage"]
    # a) 入场滑点 (100.1−100)×1.0 = 0.1（不利 = 正），R = 0.05
    assert lk["entry_slip"]["usdt"] == pytest.approx(0.1)
    assert lk["entry_slip"]["r"] == pytest.approx(0.05)
    # b) 手续费取绝对值；c) 资金费成本口径（支付 −0.2 → +0.2）
    assert lk["fee"]["usdt"] == pytest.approx(0.5)
    assert lk["funding"]["usdt"] == pytest.approx(0.2)
    # d) SL 穿价：多单成交低于触发价 → (100−99.7)×0.25 = 0.075
    assert lk["sl_breach"]["usdt"] == pytest.approx(0.075)
    # e) TP1 滑点：反推成交均价 100.1+1.1/0.75≈101.5667 → 多单卖在计划 101.5 上方
    #    = 有利 → (101.5−fill)×0.75 ≈ −0.05（成本口径：不利 = 正，有利 = 负）
    assert lk["tp1_slip"]["usdt"] == pytest.approx(-0.05, abs=1e-3)
    assert lk["tp1_slip"]["r"] == pytest.approx(-0.025, abs=1e-3)
    # f) 理论 = 0.75 档 × 1.5 价差 = 1.125 USDT = 0.5625R；实际 0.08 = 0.04R
    s = lk["summary"]
    assert s["theoretical_usdt"] == pytest.approx(1.125)
    assert s["theoretical_r"] == pytest.approx(0.5625)
    assert s["actual_usdt"] == pytest.approx(0.08)
    assert s["actual_r"] == pytest.approx(0.04)
    assert s["leakage_usdt"] == pytest.approx(1.045)
    assert s["leakage_r"] == pytest.approx(0.5225)
    assert s["covered_usdt"] == pytest.approx(0.825)
    # 明细齐全无缺失类提示，但穿价后保本亏损未入账 → 未解释项提示
    assert not any("SL_FILL" in g or "income" in g for g in out["gaps"])
    assert any("未解释" in g for g in out["gaps"])
    assert s["unexplained_usdt"] == pytest.approx(0.22)


def test_short_backfill_and_fallbacks():
    """空单 + raw 缺分批（按 75% 估算）+ fee/funding 走 income 回补 +
    TP1 分档盈亏走 fill_tranche_pnls 回退，验证方向符号"""
    trader = _FakeTrader(
        income=[{"income": "-0.3", "incomeType": "COMMISSION"},
                {"income": "0.1", "incomeType": "FUNDING_FEE"}],
        tranche=(1.2, 0.0),
    )
    rec = _rec(direction="short", ai_snapshot=None, raw={},
               entry_price=50.0, stop_loss=51.0, tp1=48.5, tp2=0.0,
               qty=2.0, risk_amount=2.0, fee_total=None, funding_total=None,
               realized_pnl=1.0)
    events = [
        _evt(rec, "OPEN", {"fill_price": 49.8, "qty": 2.0}),
        _evt(rec, "TP1_FILL", {"qty": 1.5}),
    ]
    out = reconcile_trade(_db(events), rec, trader=trader)

    assert out["plan"]["source"] == "record"
    # raw 缺分批数量 → 按 75% 规则估算并提示
    assert out["plan"]["qty_tp1"] == pytest.approx(1.5)
    assert out["plan"]["qty_tp2"] == 0.0
    assert any("分批" in g for g in out["gaps"])
    # income 回补：手续费 |−0.3| = 0.3；资金费收到 0.1 → 成本 −0.1（有利）
    assert out["leakage"]["fee"]["usdt"] == pytest.approx(0.3)
    assert out["leakage"]["funding"]["usdt"] == pytest.approx(-0.1)
    assert ("income", "BTCUSDT",
            int(datetime(2026, 9, 28, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000) - 5000
            ) in trader.calls
    # 空单入场滑点：49.8 低于计划 50（卖低了）→ (50−49.8)×2 = +0.4 不利
    assert out["leakage"]["entry_slip"]["usdt"] == pytest.approx(0.4)
    # TP1 分档盈亏反推：成交均价 49.8−1.2/1.5=49.0 → 空单买回比计划 48.5 贵 0.5
    # → 0.5×1.5 = +0.75 不利（成本口径：不利 = 正）
    assert out["leakage"]["tp1_slip"]["usdt"] == pytest.approx(0.75)
    assert ("tranche", "BTCUSDT", trader.calls[1][2], 1.5, 0.0) in trader.calls
    # 理论 R = 档位占比 0.75 × RR1 1.5 = 1.125；实际 = 1.0/2 = 0.5
    s = out["leakage"]["summary"]
    assert s["theoretical_r"] == pytest.approx(1.125)
    assert s["actual_r"] == pytest.approx(0.5)
    assert s["leakage_usdt"] == pytest.approx(1.25)
    assert any("SL_FILL" in g for g in out["gaps"])


def test_missing_data_nulls_and_gaps():
    """极端缺数据：各项 null 不抛异常，缺口逐条落 gaps"""
    trader = _FakeTrader(fail=True)  # income / tranche 全抛错
    rec = _rec(ai_snapshot=None, raw={}, fee_total=None, funding_total=None,
               realized_pnl=None, risk_amount=None, qty=None, entry_price=None)
    out = reconcile_trade(_db([]), rec, trader=trader)

    lk = out["leakage"]
    assert lk["entry_slip"] is None
    assert lk["fee"] is None and lk["funding"] is None
    assert lk["tp1_slip"] is None and lk["sl_breach"] is None
    assert lk["summary"]["theoretical_r"] is None
    assert lk["summary"]["leakage_usdt"] is None
    assert lk["summary"]["covered_usdt"] is None
    msgs = "\n".join(out["gaps"])
    assert "income 明细拉取失败" in msgs
    assert "risk_amount" in msgs
    assert "SL_FILL" in msgs
    assert "TP1 成交分档盈亏" in msgs
    assert "计划入场价" in msgs


def test_qty_mismatch_gap():
    """TP1 实际成交数量与计划分批不符 → gap 提示"""
    rec = _rec()
    events = [
        _evt(rec, "OPEN", {"fill_price": 100.0, "qty": 1.0}),
        _evt(rec, "TP1_FILL", {"qty": 0.7, "pnl": 1.0}),
    ]
    out = reconcile_trade(_db(events), rec)
    assert any("不符" in g for g in out["gaps"])


def test_endpoint_404_and_forward(monkeypatch):
    """不存在 404；命中时按 id 直查转发 reconcile_trade"""
    with pytest.raises(HTTPException) as ei:
        trade_reconciliation("no-such-id", SimpleNamespace(get=lambda m, i: None))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        trade_reconciliation("not-a-uuid", SimpleNamespace(get=lambda m, i: None))
    assert ei.value.status_code == 404

    rec = _rec()
    sentinel = {"ok": True}
    monkeypatch.setattr(recon_mod, "reconcile_trade", lambda db, r: sentinel)
    assert trade_reconciliation(str(rec.id),
                                SimpleNamespace(get=lambda m, i: rec)) is sentinel
