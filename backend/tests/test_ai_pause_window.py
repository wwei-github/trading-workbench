# -*- coding: utf-8 -*-
"""AI 分析暂停时段回归（2026-10-26 配置页新增开关）。

需求：配置中开关打开后可选每天整点区间，区间内关闭「筛选后自动 AI 分析」；
开关关闭（默认）时行为与历史一致——每小时筛选后照常 AI 分析。

覆盖：
1. services.ai_pause 判定：关闭 / 缺小时 / 空区间 / 同日区间 / 跨零点 / 半开边界
2. 时区：aware 与 naive(按 UTC) 输入统一换算北京时间
3. /api/scans/config PUT 校验（缺小时 / 起止相同 / 越界 400）与回读
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.ai_pause import is_ai_paused, pause_window_label  # noqa: E402

BJ = timezone(timedelta(hours=8))


def _cfg(start=None, end=None, enabled=True):
    return SimpleNamespace(
        ai_pause_enabled=enabled,
        ai_pause_start_hour=start,
        ai_pause_end_hour=end,
    )


def _bj(hour: int) -> datetime:
    """北京时间某整点（2026-10-26 为周一，任意日期均可）"""
    return datetime(2026, 10, 26, hour, 0, tzinfo=BJ)


# ── 1. 判定语义 ──────────────────────────────────────────────────────


def test_disabled_never_pauses():
    """开关关闭（默认）：即使库内留着时段也不生效"""
    cfg = _cfg(0, 23, enabled=False)
    assert pause_window_label(cfg) is None
    assert is_ai_paused(cfg, _bj(3)) is False


def test_missing_hours_never_pauses():
    """开关开但小时缺失（历史半配置）：视为未配置，不暂停（防全天停摆）"""
    cfg = _cfg(None, 8)
    assert pause_window_label(cfg) is None
    assert is_ai_paused(cfg, _bj(3)) is False


def test_same_hours_is_empty_window():
    """起止相同 = 空区间，不暂停（与 API 拒绝保存同口径的消费侧兜底）"""
    cfg = _cfg(9, 9)
    assert pause_window_label(cfg) is None
    assert is_ai_paused(cfg, _bj(9)) is False


def test_same_day_window_half_open():
    """同日区间 [start, end)：起点含、终点不含"""
    cfg = _cfg(9, 18)
    assert pause_window_label(cfg) == "09:00-18:00"
    assert is_ai_paused(cfg, _bj(8)) is False
    assert is_ai_paused(cfg, _bj(9)) is True
    assert is_ai_paused(cfg, _bj(17)) is True
    assert is_ai_paused(cfg, _bj(18)) is False


def test_cross_midnight_window():
    """跨零点区间 22 → 6：覆盖 22/23/0/1/2/3/4/5，不含 6 与 21"""
    cfg = _cfg(22, 6)
    assert pause_window_label(cfg) == "22:00-06:00"
    for h in (22, 23, 0, 5):
        assert is_ai_paused(cfg, _bj(h)) is True, f"{h} 点应暂停"
    for h in (6, 12, 21):
        assert is_ai_paused(cfg, _bj(h)) is False, f"{h} 点不应暂停"


def test_naive_datetime_treated_as_utc():
    """naive 输入按 UTC 理解：UTC 01:00 = 北京 09:00，落在 [09, 18) 内"""
    cfg = _cfg(9, 18)
    assert is_ai_paused(cfg, datetime(2026, 10, 26, 1, 0)) is True
    # UTC 18:00 = 北京次日 02:00，不在同日区间内
    assert is_ai_paused(cfg, datetime(2026, 10, 26, 18, 0)) is False


def test_utc_aware_converted_to_beijing():
    """UTC aware 自动换算北京时间：UTC 15:30 = 北京 23:30，命中 22→6"""
    cfg = _cfg(22, 6)
    assert is_ai_paused(cfg, datetime(2026, 10, 26, 15, 30, tzinfo=timezone.utc)) is True
    assert is_ai_paused(cfg, datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)) is False


def test_default_now_does_not_raise():
    """now 省略时取当前北京时间，只验证不抛异常且类型正确"""
    assert isinstance(is_ai_paused(_cfg(0, 23)), bool)


# ── 2. /api/scans/config 校验与回读 ─────────────────────────────────


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


def test_config_defaults_off(client):
    """默认：开关关闭；小时未配置（NULL）"""
    http, _db = client
    body = http.get("/api/scans/config").json()
    assert body["ai_pause_enabled"] is False
    assert body["ai_pause_start_hour"] is None
    assert body["ai_pause_end_hour"] is None


def test_put_and_readback(client):
    """正常保存（含跨零点）→ GET 回读一致"""
    http, _db = client
    r = http.put("/api/scans/config", json={
        "ai_pause_enabled": True,
        "ai_pause_start_hour": 22,
        "ai_pause_end_hour": 6,
    })
    assert r.status_code == 200
    assert r.json()["ai_pause_enabled"] is True
    assert r.json()["ai_pause_start_hour"] == 22
    assert r.json()["ai_pause_end_hour"] == 6

    body = http.get("/api/scans/config").json()
    assert body["ai_pause_enabled"] is True
    assert body["ai_pause_start_hour"] == 22
    assert body["ai_pause_end_hour"] == 6

    # 关闭开关：时段值保留（下次打开即生效），但不再拦截
    assert http.put("/api/scans/config", json={"ai_pause_enabled": False}).status_code == 200
    body = http.get("/api/scans/config").json()
    assert body["ai_pause_enabled"] is False
    assert body["ai_pause_start_hour"] == 22


def test_put_enabled_without_hours_400(client):
    """开启态缺开始/结束整点 → 400（避免「开了但从不生效」静默失效）"""
    http, _db = client
    assert http.put("/api/scans/config", json={
        "ai_pause_enabled": True,
        "ai_pause_start_hour": 22,
    }).status_code == 400
    assert http.put("/api/scans/config", json={
        "ai_pause_enabled": True,
        "ai_pause_end_hour": 6,
    }).status_code == 400
    # 拒绝后库内仍为关闭态
    assert http.get("/api/scans/config").json()["ai_pause_enabled"] is False


def test_put_same_start_end_400(client):
    """开启且起止相同（空区间/全天二义）→ 400"""
    http, _db = client
    r = http.put("/api/scans/config", json={
        "ai_pause_enabled": True,
        "ai_pause_start_hour": 9,
        "ai_pause_end_hour": 9,
    })
    assert r.status_code == 400
    assert "相同" in r.json()["detail"]


@pytest.mark.parametrize("field,value", [
    ("ai_pause_start_hour", -1),
    ("ai_pause_start_hour", 24),
    ("ai_pause_end_hour", 24),
])
def test_put_out_of_range_400(client, field, value):
    """整点越界 0~23 → 400"""
    http, _db = client
    r = http.put("/api/scans/config", json={field: value})
    assert r.status_code == 400
