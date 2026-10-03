# -*- coding: utf-8 -*-
"""手动单接管/巡检/导入持仓的账号范围（2026-10-03 删环境切换：管理行为恒两网）

删环境切换后无全局「当前环境」概念：
- 管理行为（手动单接管/导入持仓/结算巡检/孤儿清扫）一律走 managed_accounts
  = 测试网账号 + 正式网启用账号，与任何开关无关——账号是否存在手动单由
  交易所持仓查询决定，管理范围不随开关变化
- 自动开单走 auto_trade_accounts = 双网独立开关扇出（互不回落）

覆盖：
1. managed_accounts 组成（测试网 + 正式网启用账号，去重保持顺序），
   且不随双开关状态变化
2. auto_trade_accounts 双开关扇出（全开/默认/仅正式网/全关）
3. 巡检任务端到端（替身注入）：双网账号接管 + 孤儿清扫仅正式网账号
4. adopt-now 端点：双网账号全部执行
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
    session.add(SystemConfig(id=1, trading_enabled_testnet=True,
                             trading_enabled_live=False, live_trading_keys=None))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def both_accounts(monkeypatch):
    """注册表：测试网 + 正式网各一（冻结 dataclass 直接构造）"""
    monkeypatch.setattr(account_registry, "_registry", [_NET, _LIVE])


# ── 1. 管理清单（接管/巡检/导入持仓）──


def test_managed_accounts_composition(db, both_accounts):
    """管理清单 = 测试网账号 + 正式网启用账号（keys 空回落 live-1）"""
    assert [a.key for a in account_registry.managed_accounts(db)] == ["testnet-1", "live-1"]


def test_managed_accounts_ignores_switches(db, both_accounts):
    """管理行为不随自动交易开关变化：双开关全关，接管/巡检范围仍是两网"""
    cfg = db.get(SystemConfig, 1)
    cfg.trading_enabled_testnet = False
    cfg.trading_enabled_live = False
    db.commit()
    assert [a.key for a in account_registry.managed_accounts(db)] == ["testnet-1", "live-1"]


# ── 1.5 自动开单扇出清单（双网独立开关，互不回落）──


def test_auto_trade_accounts_both_switches_on(db, both_accounts):
    """双开关全开：测试网 + 正式网账号都在扇出清单"""
    cfg = db.get(SystemConfig, 1)
    cfg.trading_enabled_testnet = True
    cfg.trading_enabled_live = True
    db.commit()
    assert [a.key for a in account_registry.auto_trade_accounts(db)] == ["testnet-1", "live-1"]


def test_auto_trade_accounts_defaults(db, both_accounts):
    """默认（测试网开/正式网关）：仅测试网账号——正式网安全基线"""
    assert [a.key for a in account_registry.auto_trade_accounts(db)] == ["testnet-1"]


def test_auto_trade_accounts_live_only(db, both_accounts):
    """仅正式网开：清单只有 live 账号，测试网开关关时绝不回落测试网"""
    cfg = db.get(SystemConfig, 1)
    cfg.trading_enabled_testnet = False
    cfg.trading_enabled_live = True
    db.commit()
    assert [a.key for a in account_registry.auto_trade_accounts(db)] == ["live-1"]


def test_auto_trade_accounts_all_off(db, both_accounts):
    """全关：空清单（open_trade_for_analysis 侧据此跳过）"""
    cfg = db.get(SystemConfig, 1)
    cfg.trading_enabled_testnet = False
    cfg.trading_enabled_live = False
    db.commit()
    assert account_registry.auto_trade_accounts(db) == []


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


def test_task_adopts_and_sweeps_both_nets(db, both_accounts, monkeypatch):
    """巡检恒两网（2026-10-03 删环境切换）：主账号 testnet-1 无条件接管 +
    循环体接管 live-1；孤儿清扫仅正式网账号（测试网主账号不扫）"""
    adopt_calls, sweep_calls = [], []
    _run_task(db, monkeypatch, adopt_calls, sweep_calls)
    assert adopt_calls == ["testnet-1", "live-1"]
    assert sweep_calls == ["live-1"]


# ── 3. adopt-now 端点账号范围（与巡检口径一致：managed_accounts 恒两网）──


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


def test_adopt_now_both_nets(db, both_accounts, monkeypatch):
    """点导入：testnet-1 + live-1 两账号都执行（与任何开关无关）"""
    adopt_calls = []
    data = _run_adopt_now(db, monkeypatch, adopt_calls)
    assert adopt_calls == ["testnet-1", "live-1"]
    assert data["accounts"] == ["testnet-1", "live-1"]
