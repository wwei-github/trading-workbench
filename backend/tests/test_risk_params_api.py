# -*- coding: utf-8 -*-
"""风控参数上页面回归（/api/config，2026-10-02）。

9 项风控参数（10 列）沿用补丁参数模式：nullable 列 + NULL=回落 env 默认 +
model_fields_set 区分「未传」与「显式传 null」。sqlite 内存库不触网。

覆盖：
1. GET 默认全 null（未配置回落 env）；PUT 设置 → GET 回读一致
2. 显式传 null = 恢复 env 默认（列回 null）
3. 范围越界 400（代表性字段）
4. 未传字段不改动（model_fields_set 语义）
5. min_adapt_leverage 上限联动 leverage_max（cfg 覆盖 / body 同传）
"""
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

# 新增的 10 个风控参数列（min_adapt_leverage 等旧补丁参数不含在内）
RISK_FIELDS = [
    "min_recommendation", "daily_loss_stop_pct", "daily_loss_stop_enabled",
    "ai_rr_min", "risk_pct", "leverage_max", "taker_fee_rate",
    "near_anchor_factor", "portfolio_heat_cap", "same_direction_cap",
    "btc_crash_pause_pct",
]


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()

    def _override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), session
    app.dependency_overrides.pop(get_db, None)
    session.close()


def test_default_fields_null(client):
    """未配置时 GET 全部返回 null（消费侧回落 env 默认）"""
    http, _db = client
    r = http.get("/api/scans/config")
    assert r.status_code == 200
    body = r.json()
    for f in RISK_FIELDS:
        assert body[f] is None, f"{f} 默认应为 null"


def test_put_and_readback(client):
    """PUT 设置全部风控参数 → GET 回读一致（类型/数值不漂移）"""
    http, _db = client
    payload = {
        "min_recommendation": 70,
        "daily_loss_stop_pct": 12,
        "daily_loss_stop_enabled": False,
        "ai_rr_min": 1.8,
        "risk_pct": 2.5,
        "leverage_max": 10,
        "taker_fee_rate": 0.0006,
        "near_anchor_factor": 0.8,
        "portfolio_heat_cap": 0.25,
        "same_direction_cap": 3,
        "btc_crash_pause_pct": 1.5,
    }
    r = http.put("/api/scans/config", json=payload)
    assert r.status_code == 200
    body = r.json()
    for f, v in payload.items():
        assert body[f] == pytest.approx(v), f"{f} 回读不一致"


def test_explicit_null_restores_env(client):
    """先设值再显式传 null → 列回 null（页面清空 = 恢复 env 默认）"""
    http, _db = client
    assert http.put("/api/scans/config", json={"ai_rr_min": 2.0, "risk_pct": 5}).status_code == 200
    r = http.put("/api/scans/config", json={"ai_rr_min": None, "risk_pct": None})
    assert r.status_code == 200
    assert r.json()["ai_rr_min"] is None
    assert r.json()["risk_pct"] is None


def test_missing_fields_untouched(client):
    """未传字段不改动（model_fields_set 语义：未传 ≠ 显式 null）"""
    http, _db = client
    assert http.put("/api/scans/config", json={"ai_rr_min": 2.0}).status_code == 200
    # 只传别的字段，ai_rr_min 保持 2.0
    r = http.put("/api/scans/config", json={"min_recommendation": 65})
    assert r.status_code == 200
    assert r.json()["ai_rr_min"] == 2.0
    assert r.json()["min_recommendation"] == 65


def test_status_reflects_breaker_switch_toggle(client, monkeypatch):
    """改当日亏损熔断开关保存 → /status loss_breaker enabled 即时翻转
    （2026-10-06 用户报障回归：顶部卡片熔断状态未随开关更新）。
    mock 自动交易账号与钱包余额，规避触网。"""
    from types import SimpleNamespace

    import app.services.account_registry as reg
    import app.services.binance_trader as bt
    from app.services.trade_engine import _wallet_cache

    accounts = [SimpleNamespace(key="testnet-1", testnet=True)]
    monkeypatch.setattr(reg, "managed_accounts", lambda db: accounts)

    class _FakeTrader:
        def __init__(self, key):
            pass

        def wallet_balance(self):
            return {"wallet": 1000.0}

    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)
    _wallet_cache.clear()

    http, _db = client
    # 开 → enabled=True（有账号、阈值默认 9>0）
    assert http.put("/api/scans/config", json={"daily_loss_stop_enabled": True}).status_code == 200
    lb = http.get("/api/scans/status").json()["loss_breaker"]
    assert lb["testnet"]["enabled"] is True

    # 关 → enabled=False，active=None（顶部卡片显示「关」）
    assert http.put("/api/scans/config", json={"daily_loss_stop_enabled": False}).status_code == 200
    lb = http.get("/api/scans/status").json()["loss_breaker"]
    assert lb["testnet"]["enabled"] is False
    assert lb["testnet"]["active"] is None


def test_tp2_enabled_toggle(client):
    """TP2 分批止盈开关（2026-10-08）：默认关 → PUT 开/关回读一致（布尔不漂移）"""
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert body["tp2_enabled"] is False

    assert http.put("/api/scans/config", json={"tp2_enabled": True}).status_code == 200
    assert http.get("/api/scans/config").json()["tp2_enabled"] is True

    assert http.put("/api/scans/config", json={"tp2_enabled": False}).status_code == 200
    assert http.get("/api/scans/config").json()["tp2_enabled"] is False


def test_tp_qty_ratio_config(client):
    """TP 分批比例（2026-10-09 配置化）：默认 None → PUT 回读一致；
    越界/两档合计>95% 拒绝（跟进腿 ≥5% 铁律）；传 null 恢复 env 默认"""
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert body["tp1_qty_pct_with_tp2"] is None
    assert body["tp2_qty_pct"] is None
    assert body["tp1_qty_pct_no_tp2"] is None

    assert http.put("/api/scans/config", json={
        "tp1_qty_pct_with_tp2": 55, "tp2_qty_pct": 25, "tp1_qty_pct_no_tp2": 70,
    }).status_code == 200
    b = http.get("/api/scans/config").json()
    assert b["tp1_qty_pct_with_tp2"] == 55
    assert b["tp2_qty_pct"] == 25
    assert b["tp1_qty_pct_no_tp2"] == 70

    # 两档合计 >95 拒绝（新值 80 + 库里 TP2 25 = 105）
    assert http.put("/api/scans/config", json={"tp1_qty_pct_with_tp2": 80}).status_code == 400
    # 单值越界拒绝
    assert http.put("/api/scans/config", json={"tp1_qty_pct_no_tp2": 100}).status_code == 400
    assert http.put("/api/scans/config", json={"tp2_qty_pct": 0}).status_code == 400
    # 传 null 恢复 env 默认
    assert http.put("/api/scans/config", json={"tp1_qty_pct_with_tp2": None}).status_code == 200
    assert http.get("/api/scans/config").json()["tp1_qty_pct_with_tp2"] is None


def test_ai_rr_tol_config(client):
    """现价 RR 复算容差（2026-10-11 配置化）：默认 None → PUT 回读一致；
    越界拒绝（0~0.2）；传 null 恢复 env 默认 0.02"""
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert body["ai_rr_tol"] is None

    assert http.put("/api/scans/config", json={"ai_rr_tol": 0.05}).status_code == 200
    assert http.get("/api/scans/config").json()["ai_rr_tol"] == 0.05

    # 0=严格、0.2=上限均合法
    assert http.put("/api/scans/config", json={"ai_rr_tol": 0}).status_code == 200
    assert http.get("/api/scans/config").json()["ai_rr_tol"] == 0
    assert http.put("/api/scans/config", json={"ai_rr_tol": 0.2}).status_code == 200

    # 传 null 恢复 env 默认
    assert http.put("/api/scans/config", json={"ai_rr_tol": None}).status_code == 200
    assert http.get("/api/scans/config").json()["ai_rr_tol"] is None


@pytest.mark.parametrize("field,value", [
    ("min_recommendation", 101),
    ("min_recommendation", -1),
    ("daily_loss_stop_pct", 51),
    ("ai_rr_min", 0.4),
    ("ai_rr_min", 10.1),
    ("ai_rr_tol", -0.01),
    ("ai_rr_tol", 0.21),
    ("risk_pct", 0.05),
    ("risk_pct", 20.1),
    ("leverage_max", 0),
    ("leverage_max", 126),
    ("taker_fee_rate", 0.006),
    ("near_anchor_factor", 1.5),
    ("portfolio_heat_cap", 1.5),
    ("same_direction_cap", 21),
    ("btc_crash_pause_pct", 20.1),
])
def test_out_of_range_400(client, field, value):
    """范围越界 400，配置不被写入"""
    http, _db = client
    r = http.put("/api/scans/config", json={field: value})
    assert r.status_code == 400, f"{field}={value} 应越界 400"


def test_min_adapt_leverage_cap_follows_leverage_max(client):
    """min_adapt_leverage 校验上限联动 leverage_max：cfg 已设 10 → 15 被拒、8 放行"""
    http, _db = client
    assert http.put("/api/scans/config", json={"leverage_max": 10}).status_code == 200
    r = http.put("/api/scans/config", json={"min_adapt_leverage": 15})
    assert r.status_code == 400
    assert "10" in r.json()["detail"]
    assert http.put("/api/scans/config", json={"min_adapt_leverage": 8}).status_code == 200


def test_min_adapt_leverage_cap_same_body(client):
    """同请求内先传 leverage_max 再传 min_adapt_leverage：按本次新值为准"""
    http, _db = client
    r = http.put(
        "/api/scans/config",
        json={"leverage_max": 5, "min_adapt_leverage": 8},
    )
    assert r.status_code == 400  # 8 > 新上限 5
    r = http.put(
        "/api/scans/config",
        json={"leverage_max": 10, "min_adapt_leverage": 8},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["leverage_max"] == 10
    assert body["min_adapt_leverage"] == 8


def test_min_adapt_leverage_cap_leverage_max_cleared(client):
    """同请求显式 leverage_max=null（恢复 env 默认）：min_adapt 按 env 上限校验
    （回归：修复前误用旧 cfg 值 10 拒绝 15）"""
    http, _db = client
    assert http.put("/api/scans/config", json={"leverage_max": 10}).status_code == 200
    r = http.put(
        "/api/scans/config",
        json={"leverage_max": None, "min_adapt_leverage": 15},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["leverage_max"] is None
    assert body["min_adapt_leverage"] == 15


def test_daily_loss_stop_pct_zero_allowed(client):
    """熔断阈值 0=关闭（与 env 语义/消费侧 loss_pct>0 判定一致）"""
    http, _db = client
    r = http.put("/api/scans/config", json={"daily_loss_stop_pct": 0})
    assert r.status_code == 200
    assert r.json()["daily_loss_stop_pct"] == 0


# ── 配置变更审计日志（2026-10-14：模型/风控参数被改须可回溯） ──

_AUDIT_LOGGER = "app.api.scan"


def test_put_config_writes_audit_log(client, caplog):
    """PUT 改模型 → WARNING 审计落 app_logs 链（log_sink 收 app.* WARNING），
    含键名、旧值（未配置=NULL）与新值"""
    http, _db = client
    with caplog.at_level(logging.WARNING, logger=_AUDIT_LOGGER):
        r = http.put("/api/scans/config", json={"ai_model": "deepseek/deepseek-flash"})
    assert r.status_code == 200
    audit = [
        x.message for x in caplog.records
        if x.name == _AUDIT_LOGGER and "系统配置更新" in x.message
    ]
    assert len(audit) == 1
    assert "ai_model" in audit[0]
    assert "deepseek/deepseek-flash" in audit[0]
    assert "NULL" in audit[0]  # 旧值未配置


def test_put_config_rejected_writes_no_audit(client, caplog):
    """越界 400 未落库 → 不产生审计日志（防止审计记录与实际配置不符）"""
    http, _db = client
    with caplog.at_level(logging.WARNING, logger=_AUDIT_LOGGER):
        r = http.put("/api/scans/config", json={"ai_rr_tol": 5})
    assert r.status_code == 400
    assert not any("系统配置更新" in x.message for x in caplog.records)
