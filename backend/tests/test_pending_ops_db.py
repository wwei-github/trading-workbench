"""_save_results 返回值 + pending confirm 落库回归测试（2026-09-28）

回归背景：_save_results 非空路径漏写 return，隐式返回 None →
_apply_pending_ops 的 confirm 分支 result_ids.get(symbol) 抛
AttributeError，confirm 处置整体回滚（"回踩 pending 处置落库失败"）。
scan_results / pullback_pendings 主键为 PG UUID 类型（sqlite
create_all 兼容但绑定为 hex 串），建表走 ORM metadata 子集即可。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.scanner as scanner_mod  # noqa: E402
from app.models.scan import PullbackPending, ScanResult  # noqa: E402
from app.services.scanner import Scanner  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(bind=engine, tables=[ScanResult.__table__, PullbackPending.__table__])

    # _apply_pending_ops 内部自建 SessionLocal（生产绑定真实库），
    # 测试改绑内存 sqlite（同 engine 单连接池，跨 session 可见）
    monkeypatch.setattr(
        scanner_mod, "SessionLocal",
        lambda: Session(bind=engine),
    )
    return Session(bind=engine)


HIT = {
    "signal_type": "trigger_candlestick",
    "current_price": 100.0,
    "breakout_pct": 0.5,
    "trend_slope": 0.0,
    "r_squared": 0.0,
    "pattern": "hammer",
    "signal_reason": "test",
    "strength": 0.8,
    "position": "support",
    "quality_score": 70,
}


def test_save_results_returns_symbol_id_map(db):
    """有命中时必须返回 {symbol: id}（回归：此前隐式返回 None）"""
    record_id = uuid.uuid4()
    result_ids = Scanner._save_results(record_id, [{"symbol": "BTC/USDT", **HIT}])
    assert set(result_ids) == {"BTC/USDT"}
    rid = result_ids["BTC/USDT"]
    assert isinstance(rid, uuid.UUID)
    # 已真实落库
    row = db.execute(select(ScanResult).where(ScanResult.symbol == "BTC/USDT")).scalars().one()
    assert row.id == rid and row.scan_record_id == record_id


def test_save_results_empty_returns_empty_dict():
    assert Scanner._save_results(uuid.uuid4(), []) == {}


def test_confirm_op_persists_with_scan_result_id(db):
    """confirm 处置：pending 落终态 confirmed 且回写 scan_result_id（不再回滚）"""
    sym = "ETH/USDT"
    db.add(PullbackPending(symbol=sym, direction=1, stage1_candle_open=datetime.utcnow()))
    db.commit()

    result_ids = Scanner._save_results(uuid.uuid4(), [{"symbol": sym, **HIT}])
    Scanner._apply_pending_ops(
        {sym: {"op": "confirm", "confirm_pattern": "hammer"}}, result_ids
    )

    row = db.execute(select(PullbackPending).where(PullbackPending.symbol == sym)).scalars().one()
    assert row.status == "confirmed"
    assert row.confirm_pattern == "hammer"
    assert row.confirmed_scan_result_id == result_ids[sym]
    assert row.resolved_at is not None
