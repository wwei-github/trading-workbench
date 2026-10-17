# -*- coding: utf-8 -*-
"""开仓现价 RR 复算闸（2026-10-11-3 容差带 ai_rr_tol）

原 test_dual_entry_strategy.py 于 2026-10-12 策略二删除后裁剪重命名：
仅保留 s1（完整规则）相关回归——现价 RR 复算拦截 / 容差放行 / 页面覆盖；
s2 现价 RR 豁免用例随策略二退役删除（现价复算对所有单沿铁律生效）。
"""
import sys
from pathlib import Path
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models.system_config import SystemConfig
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


def _analysis(db):
    from app.models.scan import AIAnalysis
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction="long",
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=90, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _PenetrationTrader:
    """闸门穿透替身：前置闸门数据齐备，RR 复算之后第一个 trader 调用即哨兵报错。
    __getattr__ 兜底——RR 闸后（bracket_mmr）及以后任何方法调用均抛「替身终点」，
    断言该异常即证明现价 RR 复算闸已放行。price 可参数化（RR 容差带用例）。"""

    account_key = "testnet-1"
    testnet = True

    def __init__(self, price=100.5):
        self._price = price

    def listed_symbols(self):
        return {"TESTUSDT"}

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": 1000.0, "available": 900.0}

    def price(self, symbol):
        # 默认 entry=100 / sl=98 / tp1=104：现价 100.5 → rr_now = 3.5/2.5 = 1.4
        return self._price

    def __getattr__(self, name):
        raise RuntimeError("替身终点")


def test_s1_still_blocked_by_current_price_rr(db, monkeypatch):
    """现价复算 1.4 < 1.5 → 拦截，open_block_reason 记录死因（legacy 路径：PENDING_ENTRY_ENABLED=False）"""
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", False)
    a = _analysis(db)
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _PenetrationTrader(), a.id) == "blocked"
    assert "盈亏比按现价复算" in (a.open_block_reason or "")


def _set_rr_tol(db, value):
    """页面覆盖路径：写 system_config.ai_rr_tol（NULL 回落 env 默认 0.02）"""
    from app.models.system_config import SystemConfig
    cfg = db.get(SystemConfig, 1)
    cfg.ai_rr_tol = value
    db.commit()


def test_s1_rr_tol_passes(db):
    """容差带放行：默认 tol=0.02，现价 100.41 → rr=3.59/2.41≈1.49 落在
    [1.48, 1.5) → 放行穿透（CYBERUSDT 1.4998 被误杀案例的回归用例）"""
    a = _analysis(db)
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _PenetrationTrader(price=100.41), a.id) == "blocked"
    assert "替身终点" in (a.open_block_reason or "")


def test_s1_rr_tol_exceeded_blocks(db, monkeypatch):
    """容差带外仍拦：现价 100.5 → rr=1.4 < 1.48 → 拦截，文案含容差口径
    （legacy 路径：PENDING_ENTRY_ENABLED=False）"""
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", False)
    a = _analysis(db)
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _PenetrationTrader(price=100.5), a.id) == "blocked"
    reason = a.open_block_reason or ""
    assert "盈亏比按现价复算" in reason
    assert "容差" in reason


def test_s1_rr_tol_zero_strict(db, monkeypatch):
    """tol=0（页面显式严格）：退化为旧铁律，rr≈1.49 < 1.5 仍拦截
    （legacy 路径：PENDING_ENTRY_ENABLED=False）"""
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", False)
    _set_rr_tol(db, 0)
    a = _analysis(db)
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _PenetrationTrader(price=100.41), a.id) == "blocked"
    assert "盈亏比按现价复算" in (a.open_block_reason or "")
    assert "替身终点" not in (a.open_block_reason or "")


def test_s1_rr_tol_db_override(db):
    """页面覆盖生效：tol=0.11 → 门槛 1.39，rr=1.4 ≥ 1.39 放行穿透"""
    _set_rr_tol(db, 0.11)
    a = _analysis(db)
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _PenetrationTrader(price=100.5), a.id) == "blocked"
    assert "替身终点" in (a.open_block_reason or "")