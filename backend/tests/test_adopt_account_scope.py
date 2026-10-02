# -*- coding: utf-8 -*-
"""手动单接管账号范围（2026-10-02 修复：管理行为不随当前环境切换中断）

修复前：巡检任务接管/孤儿清扫循环遍历 enabled_accounts（当前环境清单）——
当前环境测试网时循环体空转，正式网 App 手动单无人接管、不进库不巡检，
止盈止损/保本移损/跟进止损全部失管。

修复后：接管/清扫按 live_enabled_accounts（正式网启用账号）独立成清单，
与当前环境无关；开单扇出（enabled_accounts）仍按当前环境过滤。

覆盖：
1. live_enabled_accounts 与 enabled_accounts 的语义分野（测试网模式：
   开单清单=测试网账号，管理清单=正式网账号）
2. 巡检任务端到端（替身注入）：测试网模式下正式网账号的 auto_adopt +
   孤儿清扫照常执行；测试网主账号不接管
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.services import account_registry, trade_engine  # noqa: E402
from app.services.account_registry import Account  # noqa: E402
from app.tasks import trade_tasks  # noqa: E402


_NET = Account("testnet-1", "测试网", True, "tk", "ts")
_LIVE = Account("live-1", "正式网-1", False, "lk", "ls")


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在独立线程跑请求，
    # 共享同一内存库连接（test_risk_params_api 同款惯例）
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1, trading_enabled=True, trading_testnet=True,
                             live_trading_keys=None))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def both_accounts(monkeypatch):
    """注册表：测试网 + 正式网各一（冻结 dataclass 直接构造）"""
    monkeypatch.setattr(account_registry, "_registry", [_NET, _LIVE])


# ── 1. 账号清单语义 ──


def test_live_enabled_accounts_independent_of_env(db, both_accounts):
    """测试网模式：开单清单=测试网账号；管理清单=正式网账号"""
    assert [a.key for a in account_registry.enabled_accounts(db)] == ["testnet-1"]
    assert [a.key for a in account_registry.live_enabled_accounts(db)] == ["live-1"]


def test_live_enabled_accounts_same_as_enabled_in_live_mode(db, both_accounts):
    """正式网模式：两清单一致（enabled_accounts 正式网分支复用同一实现）"""
    db.get(SystemConfig, 1).trading_testnet = False
    db.commit()
    assert [a.key for a in account_registry.enabled_accounts(db)] == ["live-1"]
    assert [a.key for a in account_registry.live_enabled_accounts(db)] == ["live-1"]


# ── 2. 巡检任务端到端（替身注入，不触网）──


class _FakeTrader:
    """BinanceTrader 替身：按账号 key 构造，仅暴露巡检路径用到的成员"""
    configured = True
    testnet = False

    def __init__(self, key=None):
        self.account_key = key
        self.testnet = key == "testnet-1"

    def positions(self):
        return {}


def _run_task(db, monkeypatch, adopt_calls, sweep_calls):
    """run_auto_trade_task 全替身环境；记录 adopt/sweep 调用的账号"""
    engine = db.bind
    monkeypatch.setattr(trade_tasks, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(trade_tasks, "BinanceTrader", _FakeTrader)
    monkeypatch.setattr(trade_tasks, "_acquire_open_lock", lambda *a, **k: True)
    monkeypatch.setattr(trade_tasks, "_release_open_lock", lambda *a, **k: None)
    monkeypatch.setattr(trade_tasks, "settle_trades", lambda db, t: (0, []))
    monkeypatch.setattr("app.services.task_log.open_task", lambda **kw: None)
    monkeypatch.setattr("app.services.task_log.close_task", lambda *a, **k: None)
    monkeypatch.setattr(trade_engine, "check_hold_warnings", lambda db: [])
    monkeypatch.setattr(trade_engine, "sync_account_transfers", lambda db: 0)

    def _fake_adopt(db, trader):
        adopt_calls.append(trader.account_key)
        return []

    def _fake_sweep(trader, run_syms, positions):
        sweep_calls.append(trader.account_key)

    monkeypatch.setattr(trade_engine, "auto_adopt_positions", _fake_adopt)
    monkeypatch.setattr(trade_engine, "_sweep_orphan_algo_orders", _fake_sweep)

    class _FakeRedis:
        @staticmethod
        def from_url(*a, **k):
            return None

    monkeypatch.setattr(trade_tasks.Redis, "from_url",
                        lambda *a, **k: _FakeRedis())
    trade_tasks.run_auto_trade_task()


def test_task_adopts_both_nets_in_testnet_mode(db, both_accounts, monkeypatch):
    """当前环境测试网：主账号 testnet-1 + 正式网账号都接管（2026-10-03 拍板：
    测试网环境两网手动单都导入，取代 09-29 "测试网不接管"写死）；孤儿清扫
    仍只扫正式网账号（循环体，testnet 主账号不扫）"""
    adopt_calls, sweep_calls = [], []
    _run_task(db, monkeypatch, adopt_calls, sweep_calls)
    assert adopt_calls == ["testnet-1", "live-1"]
    assert sweep_calls == ["live-1"]


def test_task_adopts_via_main_account_in_live_mode(db, both_accounts, monkeypatch):
    """当前环境正式网：主账号接管走无条件分支，循环不重复调用"""
    db.get(SystemConfig, 1).trading_testnet = False
    db.commit()
    adopt_calls, sweep_calls = [], []
    _run_task(db, monkeypatch, adopt_calls, sweep_calls)
    # 主账号 live-1 由上方分支接管；adopt_accs 循环跳过主账号 → 不重复
    assert adopt_calls == ["live-1"]
    assert sweep_calls == []  # 唯一正式网账号即主账号，循环体无其余账号


# ── 3. adopt-now 端点账号范围（2026-10-03 与巡检口径对齐）──


class _FakeApiTrader:
    configured = True

    def __init__(self, key=None):
        self.account_key = key


def _run_adopt_now(db, monkeypatch, adopt_calls, lock_calls=None):
    """adopt-now 端点全替身环境（TestClient + sqlite override，不触网）"""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    monkeypatch.setattr(
        trade_tasks, "_acquire_open_lock",
        lambda *a, **k: (lock_calls.append(k.get("account")) if lock_calls is not None else None) or True,
    )
    monkeypatch.setattr(trade_tasks, "_release_open_lock", lambda *a, **k: None)

    import redis as _redis
    monkeypatch.setattr(_redis.Redis, "from_url", lambda *a, **k: None, raising=False)

    from app.services import binance_trader
    monkeypatch.setattr(binance_trader, "BinanceTrader", _FakeApiTrader)
    monkeypatch.setattr(trade_engine, "auto_adopt_positions",
                        lambda db, trader: adopt_calls.append(trader.account_key) or [])

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        client = TestClient(app)
        resp = client.post("/api/trades/adopt-now")
        assert resp.status_code == 200, resp.text
        return resp.json()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_adopt_now_testnet_mode_both_nets(db, both_accounts, monkeypatch):
    """测试网环境点导入：testnet-1 + live-1 两账号都执行"""
    adopt_calls = []
    data = _run_adopt_now(db, monkeypatch, adopt_calls)
    assert adopt_calls == ["testnet-1", "live-1"]
    assert data["accounts"] == ["testnet-1", "live-1"]


def test_adopt_now_live_mode_live_only(db, both_accounts, monkeypatch):
    """正式网环境点导入：仅 live-1（不回落测试网账号）"""
    db.get(SystemConfig, 1).trading_testnet = False
    db.commit()
    adopt_calls = []
    data = _run_adopt_now(db, monkeypatch, adopt_calls)
    assert adopt_calls == ["live-1"]
    assert data["accounts"] == ["live-1"]
