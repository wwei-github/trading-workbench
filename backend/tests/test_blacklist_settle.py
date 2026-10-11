# -*- coding: utf-8 -*-
"""黑名单止损分档回归（2026-10-11 用户拍板）：实亏 ≥1.5R → 永久档（stop_hard，
只能手动移出）；1.2R≤实亏<1.5R → 冷却档（auto，30 天）维持原口径。

register_settle 挂在平仓结算路径（trade_engine 结算后调用），分档边界直接
决定线上拉黑行为，纯函数边界用真实 Session 驱动（SQLite 内存表）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.blacklist import SymbolBlacklist  # noqa: E402
from app.services import blacklist_service as bs  # noqa: E402
from app.services.blacklist_service import register_settle, active_symbols  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[SymbolBlacklist.__table__])
    session = Session(bind=engine)
    yield session
    session.close()


def _row(db, symbol):
    return db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()


def test_stop_loss_15r_permanent(db):
    """实亏 =1.5R → stop_hard 永久档，expires_at=None，active 生效"""
    register_settle(db, "AAAUSDT", planned_risk=10.0, realized_pnl=-15.0,
                    exit_reason="stop_loss")
    db.commit()
    r = _row(db, "AAAUSDT")
    assert r is not None and r.origin == "stop_hard" and r.expires_at is None
    assert "永久" in r.reason
    assert active_symbols(db) == {"AAAUSDT"}


def test_stop_loss_12r_cooldown(db):
    """实亏 1.2R~1.5R 区间 → 冷却档（auto，30 天）维持原口径"""
    register_settle(db, "BBBUSDT", planned_risk=10.0, realized_pnl=-13.0,
                    exit_reason="stop_loss")
    db.commit()
    r = _row(db, "BBBUSDT")
    assert r is not None and r.origin == "auto" and r.expires_at is not None
    assert "冷却" in r.reason
    assert active_symbols(db) == {"BBBUSDT"}


def test_boundary_15r_exclusive_tiers(db):
    """边界：恰好 -1.5R 落永久档；-1.49R 落冷却档（≥ 判定）"""
    register_settle(db, "CCCUSDT", 10.0, -15.0, "stop_loss")
    register_settle(db, "DDCUSDT", 10.0, -14.9, "stop_loss")
    db.commit()
    assert _row(db, "CCCUSDT").origin == "stop_hard"
    assert _row(db, "DDCUSDT").origin == "auto"


def test_liquidation_tiered_by_overshoot(db):
    """强平无条件触发，档位仍按倍数：<1.5R 冷却、≥1.5R 永久"""
    register_settle(db, "EEEUSDT", 10.0, -12.0, "liquidation")
    register_settle(db, "FFFUSDT", 10.0, -30.0, "liquidation")
    db.commit()
    assert _row(db, "EEEUSDT").origin == "auto"
    assert _row(db, "FFFUSDT").origin == "stop_hard"


def test_below_12r_no_blacklist(db):
    """实亏 <1.2R 且非强平 → 不拉黑"""
    register_settle(db, "GGGUSDT", 10.0, -11.9, "stop_loss")
    db.commit()
    assert _row(db, "GGGUSDT") is None and active_symbols(db) == set()


def test_upgrade_cooldown_to_permanent(db):
    """冷却档行再触 ≥1.5R → 升级永久档（expires_at 清空）"""
    register_settle(db, "HHHUSDT", 10.0, -13.0, "stop_loss")
    db.commit()
    assert _row(db, "HHHUSDT").origin == "auto"
    register_settle(db, "HHHUSDT", 10.0, -20.0, "stop_loss")
    db.commit()
    r = _row(db, "HHHUSDT")
    assert r.origin == "stop_hard" and r.expires_at is None
    assert active_symbols(db) == {"HHHUSDT"}


def test_manual_row_not_downgraded(db):
    """manual 永久行触发自动登记 → 保持 manual 不被覆盖降级"""
    bs.add_manual(db, "IIIUSDT", reason="手动")
    db.commit()
    register_settle(db, "IIIUSDT", 10.0, -15.0, "stop_loss")
    db.commit()
    r = _row(db, "IIIUSDT")
    assert r.origin == "manual" and r.expires_at is None


def test_expired_cooldown_not_active_but_stop_hard_is(db):
    """冷却档到期自动失效、stop_hard 恒生效——active_symbols 过滤口径"""
    from datetime import datetime, timedelta
    db.add(SymbolBlacklist(symbol="JJJUSDT", origin="auto",
                           expires_at=datetime.utcnow() - timedelta(days=1)))
    db.add(SymbolBlacklist(symbol="KKKUSDT", origin="stop_hard", expires_at=None))
    db.commit()
    assert active_symbols(db) == {"KKKUSDT"}


def test_disabled_row_excluded(db):
    """disabled 行不生效（manual/stop_hard 同口径）"""
    bs.add_manual(db, "LLLUSDT")
    db.commit()
    row = _row(db, "LLLUSDT")
    row.disabled = True
    db.commit()
    assert active_symbols(db) == set()
