# -*- coding: utf-8 -*-
"""4h 状态机接入 trail 出场（2026-10-12 拍板，docs/21 O1）——单测。

锁六件事：
1. 下车态矩阵（多空 × 11 态）：逆侧趋势级反转（macro_cross/aligned/recover 逆侧）
   + turn 同侧 → EXIT_HTF_STATE 事件 + 市价全平 + exit_override；
   缠绕与过渡态（bull_dip/bear_rally）等其余 7 态不动作；
   判定路径不预撤单（撤单交 close_market_now 内部先平后撤，trader.cancelled 恒空）；
2. analyze_htf_background 返回 None（4h <156 已收盘根）→ 不动作且不计 fail；
3. 拉取异常 fail-closed：首桶 streak=1 不动作；连续第 2 个 4h 桶仍失败 →
   保守全平（fail_closed=True）；同桶重复巡检不重复计数；
4. 4h 换根缓存：raw.htf_state.bar == 未收盘根开盘时刻 → 桶内命中零重算
   （analyze_htf_background 被调即 AssertionError）；缓存态为下车态照样全平；
5. 成功判定后 fail 计数清零（htf_state_fail 复位 None）；
6. 全平执行修复回归（2026-10-12-7）：真实 close_market_now 集成落
   exit_reason=htf_state（飞书按钮默认 manual 标签不变）；EXIT_HTF_STATE
   事件幂等（失败重试不重复落）；失败清理 override——仓位仍开放清除
   （防后续真实 SL 被 settle 误标）、已归零保留（等下轮落账）。

sqlite 内存库夹具镜像 test_tp_mode.py；ExchangePool / analyze_htf_background
/ close_market_now 桩化（1~5 组判定矩阵）；第 6 组走真实 close_market_now
（只桩 ExchangePool/analyze 与 time.sleep）。
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
import app.services.strategy.technical as tech  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402

ALL_STATES = (
    "tangle", "turn_bull", "turn_bear",
    "macro_cross_bull", "macro_cross_bear",
    "bull_recover", "bear_recover",
    "aligned_bull", "aligned_bear",
    "bull_dip", "bear_rally",
)
LONG_EXIT = {"macro_cross_bear", "aligned_bear", "bear_recover", "turn_bull"}
SHORT_EXIT = {"macro_cross_bull", "aligned_bull", "bull_recover", "turn_bear"}

BAR = 1_700_000_000_000  # 4h 未收盘根开盘时刻（换根检测键）


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(bind=engine, tables=[
        TradeRecord.__table__, TradeEvent.__table__])
    return Session(bind=engine)


def _rec(db, direction="long", raw=None):
    rec = TradeRecord(
        id=uuid.uuid4(), symbol="BTCUSDT", direction=direction, status="OPENED",
        testnet=True, account_key="testnet-1",
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=None, tp2=0.0,
        realized_pnl=0.0, unrealized_pnl=0.0,
        raw=raw if raw is not None else {"tp_mode": "trail", "trail_armed": True},
        opened_at=datetime.utcnow(),
    )
    db.add(rec)
    db.commit()
    return rec


class StubTrader:
    """最小 trader 桩。

    判定矩阵（1~5 组）：close_market_now 被桩掉——本桩只需 positions
    （全平失败时的清理二分）与 cancel_all_algo（验「不预撤单」恒空）。
    真实 close_market_now 集成（第 6 组）：另需 round_qty / market_order /
    income_rows / sum_income / INCOME_KEEP；calls 记录执行序（先平仓后撤单断言）。
    """

    INCOME_KEEP = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR"}

    def __init__(self, amt=1.0):
        self.cancelled: list[str] = []
        self.market_closes: list[tuple] = []
        self.calls: list[str] = []
        self.amt = amt

    def positions(self):
        return {"BTCUSDT": {"amt": self.amt}} if self.amt > 0 else {}

    def round_qty(self, symbol, qty):
        return round(qty, 3)

    def market_order(self, symbol, side, qty, reduce_only=False):
        self.calls.append("order")
        self.market_closes.append((side, qty, reduce_only))
        if reduce_only:
            self.amt = max(0.0, self.amt - qty)  # 桩内模拟减仓
        return {"orderId": 900 + len(self.market_closes)}

    def cancel_all_algo(self, symbol):
        self.calls.append("cancel")
        self.cancelled.append(symbol)

    def income_rows(self, symbol, since_ms):
        return [
            {"incomeType": "REALIZED_PNL", "income": "2.0"},
            {"incomeType": "COMMISSION", "income": "-0.1"},
        ]

    def sum_income(self, rows, keep):
        return sum(float(r.get("income") or 0) for r in rows
                   if r.get("incomeType") in keep)


def _patch(monkeypatch, *, kl=None, bg=None, raise_on_fetch=False,
           bg_raises=False, close_impl="stub"):
    """装配一轮判定：ExchangePool 返回 kl（或抛异常），analyze_htf_background
    返回 bg（或抛 AssertionError 证明未被调）；close_market_now 默认换记录桩
    （close_impl="stub"），close_impl=None 保留真实实现（第 6 组集成用例），
    可调用对象则直接替换（失败/重试用例）。返回 close 桩调用记录列表。"""
    closes: list = []

    def _get_klines(symbol, interval, limit, **kw):
        if raise_on_fetch:
            raise RuntimeError("exchange down")
        return kl if kl is not None else []

    monkeypatch.setattr(te, "ExchangePool",
                        lambda: SimpleNamespace(get_klines=_get_klines))

    def _bg(klines):
        if bg_raises:
            raise AssertionError("缓存命中不应重算 analyze_htf_background")
        return bg

    monkeypatch.setattr(tech, "analyze_htf_background", _bg)
    if close_impl == "stub":
        monkeypatch.setattr(
            te, "close_market_now",
            lambda db_, trader_, rec_, **kw: closes.append(rec_.id) or {})
    elif callable(close_impl):
        monkeypatch.setattr(te, "close_market_now", close_impl)
    # close_impl=None → 保留真实 close_market_now（集成用例）
    return closes


def _kl(bar=BAR):
    """伪 4h 序列：仅需非空且 kl[-1][0] 为换根检测键"""
    return [[bar, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]]


def _bucket():
    return int(datetime.utcnow().timestamp() // (4 * 3600))


# ── 1. 下车态矩阵 ────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ALL_STATES)
def test_exit_matrix_long(db, monkeypatch, state):
    """多单：逆侧趋势级反转（macro_cross_bear/aligned_bear/bear_recover）
    + turn_bull 同侧拐头 → 全平；其余 7 态不动作"""
    closes = _patch(monkeypatch, kl=_kl(),
                    bg={"state": state, "age_bars": 3})
    rec = _rec(db, direction="long")
    trader = StubTrader()
    exited = te._htf_state_exit(db, trader, rec)
    if state in LONG_EXIT:
        assert exited is True
        assert closes == [rec.id]
        assert trader.cancelled == []  # 不预撤单（撤单交 close_market_now 先平后撤）
        assert rec.raw["exit_override"] == "htf_state"
        evs = db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").all()
        assert len(evs) == 1 and evs[0].detail["state"] == state
    else:
        assert exited is False
        assert closes == [] and trader.cancelled == []
        assert db.query(TradeEvent).count() == 0


@pytest.mark.parametrize("state", ALL_STATES)
def test_exit_matrix_short(db, monkeypatch, state):
    """空单镜像：macro_cross_bull/aligned_bull/bull_recover/turn_bear → 全平"""
    closes = _patch(monkeypatch, kl=_kl(),
                    bg={"state": state, "age_bars": 3})
    rec = _rec(db, direction="short")
    trader = StubTrader()
    exited = te._htf_state_exit(db, trader, rec)
    if state in SHORT_EXIT:
        assert exited is True
        assert closes == [rec.id]
        assert rec.raw["exit_override"] == "htf_state"
    else:
        assert exited is False
        assert closes == []
        assert db.query(TradeEvent).count() == 0


# ── 2. 数据不足（analyze 返回 None）──────────────────────────────────


def test_htf_data_insufficient_no_action(db, monkeypatch):
    """4h <156 已收盘根（bg=None）→ 不动作，且不计 fail（新币常态）"""
    closes = _patch(monkeypatch, kl=_kl(), bg=None)
    rec = _rec(db)
    assert te._htf_state_exit(db, StubTrader(), rec) is False
    assert closes == []
    assert "htf_state_fail" not in (rec.raw or {})


# ── 3. 拉取异常 fail-closed ─────────────────────────────────────────


def test_fetch_fail_first_bucket_no_action(db, monkeypatch):
    """首个 4h 桶拉取异常 → 不动作，streak=1 落库（下桶再败才全平）"""
    closes = _patch(monkeypatch, raise_on_fetch=True)
    rec = _rec(db)
    assert te._htf_state_exit(db, StubTrader(), rec) is False
    assert closes == []
    fail = rec.raw["htf_state_fail"]
    assert fail == {"bucket": _bucket(), "streak": 1}


def test_fetch_fail_second_bucket_fail_closed(db, monkeypatch):
    """连续第 2 个 4h 桶仍拉取失败 → 保守全平（4h 盲飞不可接受）：
    EXIT_HTF_STATE(fail_closed=True) + exit_override + 市价全平（不预撤单）"""
    closes = _patch(monkeypatch, raise_on_fetch=True)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state_fail": {"bucket": _bucket() - 1, "streak": 1}})
    trader = StubTrader()
    assert te._htf_state_exit(db, trader, rec) is True
    assert closes == [rec.id]
    assert trader.cancelled == []
    assert rec.raw["exit_override"] == "htf_state"
    assert rec.raw["htf_state_fail"]["streak"] == 2
    evs = db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").all()
    assert len(evs) == 1
    assert evs[0].detail["fail_closed"] is True and evs[0].detail["state"] is None


def test_fetch_fail_same_bucket_not_double_counted(db, monkeypatch):
    """同一 4h 桶内重复巡检失败 → streak 不递增（桶粒度计数），不全平"""
    closes = _patch(monkeypatch, raise_on_fetch=True)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state_fail": {"bucket": _bucket(), "streak": 1}})
    assert te._htf_state_exit(db, StubTrader(), rec) is False
    assert closes == []
    assert rec.raw["htf_state_fail"]["streak"] == 1


# ── 4. 4h 换根缓存 ──────────────────────────────────────────────────


def test_cache_same_bar_no_recompute(db, monkeypatch):
    """raw.htf_state.bar == 当前未收盘根 → 桶内命中缓存零重算
    （analyze 被调即 AssertionError）；缓存非下车态 → 不动作"""
    closes = _patch(monkeypatch, kl=_kl(), bg_raises=True)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state": {"bar": BAR, "state": "tangle", "age": 5}})
    assert te._htf_state_exit(db, StubTrader(), rec) is False
    assert closes == []


def test_cache_exit_state_still_exits(db, monkeypatch):
    """缓存态为下车态（换根前算的）→ 照样全平，不重算"""
    closes = _patch(monkeypatch, kl=_kl(), bg_raises=True)
    rec = _rec(db, direction="long",
               raw={"tp_mode": "trail",
                    "htf_state": {"bar": BAR, "state": "aligned_bear", "age": 9}})
    trader = StubTrader()
    assert te._htf_state_exit(db, trader, rec) is True
    assert closes == [rec.id]
    evs = db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").all()
    assert evs[0].detail["state"] == "aligned_bear"


# ── 5. 成功判定复位 fail 计数 ────────────────────────────────────────


def test_success_clears_fail_streak(db, monkeypatch):
    """此前拉取失败 streak=1，本轮成功（非下车态）→ htf_state_fail 复位 None，
    htf_state 换根缓存落库"""
    closes = _patch(monkeypatch, kl=_kl(),
                    bg={"state": "aligned_bull", "age_bars": 12})
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state_fail": {"bucket": _bucket(), "streak": 1}})
    assert te._htf_state_exit(db, StubTrader(), rec) is False
    assert closes == []
    assert rec.raw["htf_state_fail"] is None
    assert rec.raw["htf_state"] == {"bar": BAR, "state": "aligned_bull", "age": 12}


# ── 6. 全平执行修复回归（2026-10-12-7）──────────────────────────────


def test_manual_close_default_label_unchanged(db, monkeypatch):
    """飞书按钮口径回归：close_market_now 不带 exit_reason → manual 标签不变"""
    monkeypatch.setattr("time.sleep", lambda s: None)
    rec = _rec(db)
    te.close_market_now(db, StubTrader(), rec)
    assert rec.status == "CLOSED" and rec.exit_reason == "manual"
    evs = db.query(TradeEvent).filter_by(event_type="MANUAL_CLOSE").all()
    assert len(evs) == 1 and evs[0].detail["exit_reason"] == "manual"
    assert rec.pnl_pct == 95.0  # (2.0 − 0.1) / 2.0 × 100


def test_real_close_labels_htf_state(db, monkeypatch):
    """真实 close_market_now 集成：4h 下车态全平落 exit_reason=htf_state
    （不再被硬编码误标手动平仓）；执行序为先市价平仓后撤挂单（不预撤）"""
    monkeypatch.setattr("time.sleep", lambda s: None)
    _patch(monkeypatch, kl=_kl(), bg={"state": "aligned_bear", "age_bars": 4},
           close_impl=None)
    rec = _rec(db, direction="long")
    trader = StubTrader()
    assert te._htf_state_exit(db, trader, rec) is True
    assert rec.status == "CLOSED" and rec.exit_reason == "htf_state"
    assert trader.calls.index("order") < trader.calls.index("cancel")
    evs = db.query(TradeEvent).filter_by(event_type="MANUAL_CLOSE").all()
    assert len(evs) == 1 and evs[0].detail["exit_reason"] == "htf_state"
    assert db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").count() == 1


def test_close_failure_clears_override_no_dup_event(db, monkeypatch):
    """平仓失败 + 仓位确认仍开放 → 清除 exit_override（否则后续真实 SL
    平仓会被 settle 误标 htf_state）；重试轮事件幂等、二次执行成功"""
    calls: list = []

    def _close_once(db_, trader_, rec_, **kw):
        calls.append(kw.get("exit_reason"))
        if len(calls) == 1:
            raise RuntimeError("exchange down")
        return {}

    _patch(monkeypatch, kl=_kl(), bg_raises=True, close_impl=_close_once)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state": {"bar": BAR, "state": "aligned_bear", "age": 9}})
    trader = StubTrader(amt=1.0)
    assert te._htf_state_exit(db, trader, rec) is False
    assert rec.raw["exit_override"] is None
    assert rec.raw["htf_exit_notified"] is True
    assert db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").count() == 1
    # 下轮重试（缓存态仍下车）：事件不重复落，平仓成功
    assert te._htf_state_exit(db, trader, rec) is True
    assert calls == ["htf_state", "htf_state"]
    assert rec.raw["exit_override"] == "htf_state"
    assert db.query(TradeEvent).filter_by(event_type="EXIT_HTF_STATE").count() == 1


def test_close_failure_position_gone_keeps_override(db, monkeypatch):
    """平仓失败但仓位已归零（平仓单已成交、结算段抛异常）→ 保留 override，
    下轮 settle 零仓分支按 htf_state 落账（不误标 sl）"""
    def _boom(db_, trader_, rec_, **kw):
        raise RuntimeError("settle crashed")

    _patch(monkeypatch, kl=_kl(), bg_raises=True, close_impl=_boom)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state": {"bar": BAR, "state": "aligned_bear", "age": 9}})
    assert te._htf_state_exit(db, StubTrader(amt=0.0), rec) is False
    assert rec.raw["exit_override"] == "htf_state"
