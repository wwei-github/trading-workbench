# -*- coding: utf-8 -*-
"""TP2 执行缺陷防护（docs/23 T14d / T14e，2026-10-29）。

T14d：AI 报了 TP2 却没挂上时（数量取整/合计溢出/下单失败）要能区分「策略没要
       TP2」与「要了没挂上」——`_tp2_skip_info` 给原因，落 raw.tp2_skipped。
T14e：全平模式（raw.tp_full_exit）TP2 成交后仍余残仓时，市价 reduceOnly 补平，
      同单只补一次，且补平后出场原因记 tp_full（不被误标止损类）。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _enforce_full_exit_residual,
    _tp2_skip_info,
)


# ── T14d：TP2 未挂上原因 ────────────────────────────────────────────────

def test_no_warning_when_tp2_absent_or_placed():
    assert _tp2_skip_info(0, None, 1.0, 0.5, 2.0, 0.1, True) is None
    assert _tp2_skip_info(110.5, {"algoId": 1}, 1.0, 0.5, 2.0, 0.1, True) is None


def test_reason_qty_below_min():
    info = _tp2_skip_info(110.5, None, 1.0, 0.02, 2.0, 0.1, True)
    assert info["reason"] == "qty_below_min"
    assert info["tp2"] == 110.5 and info["qty_tp2"] == 0.02


def test_reason_tp_sum_not_ok():
    # 全平模式：1.2 + 1.2 = 2.4 > 2.0 全仓
    info = _tp2_skip_info(110.5, None, 1.2, 1.2, 2.0, 0.1, True)
    assert info["reason"] == "tp_sum_not_ok"
    assert info["full_exit"] is True


def test_full_exit_allows_exact_sum():
    """全平模式允许两档合计=全仓：不再判溢出，reason 落到下单失败（数量合法但没单）"""
    info = _tp2_skip_info(110.5, None, 1.0, 1.0, 2.0, 0.1, True)
    assert info["reason"] == "order_failed"
    info_off = _tp2_skip_info(110.5, None, 1.0, 1.0, 2.0, 0.1, False)
    assert info_off["reason"] == "tp_sum_not_ok"


# ── T14e：全平模式残仓补平 ──────────────────────────────────────────────

class _FakeTrader:
    testnet = True
    account_key = "testnet-1"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple] = []

    def round_qty(self, symbol, qty):
        return round(float(qty), 3)

    def market_order(self, symbol, side, qty, reduce_only=False):
        if self.fail:
            raise RuntimeError("-2022 ReduceOnly Order is rejected")
        self.calls.append((symbol, side, qty, reduce_only))
        return {"orderId": 1}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _rec(db, *, full_exit: bool = True, raw_extra: dict | None = None) -> TradeRecord:
    rec = TradeRecord(
        symbol="TRUTHUSDT",
        direction="long",
        leverage=10,
        status="TP1_HIT",
        testnet=True,
        account_key="testnet-1",
        raw={"tp_full_exit": full_exit, "qty_tp2": 0.25, **(raw_extra or {})},
    )
    db.add(rec)
    db.flush()
    return rec


def test_residual_closed_once_and_marked(db):
    rt = _FakeTrader()
    rec = _rec(db)

    assert _enforce_full_exit_residual(db, rt, rec, amt=0.31, ratio=0.062) is True
    assert rt.calls == [("TRUTHUSDT", "SELL", 0.31, True)]
    assert rec.raw["full_exit_residual_at"]
    assert rec.raw["exit_override"] == "tp_full"  # 下一轮零仓位结算按 TP 档全平记

    events = db.query(TradeEvent).filter_by(trade_record_id=rec.id).all()
    assert [e.event_type for e in events] == ["FULL_EXIT_RESIDUAL"]
    assert events[0].detail["qty"] == 0.31

    # 同单只补一次：第二次直接返回 False，不再下单
    assert _enforce_full_exit_residual(db, rt, rec, amt=0.31, ratio=0.062) is False
    assert len(rt.calls) == 1


def test_skip_when_not_full_exit(db):
    """分批模式（tp_full_exit=false）不补平——尾仓本来就该留给跟踪止损"""
    rt = _FakeTrader()
    rec = _rec(db, full_exit=False)
    assert _enforce_full_exit_residual(db, rt, rec, amt=0.5, ratio=0.2) is False
    assert rt.calls == []


def test_no_position_no_order(db):
    rt = _FakeTrader()
    rec = _rec(db)
    assert _enforce_full_exit_residual(db, rt, rec, amt=0.0, ratio=0.0) is False
    assert rt.calls == []


def test_failure_logged_without_marker(db):
    """补平失败：记 FAILED 事件但不写幂等标记（下轮巡检重试）"""
    rt = _FakeTrader(fail=True)
    rec = _rec(db)
    assert _enforce_full_exit_residual(db, rt, rec, amt=0.31, ratio=0.06) is False
    assert "full_exit_residual_at" not in rec.raw
    events = db.query(TradeEvent).filter_by(trade_record_id=rec.id).all()
    assert [e.event_type for e in events] == ["FULL_EXIT_RESIDUAL_FAILED"]


def test_large_residual_not_auto_closed(db):
    """残仓 >10% 疑似手动加仓：不自动平（reduceOnly 会越权平掉加仓），只告警一次"""
    rt = _FakeTrader()
    rec = _rec(db)
    assert _enforce_full_exit_residual(db, rt, rec, amt=1.0, ratio=0.5) is False
    assert rt.calls == []
    assert rec.raw["residual_skip_notified"] is True
    assert "full_exit_residual_at" not in rec.raw

    # 下一轮仍超阈值：不重复记事件/不复位标记
    _enforce_full_exit_residual(db, rt, rec, amt=1.0, ratio=0.5)
    events = db.query(TradeEvent).filter_by(trade_record_id=rec.id).all()
    assert [e.event_type for e in events] == ["FULL_EXIT_RESIDUAL_SKIPPED"]


def test_event_type_column_fits_all_event_names():
    """event_type 列宽必须容纳新事件名——PG 超长会抛 StringDataRightTruncation，
    整轮巡检回滚导致补平幂等标记丢失（sqlite 不校验长度，只能靠本断言兜）"""
    from app.models.trade import TradeEvent

    length = TradeEvent.__table__.c.event_type.type.length
    names = [
        "FULL_EXIT_RESIDUAL", "FULL_EXIT_RESIDUAL_FAILED",
        "FULL_EXIT_RESIDUAL_SKIPPED", "TP2_SKIPPED", "EXIT_HTF_STATE",
    ]
    assert length >= max(len(n) for n in names)
