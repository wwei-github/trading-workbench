# -*- coding: utf-8 -*-
"""满仓跳过判定按网拆分（2026-10-04 docs/06；2026-10-15 测试网不限持仓）：

Scanner._slots_full 用于定时扫描后决定是否跳过本轮 AI 分析。演进：
- 2026-10-04 前：「两网 OPENED 合计 ≥ max_open_trades」整体跳过
- 2026-10-04：按网判定，任一网 OPENED 数达上限即跳过
- 2026-10-15（用户拍板）：测试网不限持仓数（模拟环境多开观察），OPENED 数
  不参与满仓判定——仅正式网达上限才跳过本轮 AI 分析（正式网反正开不了仓）

覆盖：
1. 测试网单独达上限 → 不跳过（2026-10-15 新口径的关键用例）
2. 正式网单独达上限 → 跳过，明细文案含两网计数
3. 两网合计达上限但正式网未满 → 不跳过
4. 已保本单（TP1_HIT/TP2_HIT）不占名额；上限 0=不限
5. 配置行缺失/查询异常 → 按未满仓处理（不因统计故障漏掉分析）
"""
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services.scanner import Scanner  # noqa: E402


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：_slots_full 用独立 SessionLocal 连接，
    # 需共享同一内存库（test_adopt_account_scope 同款惯例）
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1, max_open_trades=10))
    session.commit()
    yield session, sessionmaker(bind=engine)
    session.close()


def _opened(db, testnet, status="OPENED"):
    db.add(TradeRecord(
        symbol=f"T{uuid.uuid4().hex[:6]}USDT", direction="long", status=status,
        account_key="testnet-1" if testnet else "live-1", testnet=testnet,
        opened_at=__import__("datetime").datetime.utcnow(), risk_amount=10.0,
    ))
    db.commit()


def _full(db_factory, monkeypatch):
    """_slots_full 走独立短会话：替换 scanner 命名空间里的 SessionLocal"""
    import app.services.scanner as sc
    monkeypatch.setattr(sc, "SessionLocal", db_factory)
    return Scanner._slots_full()


def test_testnet_full_alone_not_full(db, monkeypatch):
    """仅测试网达上限 → 不跳过（2026-10-15 测试网不限持仓数）"""
    session, factory = db
    for _ in range(10):
        _opened(session, True)
    _opened(session, False)
    full, detail = _full(factory, monkeypatch)
    assert full is False
    assert "测试网 10（不限）" in detail and "正式网 1/10" in detail


def test_live_full_alone(db, monkeypatch):
    """仅正式网达上限 → 跳过，文案指向正式网"""
    session, factory = db
    _opened(session, True)
    for _ in range(11):
        _opened(session, False)
    full, detail = _full(factory, monkeypatch)
    assert full is True
    assert "正式网在跑单达上限" in detail
    assert "正式网 11/10" in detail


def test_combined_full_but_live_not(db, monkeypatch):
    """两网合计 10（6+4）达上限但正式网未满 → 不跳过"""
    session, factory = db
    for _ in range(6):
        _opened(session, True)
    for _ in range(4):
        _opened(session, False)
    full, detail = _full(factory, monkeypatch)
    assert full is False
    assert "测试网 6（不限）" in detail and "正式网 4/10" in detail


def test_tp_hit_not_counted_and_zero_cap(db, monkeypatch):
    """已保本单不占名额（TP1_HIT 不计入）；上限 0=不限单数恒不跳过"""
    session, factory = db
    for _ in range(12):
        _opened(session, True, status="TP1_HIT")
    full, _ = _full(factory, monkeypatch)
    assert full is False
    cfg = session.get(SystemConfig, 1)
    cfg.max_open_trades = 0
    session.commit()
    for _ in range(10):
        _opened(session, True)
    full, detail = _full(factory, monkeypatch)
    assert full is False and detail == ""  # 上限 0=不限单数，恒不跳过


def test_missing_cfg_and_query_error(db, monkeypatch):
    """配置行缺失 / 查询异常 → (False, "")（按未满仓处理，不阻断分析）"""
    session, factory = db
    session.delete(session.get(SystemConfig, 1))
    session.commit()
    full, detail = _full(factory, monkeypatch)
    assert (full, detail) == (False, "")

    import app.services.scanner as sc

    class _Boom:
        def __init__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(sc, "SessionLocal", _Boom)
    full, detail = Scanner._slots_full()
    assert (full, detail) == (False, "")
