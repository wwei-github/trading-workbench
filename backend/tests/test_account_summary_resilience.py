"""账户概览限频韧性（2026-10-22）

测试网出口 IP 被币安 418/-1003 封禁时：
- account-summary 不再返回空壳（只有 4 个字段、available=false）——
  有成功快照则回退旧数据并标 stale，无快照才 available=false，但都带 detail；
- BinanceTrader 记住 "banned until"，封禁期内快速失败、不再发请求。

sqlite 内存库 + 打桩，不触网。
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
import app.services.binance_trader as bt  # noqa: E402
from app.api import trades as trades_mod  # noqa: E402
from app.api.trades import account_summary  # noqa: E402
from app.database import Base  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_caches():
    trades_mod._account_summary_cache.clear()
    bt._ip_ban_until.clear()
    yield
    trades_mod._account_summary_cache.clear()
    bt._ip_ban_until.clear()


def _patch_registry(monkeypatch):
    monkeypatch.setattr(
        "app.services.account_registry.get_account",
        lambda key: SimpleNamespace(key=key, testnet=True, name="测试网"),
    )


class _FakeTrader:
    fail = False
    calls = 0

    def __init__(self, account_key):
        self.account_key = account_key

    def wallet_balance(self):
        type(self).calls += 1
        if type(self).fail:
            raise bt.BinanceTradeError(
                '418: {"code":-1003,"msg":"Way too many requests; '
                'IP(3.172.30.76) banned until 1792683476576."}')
        return {"wallet": 1000.0, "available": 600.0}

    def positions(self):
        return {}


@pytest.fixture(autouse=True)
def _reset_fake_trader():
    _FakeTrader.fail = False
    _FakeTrader.calls = 0
    yield


def test_ttl_reuses_snapshot_without_exchange_call(db, monkeypatch):
    """15s TTL 内连点刷新不再打交易所（抑制限频放大）"""
    _patch_registry(monkeypatch)
    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)

    first = account_summary(account_key="testnet-1", db=db)
    second = account_summary(account_key="testnet-1", db=db)

    assert first["available"] is True and first["stale"] is False
    assert second["wallet"] == 1000.0 and second["stale"] is False
    assert _FakeTrader.calls == 1  # 第二次命中缓存，未再请求


def test_failure_falls_back_to_last_snapshot(db, monkeypatch):
    """查询失败且有快照：保留数字 + stale + detail，不再降级成空壳"""
    _patch_registry(monkeypatch)
    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)
    account_summary(account_key="testnet-1", db=db)

    # 把快照时间推到 TTL 之外，模拟再次查询
    cached_at, snapshot = trades_mod._account_summary_cache["testnet-1"]
    trades_mod._account_summary_cache["testnet-1"] = (cached_at - 60, snapshot)
    _FakeTrader.fail = True

    out = account_summary(account_key="testnet-1", db=db)

    assert out["available"] is True
    assert out["stale"] is True
    assert out["stale_age_sec"] >= 59
    assert out["wallet"] == 1000.0
    assert "限频" in out["detail"]


def test_failure_without_snapshot_exposes_detail(db, monkeypatch):
    """无快照时保持 available=false，但必须带 detail 说明原因"""
    _patch_registry(monkeypatch)
    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)
    _FakeTrader.fail = True

    out = account_summary(account_key="testnet-1", db=db)

    assert out["available"] is False
    assert "wallet" not in out
    assert "限频" in out["detail"]


def test_ip_ban_shortcircuits_followup_requests():
    """418/-1003 后记住 banned until，封禁期内不再发请求"""
    base = "https://testnet.binancefuture.com"
    until_ms = int((time.time() + 120) * 1000)
    calls = []

    class _Client:
        def request(self, *args, **kwargs):
            calls.append(1)
            return httpx.Response(
                418, json={"code": -1003, "msg": f"banned until {until_ms}."})

    trader = bt.BinanceTrader.__new__(bt.BinanceTrader)
    trader.base = base
    trader.key = "k"
    trader.secret = "s"
    trader._client = _Client()

    with pytest.raises(bt.BinanceIpBanned):
        trader._req("GET", "/fapi/v2/balance")
    assert len(calls) == 1
    assert bt._ip_ban_until[base] == pytest.approx(until_ms / 1000.0)

    with pytest.raises(bt.BinanceIpBanned):
        trader._req("GET", "/fapi/v2/balance")
    assert len(calls) == 1  # 短路，未再次请求


def test_failure_picks_up_concurrently_written_snapshot(db, monkeypatch):
    """并发穿透：本次查询失败时，另一请求刚写入的快照应被采用"""
    _patch_registry(monkeypatch)

    class _Racer:
        def __init__(self, account_key):
            pass

        def wallet_balance(self):
            trades_mod._account_summary_cache["testnet-1"] = (time.time(), {
                "testnet": True, "account_key": "testnet-1", "account_name": "测试网",
                "available": True, "wallet": 999.0, "stale": False,
            })
            raise bt.BinanceTradeError("boom")

        def positions(self):
            return {}

    monkeypatch.setattr(bt, "BinanceTrader", _Racer)

    out = account_summary(account_key="testnet-1", db=db)

    assert out["available"] is True
    assert out["stale"] is True
    assert out["wallet"] == 999.0


def test_cache_isolated_per_account(db, monkeypatch):
    """快照按账号分桶：切账号不能命中另一账号的数据"""
    _patch_registry(monkeypatch)
    seen = []

    class _T:
        def __init__(self, account_key):
            seen.append(account_key)

        def wallet_balance(self):
            return {"wallet": 1000.0, "available": 600.0}

        def positions(self):
            return {}

    monkeypatch.setattr(bt, "BinanceTrader", _T)

    account_summary(account_key="testnet-1", db=db)
    account_summary(account_key="testnet-2", db=db)

    assert seen == ["testnet-1", "testnet-2"]


def test_ip_ban_covers_listen_key_paths():
    """listenKey 走 _client 不经过 _req，封禁期内也必须短路"""
    base = "https://testnet.binancefuture.com"
    calls = []

    class _Client:
        def post(self, *args, **kwargs):
            calls.append("post")

        def put(self, *args, **kwargs):
            calls.append("put")

        def delete(self, *args, **kwargs):
            calls.append("delete")

    trader = bt.BinanceTrader.__new__(bt.BinanceTrader)
    trader.base = base
    trader.key = "k"
    trader.secret = "s"
    trader.account_key = "testnet-1"
    trader._client = _Client()
    bt._ip_ban_until[base] = time.time() + 60

    with pytest.raises(bt.BinanceIpBanned):
        trader.create_listen_key()
    with pytest.raises(bt.BinanceIpBanned):
        trader.keepalive_listen_key("lk")
    trader.close_listen_key("lk")  # 尽力而为，吞异常

    assert calls == []


def test_ip_ban_expiry_resumes_requests():
    """封禁到期后自动恢复，不需要重启进程"""
    calls = []

    class _Client:
        def request(self, *args, **kwargs):
            calls.append(1)
            return httpx.Response(200, json=[])

    trader = bt.BinanceTrader.__new__(bt.BinanceTrader)
    trader.base = "https://testnet.binancefuture.com"
    trader.key = "k"
    trader.secret = "s"
    trader._client = _Client()
    bt._ip_ban_until[trader.base] = time.time() - 1

    assert trader._req("GET", "/fapi/v2/balance") == []
    assert len(calls) == 1


def test_ip_ban_without_timestamp_uses_default_backoff():
    """418 未给 banned until 时按默认退避，不能无限重试"""
    calls = []

    class _Client:
        def request(self, *args, **kwargs):
            calls.append(1)
            return httpx.Response(418, json={"code": -1003, "msg": "Way too many requests"})

    trader = bt.BinanceTrader.__new__(bt.BinanceTrader)
    trader.base = "https://testnet.binancefuture.com"
    trader.key = "k"
    trader.secret = "s"
    trader._client = _Client()

    with pytest.raises(bt.BinanceIpBanned):
        trader._req("GET", "/fapi/v2/balance")

    assert bt._ip_ban_until[trader.base] == pytest.approx(
        time.time() + bt.IP_BAN_DEFAULT_SECONDS, abs=5)
