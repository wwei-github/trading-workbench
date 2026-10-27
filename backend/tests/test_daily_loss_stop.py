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
    " excluded BOOLEAN,"
    " realized_pnl FLOAT)"
)


def _row(account_key, testnet, status, closed_at, pnl, excluded=False):
    ak = "NULL" if account_key is None else f"'{account_key}'"
    ex = 1 if excluded else 0
    return (
        f"INSERT INTO trade_records "
        f"(id, account_key, testnet, status, closed_at, excluded, realized_pnl) "
        f"VALUES ('{uuid.uuid4()}', {ak}, {int(testnet)}, '{status}', "
        f"'{closed_at.isoformat(sep=' ')}', {ex}, {pnl})"
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
    assert _daily_loss_cap(65.0) == pytest.approx(5.85)     # <100 取本金 65（旧口径抬到 100 → 9.0，占权益 13.8%）
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


def test_excluded_trade_not_counted(db):
    """异常单标记（2026-10-18）：excluded=true 的已平仓单不计入当日亏损熔断口径。

    模拟测试网伪巨亏单（模拟撮合 -2330.51）标记后被熔断闸门彻底剔除——
    即使数量级远超熔断阈值也不触发。"""
    base = _daily_closed_pnl(db, "live-1", False)
    db.execute(text(_row(
        "live-1", False, "CLOSED", datetime.utcnow(), -2330.51, excluded=True)))
    db.commit()
    assert _daily_closed_pnl(db, "live-1", False) == pytest.approx(base)


# ── breaker_status_by_net（2026-10-04 顶部卡片「熔断状态」列） ─────────


class _FakeTrader:
    """按账号返回预设钱包，免打币安"""
    wallets: dict = {}

    def __init__(self, account):
        self.account = account

    def wallet_balance(self):
        key = self.account if isinstance(self.account, str) else "?"
        return {"wallet": _FakeTrader.wallets.get(key, 500.0), "available": 0.0}


def _setup_breaker(monkeypatch, accounts):
    from types import SimpleNamespace
    import app.services.account_registry as reg
    import app.services.binance_trader as bt
    import app.services.trade_engine as te

    cfg = SimpleNamespace(
        daily_loss_stop_enabled=True, daily_loss_stop_pct=9,
        weekly_dd_stop_pct=None, monthly_dd_stop_pct=None,  # 分层默认关（本文件只测日层）
        monthly_breaker_ack_at=None, weekly_breaker_ack_at=None,
    )
    monkeypatch.setattr(reg, "managed_accounts", lambda db: accounts)
    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)
    te._wallet_cache.clear()
    return cfg


def test_breaker_status_by_net_states(db, monkeypatch):
    """live 亏 -25（阈值 45=基数 500×9%）未熔断；补亏至 -55 → active=True；
    testnet 无平仓 → 正常。钱包走 fake，不触网"""
    from types import SimpleNamespace
    from app.services.trade_engine import breaker_status_by_net

    accounts = [
        SimpleNamespace(key="live-1", testnet=False),
        SimpleNamespace(key="testnet-1", testnet=True),
    ]
    _FakeTrader.wallets = {"live-1": 500.0, "testnet-1": 500.0}
    cfg = _setup_breaker(monkeypatch, accounts)

    out = breaker_status_by_net(db, cfg)
    # week/month=None：本文件 cfg 分层阈值 None→回落 env，但 env 15/25>0 会算出
    # week/month 状态——精确断言日层字段 + 分层键存在性（分层行为在
    # test_tiered_breaker_atr_trail.py 专测）
    assert out["live"]["enabled"] is True
    assert out["live"]["active"] is False
    assert out["live"]["day_pnl"] == -25.0
    assert out["live"]["cap"] == 45.0
    # testnet 账号吃夹具里 NULL/testnet 回落行 -77 ≤ -45 → 熔断（回落语义与开仓闸门同源）
    assert out["testnet"]["active"] is True and out["testnet"]["day_pnl"] == -77.0

    db.execute(text(_row("live-1", False, "CLOSED", datetime.utcnow(), -30.0)))
    db.commit()
    out = breaker_status_by_net(db, cfg)
    assert out["live"]["active"] is True and out["live"]["day_pnl"] == -55.0


def test_breaker_status_disabled_and_wallet_fail(db, monkeypatch):
    """开关关 → enabled=False / active=None；钱包查询失败且无缓存 → active=None
    （未知态），不影响 day_pnl 计算"""
    from types import SimpleNamespace
    from app.services.trade_engine import breaker_status_by_net

    accounts = [SimpleNamespace(key="live-1", testnet=False)]
    cfg = _setup_breaker(monkeypatch, accounts)
    cfg.daily_loss_stop_enabled = False
    out = breaker_status_by_net(db, cfg)
    assert out["live"]["enabled"] is False and out["live"]["active"] is None

    cfg.daily_loss_stop_enabled = True
    import app.services.trade_engine as te  # noqa: F811  函数内用 _wallet_cache
    te._wallet_cache.clear()  # 上分支 enabled=False 也会取 wallet（分层需要），缓存须清

    class _Boom:
        def __init__(self, account):
            pass

        def wallet_balance(self):
            raise RuntimeError("network down")

    import app.services.binance_trader as bt
    monkeypatch.setattr(bt, "BinanceTrader", _Boom)
    out = breaker_status_by_net(db, cfg)
    assert out["live"]["active"] is None and out["live"]["day_pnl"] == -25.0


def test_breaker_status_multi_account_min_cap(db, monkeypatch):
    """同网多账号：day_pnl 合计、cap 取最小（最保守）、任一口径触发即熔断；
    该网无管理账号 → enabled=False"""
    from types import SimpleNamespace
    from app.services.trade_engine import breaker_status_by_net

    accounts = [
        SimpleNamespace(key="live-1", testnet=False),
        SimpleNamespace(key="live-2", testnet=False),
    ]
    _FakeTrader.wallets = {"live-1": 500.0, "live-2": 1000.0}  # cap 45 / 90
    cfg = _setup_breaker(monkeypatch, accounts)

    out = breaker_status_by_net(db, cfg)
    assert out["live"]["day_pnl"] == pytest.approx(-25.0 + -7.0)
    assert out["live"]["cap"] == 45.0
    assert out["live"]["active"] is False

    no_acc = breaker_status_by_net(db, cfg)  # 无账号网
    assert no_acc["testnet"]["enabled"] is False and no_acc["testnet"]["active"] is None
