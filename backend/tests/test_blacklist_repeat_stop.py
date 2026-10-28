# -*- coding: utf-8 -*-
"""黑名单「重复止损」自动档（docs/16 §13，2026-10-27；2026-10-28 阈值 2→3 次）。

与既有的「超预算」两档本质不同：**不看倍数，只看次数**。
依据（14 天正式网 151 笔亏损单）：实亏超计划风险的最大值只有 1.33R，
`AUTO_RISK_OVERSHOOT=1.5` 一次都没触发过（现有 9 条黑名单全部来自
10-20 修仓位算法之前的巨风险单）；真正在流血的是同一币反复止损——
ICNT 4 次 -12.28、LIGHT 3 次 -8.65、DUSK 3 次 -7.54、SU 3 次 -7.05，
每一次都在正常 -1R 以内。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.blacklist import SymbolBlacklist  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services.blacklist_service import register_settle, _repeat_stopouts  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine, tables=[SymbolBlacklist.__table__, TradeRecord.__table__])
    session = Session(bind=engine)
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _repeat_enabled(monkeypatch):
    """默认对每个用例显式打开重复档，边界用例自己再覆盖。"""
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_STOP_N", 3, raising=False)
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_STOP_DAYS", 14, raising=False)
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_COOLDOWN_DAYS", 14, raising=False)


def _row(db, symbol):
    return db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()


def _prior_stop(db, symbol, days_ago=1, pnl=-3.0, exit_reason="sl", testnet=False):
    db.add(TradeRecord(
        symbol=symbol, direction="long", testnet=testnet,
        realized_pnl=pnl, exit_reason=exit_reason,
        closed_at=datetime.utcnow() - timedelta(days=days_ago),
    ))
    db.commit()


def test_repeat_disabled_by_default(db, monkeypatch):
    """N=0（默认）→ 正常 1R 止损不拉黑。"""
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_STOP_N", 0, raising=False)
    _prior_stop(db, "AAAUSDT")
    register_settle(db, "AAAUSDT", 3.0, -3.0, "sl")
    db.commit()
    assert _row(db, "AAAUSDT") is None


def test_repeat_triggers_at_threshold(db):
    """窗口内已 2 次 + 本次 = 3 次 ≥ N=3 → 冷却档，理由含次数。"""
    _prior_stop(db, "BBBUSDT")
    _prior_stop(db, "BBBUSDT", days_ago=3)
    register_settle(db, "BBBUSDT", 3.0, -3.0, "sl")
    db.commit()
    r = _row(db, "BBBUSDT")
    assert r is not None and r.origin == "auto" and r.expires_at is not None
    assert "第 3 次止损出场" in r.reason
    days = (r.expires_at - datetime.utcnow()).total_seconds() / 86400
    assert 13.9 < days <= 14.0


def test_repeat_below_threshold(db, monkeypatch):
    """N=3、窗口内只有 1 次 → 不拉黑。"""
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_STOP_N", 3, raising=False)
    _prior_stop(db, "CCCUSDT")
    register_settle(db, "CCCUSDT", 3.0, -3.0, "sl")
    db.commit()
    assert _row(db, "CCCUSDT") is None


def test_repeat_outside_window_ignored(db):
    """窗口外的旧止损不计数（30 天前 vs 窗口 14 天）。"""
    _prior_stop(db, "DDDUSDT", days_ago=30)
    register_settle(db, "DDDUSDT", 3.0, -3.0, "sl")
    db.commit()
    assert _row(db, "DDDUSDT") is None


def test_repeat_counts_only_stop_exits(db, monkeypatch):
    """manual / tp1_trail 等出场不计入止损次数。"""
    monkeypatch.setattr(settings, "BLACKLIST_REPEAT_STOP_N", 2, raising=False)
    _prior_stop(db, "EEEUSDT", exit_reason="manual")
    register_settle(db, "EEEUSDT", 3.0, -3.0, "sl")
    db.commit()
    assert _row(db, "EEEUSDT") is None


def test_repeat_ignores_testnet_rows(db):
    """测试网止损不参与正式网币种的拉黑判定。"""
    _prior_stop(db, "FFFUSDT", testnet=True)
    register_settle(db, "FFFUSDT", 3.0, -3.0, "sl")
    db.commit()
    assert _row(db, "FFFUSDT") is None


def test_repeat_does_not_downgrade_manual(db):
    """已手动永久拉黑 → 重复档不覆盖 origin，也不动 expires_at。"""
    db.add(SymbolBlacklist(symbol="GGGUSDT", origin="manual", reason="人工"))
    db.commit()
    _prior_stop(db, "GGGUSDT")
    _prior_stop(db, "GGGUSDT", days_ago=2)
    register_settle(db, "GGGUSDT", 3.0, -3.0, "sl")
    db.commit()
    r = _row(db, "GGGUSDT")
    assert r.origin == "manual" and r.expires_at is None and r.reason == "人工"


def test_repeat_does_not_downgrade_stop_hard(db):
    """已自动永久档（stop_hard）→ 重复档不降级为 auto，expires_at 仍 NULL"""
    db.add(SymbolBlacklist(symbol="KKKUSDT", origin="stop_hard",
                           reason="永久", expires_at=None))
    db.commit()
    _prior_stop(db, "KKKUSDT")
    _prior_stop(db, "KKKUSDT", days_ago=2)
    register_settle(db, "KKKUSDT", 3.0, -3.0, "sl")
    db.commit()
    r = _row(db, "KKKUSDT")
    assert r.origin == "stop_hard" and r.expires_at is None and r.reason == "永久"


def test_overshoot_path_still_takes_precedence(db):
    """-2.0R：走超预算档 → 永久档（不是重复档的冷却档）。"""
    _prior_stop(db, "HHHUSDT")
    register_settle(db, "HHHUSDT", 3.0, -6.0, "sl")
    db.commit()
    r = _row(db, "HHHUSDT")
    assert r.origin == "stop_hard" and r.expires_at is None


def test_repeat_helper_counts_window_only(db):
    """底层计数：窗口内正式网止损计数，盈利单/非止损出场不算。"""
    _prior_stop(db, "IIIUSDT", days_ago=1)
    _prior_stop(db, "IIIUSDT", days_ago=3)
    _prior_stop(db, "IIIUSDT", days_ago=20)          # 窗口外
    _prior_stop(db, "IIIUSDT", days_ago=2, pnl=5.0)  # 盈利
    _prior_stop(db, "IIIUSDT", days_ago=2, exit_reason="manual")
    _prior_stop(db, "IIIUSDT", days_ago=2, testnet=True)
    assert _repeat_stopouts(db, "IIIUSDT", 14) == 2


def test_repeat_excludes_current_trade(db):
    """结算路径上本次这笔已落库 → 必须靠 trade_id 排除，否则阈值会提前一格。

    模拟真实调用：本次 TradeRecord 已写 realized_pnl/closed_at（autoflush 可见），
    窗口内另有 2 笔历史止损，N=3 → 恰好第 3 次，应触发。
    """
    cur = TradeRecord(
        symbol="JJJUSDT", direction="long", testnet=False,
        realized_pnl=-3.0, exit_reason="sl", closed_at=datetime.utcnow(),
    )
    db.add(cur)
    db.commit()
    _prior_stop(db, "JJJUSDT")
    _prior_stop(db, "JJJUSDT", days_ago=2)

    from app.services.blacklist_service import _repeat_stopouts
    # 不排除 → 3（含本次）；排除 → 2
    assert _repeat_stopouts(db, "JJJUSDT", 14) == 3
    assert _repeat_stopouts(db, "JJJUSDT", 14, exclude_trade_id=cur.id) == 2

    register_settle(db, "JJJUSDT", 3.0, -3.0, "sl", trade_id=cur.id)
    db.commit()
    r = _row(db, "JJJUSDT")
    assert r is not None and "第 3 次止损出场" in r.reason
