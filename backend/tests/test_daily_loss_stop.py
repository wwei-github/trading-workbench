"""当日亏损熔断单测（2026-09-27 正式网复盘 C 项）

trade_records 主键为 PG 专用 UUID 类型（sqlite create_all 不兼容），
按被查列手工建表后走 ORM 查询验证 _daily_closed_pnl 的口径：
账号匹配（NULL 按环境回落）/ status=CLOSED / UTC 日界。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.trade_engine import _daily_closed_pnl, _daily_loss_cap  # noqa: E402

_DDL = (
    "CREATE TABLE trade_records ("
    " id VARCHAR(40) PRIMARY KEY,"
    " account_key VARCHAR(20),"
    " testnet BOOLEAN,"
    " status VARCHAR(16),"
    " closed_at TIMESTAMP,"
    " realized_pnl FLOAT)"
)


def _row(account_key, testnet, status, closed_at, pnl):
    ak = "NULL" if account_key is None else f"'{account_key}'"
    return (
        f"INSERT INTO trade_records (id, account_key, testnet, status, closed_at, realized_pnl) "
        f"VALUES ('{uuid.uuid4()}', {ak}, {int(testnet)}, '{status}', "
        f"'{closed_at.isoformat(sep=' ')}', {pnl})"
    )


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)
    with engine.connect() as conn:
        conn.execute(text(_DDL))
        for stmt in (
            _row("live-1", False, "CLOSED", now, -20.0),      # 计入
            _row("live-1", False, "CLOSED", now, 5.0),        # 计入（盈利抵扣）
            _row("live-2", False, "CLOSED", now, 3.0),        # 排除：他账号（隔离验证）
            _row("live-1", False, "OPENED", now, -50.0),      # 排除：未平仓
            _row("live-1", False, "CLOSED", yesterday, -99.0),  # 排除：昨日
            _row(None, False, "CLOSED", now, -10.0),          # 计入：NULL 按 live 回落
            _row(None, True, "CLOSED", now, -77.0),           # 排除：NULL 测试网行
        ):
            conn.execute(text(stmt))
        conn.commit()
    yield Session(bind=engine)


def test_sum_account_scope_and_day_boundary(db):
    """账号过滤 + 环境回落 + 状态过滤 + UTC 日界：-20 + 5 - 10 = -25"""
    assert _daily_closed_pnl(db, "live-1", False) == pytest.approx(-25.0)


def test_fuse_triggers_at_threshold(db, monkeypatch):
    """净亏 ≤ -熔断阈值判熔断；补一笔 -10 后 -35 达到 -30 阈值触发
   （阈值 30 = 风险基数 500 × 6%，monkeypatch PCT 构造）"""
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_PCT", 6.0)
    cap = _daily_loss_cap(500.0)  # 基数 500 × 6% = 30
    assert cap == pytest.approx(30.0)
    assert _daily_closed_pnl(db, "live-1", False) > -cap
    db.execute(text(_row("live-1", False, "CLOSED", datetime.utcnow(), -10.0)))
    db.commit()
    assert _daily_closed_pnl(db, "live-1", False) <= -cap


def test_loss_cap_scales_with_wallet():
    """熔断阈值按账户金额动态缩放（2026-09-29 由写死 30 USDT 改 PCT 口径）"""
    assert _daily_loss_cap(1000.0) == pytest.approx(90.0)   # 基数 1000 × 9%
    assert _daily_loss_cap(2000.0) == pytest.approx(180.0)  # 基数 2000 × 9%
    assert _daily_loss_cap(5000.0) == pytest.approx(450.0)  # 基数 5000 封顶 × 9%
    assert _daily_loss_cap(5200.0) == pytest.approx(450.0)  # 超上限回落基数 5000
    assert _daily_loss_cap(65.0) == pytest.approx(9.0)      # 低于下限回落基数 100
    assert _daily_loss_cap(4800.0) == pytest.approx(360.0)  # 首位取整 4000 × 9%
    assert _daily_loss_cap(0.0) == pytest.approx(0.0)


def test_other_account_not_fused(db, monkeypatch):
    """熔断按账号隔离：live-2 不含 live-1 的亏损，仅自身 +3 与 NULL 回落 -10 = -7，
    未达熔断阈值"""
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_PCT", 6.0)
    cap = _daily_loss_cap(500.0)
    assert _daily_closed_pnl(db, "live-2", False) == pytest.approx(-7.0)
    assert _daily_closed_pnl(db, "live-2", False) > -cap


def test_other_env_not_matched(db):
    """测试网账号（live 口径 NULL 行不回落）：仅 testnet=true 的 NULL 行 -77"""
    assert _daily_closed_pnl(db, "any-live-missing", True) == pytest.approx(-77.0)
