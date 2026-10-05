# -*- coding: utf-8 -*-
"""请求级访问日志 middleware 单测（2026-10-05 补日志缺口 1）

口径（app.main.access_log_middleware）：
- 正常 2xx 且快 → 静默（不刷库）
- 耗时 ≥2s → WARNING「接口慢请求」
- 状态 ≥400 → WARNING；≥500 → ERROR
- call_next 抛异常 → 按 500 计（异常细节由 500 handler 落库）

TestClient 不用 with（不触发 lifespan 连真实 PG，test_market_api 惯例）。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import logging

import app.main as main_mod


def _make_app(sleep_s: float = 0, raise_exc: bool = False) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(main_mod.access_log_middleware)

    @app.get("/ok")
    def ok():
        if sleep_s:
            asyncio.get_event_loop().run_in_executor(None, lambda: None)
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("爆炸")

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(sleep_s)
        return {"ok": True}

    return app


def _records(caplog) -> list:
    return [r for r in caplog.records if r.name == "app.api.request"]


@pytest.fixture()
def client():
    return TestClient(_make_app())


def test_fast_2xx_silent(client, caplog):
    """正常快速 2xx：不产生访问日志（防刷库）"""
    with caplog.at_level(logging.WARNING, logger="app.api.request"):
        assert client.get("/ok").status_code == 200
    assert _records(caplog) == []


def test_error_4xx_warning(client, caplog):
    """404：WARNING 落「接口错误」（4xx 客户端错误可见但不吵）"""
    with caplog.at_level(logging.WARNING, logger="app.api.request"):
        resp = client.get("/no-such-path")
    assert resp.status_code == 404
    recs = _records(caplog)
    assert len(recs) == 1 and recs[0].levelname == "WARNING"
    assert "接口错误" in recs[0].message and "404" in recs[0].message


def test_error_5xx_error_level(caplog):
    """未处理异常：middleware 以 ERROR 落「接口错误 状态 500」+ 500 handler traceback"""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="app.api.request"):
        resp = client.get("/boom")
    assert resp.status_code == 500
    recs = _records(caplog)
    assert any(r.levelname == "ERROR" and "500" in r.message for r in recs)


def test_slow_request_warning(caplog):
    """耗时 ≥2s：WARNING「接口慢请求」+ 耗时毫秒数"""
    app = _make_app(sleep_s=0.05)  # 真实 50ms 延迟（零延迟时耗时可能 round 到 0ms 不触发）
    # 阈值不可等真 2s——阈值是模块常量，压到 1ms 验证触发路径
    old = main_mod._SLOW_REQUEST_MS
    main_mod._SLOW_REQUEST_MS = 1
    try:
        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="app.api.request"):
            assert client.get("/slow").status_code == 200
    finally:
        main_mod._SLOW_REQUEST_MS = old
    recs = [r for r in _records(caplog) if "慢请求" in r.message]
    assert len(recs) == 1 and "耗时" in recs[0].message
