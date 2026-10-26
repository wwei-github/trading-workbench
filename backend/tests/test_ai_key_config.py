# -*- coding: utf-8 -*-
"""AI API Key 上页面 + 凭证成对生效回归（2026-10-26）。

需求：策略配置弹窗可修改 AI_API_KEY；页面 Key 与 AI_BASE_URL **成对**都配置时
才生效，只配一项则整对回落 .env 默认值。

覆盖：
1. resolve_credentials / mask_api_key 语义（成对才用 DB，缺一回落 env）
2. GET 不回传明文，只回 ai_api_key_set / ai_api_key_masked
3. PUT 成对保存 → ai_api_pair_source=db、effective=页面值
4. 只配 Key（或只配端点）→ pair_source=env、effective=env 值
5. 不传 ai_api_key 时保留库内原值；显式 null/空串 = 清除
6. 审计日志只记「已设置/未设置」，不含明文
7. ai_analysis_enabled 开启门槛随生效凭证（同请求配齐凭证可开启）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm_client import (  # noqa: E402
    configure_from,
    current_api_key,
    current_base_url,
    mask_api_key,
    resolve_credentials,
)

ENV_KEY = "env-key-123456"
ENV_URL = "https://env.example.com/v1"
DB_KEY = "db-secret-key-9876543210"
DB_URL = "https://db.example.com/v1"


@pytest.fixture(autouse=True)
def _env_creds(monkeypatch):
    """固定 env 凭证（conftest 只清空 AI_API_KEY），并复位运行时覆盖"""
    from app.services import llm_client

    monkeypatch.setattr(settings, "AI_API_KEY", ENV_KEY)
    monkeypatch.setattr(settings, "AI_BASE_URL", ENV_URL)
    monkeypatch.setattr(llm_client, "_runtime", {})
    monkeypatch.setattr(llm_client, "_client_key", ())


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


def _cfg(key="", url=""):
    return SimpleNamespace(ai_api_key=key, ai_base_url=url)


# ── 1. 成对解析 ─────────────────────────────────────────────────────


def test_pair_resolution_env_fallback():
    """缺一（含全缺 / cfg=None）→ 整对回落 env，绝不混搭页面 Key + env 端点"""
    assert resolve_credentials(None) == (ENV_KEY, ENV_URL)
    assert resolve_credentials(_cfg()) == (ENV_KEY, ENV_URL)
    assert resolve_credentials(_cfg(DB_KEY, "")) == (ENV_KEY, ENV_URL)
    assert resolve_credentials(_cfg("", DB_URL)) == (ENV_KEY, ENV_URL)
    assert resolve_credentials(_cfg("   ", DB_URL)) == (ENV_KEY, ENV_URL)


def test_pair_resolution_db_when_both():
    assert resolve_credentials(_cfg(DB_KEY, DB_URL)) == (DB_KEY, DB_URL)


def test_configure_from_switches_runtime_creds():
    configure_from(_cfg(DB_KEY, DB_URL))
    assert current_api_key() == DB_KEY
    assert current_base_url() == DB_URL
    # 只配一项 → 整对回 env
    configure_from(_cfg(DB_KEY, ""))
    assert current_api_key() == ENV_KEY
    assert current_base_url() == ENV_URL
    configure_from(None)
    assert current_api_key() == ENV_KEY
    assert current_base_url() == ENV_URL


def test_mask_api_key_never_plaintext():
    masked = mask_api_key(DB_KEY)
    assert DB_KEY not in masked
    assert masked.startswith(DB_KEY[:3])
    assert masked.endswith(DB_KEY[-4:])
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "*****"


# ── 2. GET / PUT 配置契约 ───────────────────────────────────────────


def test_get_never_returns_plaintext(client):
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert body["ai_api_key_set"] is False
    assert body["ai_api_key_masked"] == ""
    assert "ai_api_key" not in body
    assert body["ai_api_pair_source"] == "env"
    assert body["ai_base_url_effective"] == ENV_URL


def test_put_pair_takes_effect(client):
    """Key+端点同时配置 → 页面凭证生效，响应脱敏"""
    http, _db = client
    r = http.put("/api/scans/config", json={
        "ai_api_key": DB_KEY,
        "ai_base_url": DB_URL,
    })
    assert r.status_code == 200
    body = r.json()
    assert DB_KEY not in r.text             # 明文永不回传
    assert body["ai_api_key_set"] is True
    assert body["ai_api_key_masked"] == mask_api_key(DB_KEY)
    assert body["ai_api_pair_source"] == "db"
    assert body["ai_base_url_effective"] == DB_URL
    assert body["ai_configured"] is True


def test_put_only_key_falls_back_to_env(client):
    """只配 Key：库内记下（供后续补端点），但整对回落 env"""
    http, _db = client
    r = http.put("/api/scans/config", json={"ai_api_key": DB_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["ai_api_key_set"] is True
    assert body["ai_api_pair_source"] == "env"
    assert body["ai_base_url_effective"] == ENV_URL


def test_put_only_url_falls_back_to_env(client):
    """只配端点（页面 Key 未配）→ 整对回落 env"""
    http, _db = client
    body = http.put("/api/scans/config", json={"ai_base_url": DB_URL}).json()
    assert body["ai_api_key_set"] is False
    assert body["ai_api_pair_source"] == "env"
    assert body["ai_base_url_effective"] == ENV_URL


def test_unset_key_keeps_stored_value(client):
    """不传 ai_api_key（只改别的键）→ 库内 Key 原样保留"""
    http, _db = client
    http.put("/api/scans/config", json={"ai_api_key": DB_KEY, "ai_base_url": DB_URL})
    body = http.put("/api/scans/config", json={"ai_model": "deepseek/deepseek-flash"}).json()
    assert body["ai_api_key_set"] is True
    assert body["ai_api_pair_source"] == "db"


def test_clear_key_with_null(client):
    """显式 null / 空串 = 清除，回落 env"""
    http, _db = client
    http.put("/api/scans/config", json={"ai_api_key": DB_KEY, "ai_base_url": DB_URL})
    body = http.put("/api/scans/config", json={"ai_api_key": None}).json()
    assert body["ai_api_key_set"] is False
    assert body["ai_api_pair_source"] == "env"
    http.put("/api/scans/config", json={"ai_api_key": DB_KEY})
    body = http.put("/api/scans/config", json={"ai_api_key": ""}).json()
    assert body["ai_api_key_set"] is False


def test_key_too_long_400(client):
    http, _db = client
    r = http.put("/api/scans/config", json={"ai_api_key": "x" * 256})
    assert r.status_code == 400


def test_audit_log_masks_key(client, caplog):
    """审计 WARNING 只记「已设置/未设置」，明文不入日志"""
    http, _db = client
    with caplog.at_level("WARNING"):
        http.put("/api/scans/config", json={"ai_api_key": DB_KEY, "ai_base_url": DB_URL})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert DB_KEY not in text
    assert "ai_api_key: NULL→已设置" in text


def test_enable_ai_with_same_request_credentials(client):
    """env 无 Key 时：同请求配齐页面 Key+端点即可开启 AI 分析"""
    http, _db = client
    settings.AI_API_KEY = ""
    r = http.put("/api/scans/config", json={
        "ai_api_key": DB_KEY,
        "ai_base_url": DB_URL,
        "ai_analysis_enabled": True,
    })
    assert r.status_code == 200
    assert r.json()["ai_analysis_enabled"] is True


def test_enable_ai_without_any_credentials_400(client):
    """页面与 env 都没有 Key → 开启 AI 分析 400"""
    http, _db = client
    settings.AI_API_KEY = ""
    r = http.put("/api/scans/config", json={"ai_analysis_enabled": True})
    assert r.status_code == 400
    assert "AI 未配置" in r.json()["detail"]
