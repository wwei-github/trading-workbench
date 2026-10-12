# -*- coding: utf-8 -*-
"""4h 状态机接入 trail 出场（2026-10-12 拍板，docs/21 O1）——单测。

锁五件事：
1. 下车态矩阵（多空 × 11 态）：逆侧趋势级反转（macro_cross/aligned/recover 逆侧）
   + turn 同侧 → EXIT_HTF_STATE 事件 + 撤 algo + 市价全平 + exit_override；
   缠绕与过渡态（bull_dip/bear_rally）等其余 7 态不动作；
2. analyze_htf_background 返回 None（4h <156 已收盘根）→ 不动作且不计 fail；
3. 拉取异常 fail-closed：首桶 streak=1 不动作；连续第 2 个 4h 桶仍失败 →
   保守全平（fail_closed=True）；同桶重复巡检不重复计数；
4. 4h 换根缓存：raw.htf_state.bar == 未收盘根开盘时刻 → 桶内命中零重算
   （analyze_htf_background 被调即 AssertionError）；缓存态为下车态照样全平；
5. 成功判定后 fail 计数清零（htf_state_fail 复位 None）。

sqlite 内存库夹具镜像 test_tp_mode.py；ExchangePool / analyze_htf_background /
close_market_now 全桩化（被测单元是 _htf_state_exit 判定逻辑，不是平仓执行）。
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
    """最小 trader 桩：_htf_state_exit 只调 cancel_all_algo（平仓执行被桩掉）"""

    def __init__(self):
        self.cancelled: list[str] = []

    def cancel_all_algo(self, symbol):
        self.cancelled.append(symbol)


def _patch(monkeypatch, *, kl=None, bg=None, raise_on_fetch=False,
           bg_raises=False):
    """装配一轮判定：ExchangePool 返回 kl（或抛异常），analyze_htf_background
    返回 bg（或抛 AssertionError 证明未被调）；close_market_now 换成记录桩。
    返回 close 调用记录列表。"""
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
    monkeypatch.setattr(te, "close_market_now",
                        lambda db_, trader_, rec_: closes.append(rec_.id) or {})
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
        assert trader.cancelled == ["BTCUSDT"]
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
    EXIT_HTF_STATE(fail_closed=True) + exit_override + 撤单 + 平仓"""
    closes = _patch(monkeypatch, raise_on_fetch=True)
    rec = _rec(db, raw={"tp_mode": "trail",
                        "htf_state_fail": {"bucket": _bucket() - 1, "streak": 1}})
    trader = StubTrader()
    assert te._htf_state_exit(db, trader, rec) is True
    assert closes == [rec.id]
    assert trader.cancelled == ["BTCUSDT"]
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
