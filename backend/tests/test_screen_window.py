"""扫描尾窗（2026-10-11-4）：:55 定时扫描把近收盘未收盘根视作已收盘筛选。

覆盖 market_data.in_forming_tail 边界与 screen_klines 幽灵尾根视图——
幽灵复制使 [-1] 仍为「未收盘」（被管线剔除）、[-2] 变为近收盘根（信号根），
全管线 [:-1] 剔尾约定零改动即完成信号根前移。
"""
from datetime import datetime

import pytest

from app.services.market_data import (
    FORMING_TAIL_MINUTES,
    in_forming_tail,
    screen_klines,
)


def _dt(h, m, s=0):
    return datetime(2026, 10, 11, h, m, s)


# ── in_forming_tail 边界 ──

def test_1h_tail_window_boundaries():
    """1h：:55:00 起进入尾窗（含），:00:00 整点退出（新周期刚开始）"""
    assert in_forming_tail("1h", _dt(14, 54, 59)) is False   # 剩余 5:01
    assert in_forming_tail("1h", _dt(14, 55, 0)) is True     # 剩余 5:00（含）
    assert in_forming_tail("1h", _dt(14, 59, 59)) is True    # 剩余 0:01
    assert in_forming_tail("1h", _dt(15, 0, 0)) is False     # 新周期，剩余 60:00
    assert in_forming_tail("1h", _dt(15, 30, 0)) is False    # 周期中段


def test_4h_tail_window_boundaries():
    """4h：仅每个 4h 周期最后 5 分钟进入尾窗（14:55 处于 12:00-16:00 周期中段）"""
    assert in_forming_tail("4h", _dt(14, 55, 0)) is False    # 剩余 65min
    assert in_forming_tail("4h", _dt(15, 55, 0)) is True     # 剩余 5min
    assert in_forming_tail("4h", _dt(16, 0, 0)) is False     # 新周期


def test_default_now_no_crash():
    """不注入 now 时按当前时间求值（冒烟：可调用、返回布尔）"""
    assert isinstance(in_forming_tail("1h"), bool)


# ── 调度分钟数 ↔ 尾窗宽度单一来源（2026-10-12）──

def test_beat_minute_drives_tail_width():
    """beat 的 crontab 分钟数与尾窗宽度必须同源：二者漂移会让尾窗静默失效
    （in_forming_tail 恒 False → 筛选悄悄退回「看上一根已收盘根」）"""
    from app.celery_app import celery_app
    from app.config import settings

    sched = celery_app.conf.beat_schedule["hourly-scan"]["schedule"]
    assert int(sched._orig_minute) == settings.SCAN_BEAT_MINUTE
    assert FORMING_TAIL_MINUTES == 60 - settings.SCAN_BEAT_MINUTE
    assert 0 < FORMING_TAIL_MINUTES < 60


# ── screen_klines 幽灵尾根 ──

def _klines(n=40):
    """构造 n 根 1h K线：最后一根为「未收盘根」（与交易所返回同构）"""
    return [
        [1700000000000 + i * 3600000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
        for i in range(n)
    ]


def test_screen_klines_ghost_in_window():
    """尾窗内：追加幽灵尾根（复制未收盘根）——[-2] 成为视作已收盘的信号根"""
    kl = _klines()
    forming = kl[-1]
    out = screen_klines(kl, "1h", _dt(14, 56))
    assert len(out) == len(kl) + 1
    assert out[-1] == forming          # 幽灵 = 未收盘根快照复制
    assert out[-1] is not forming      # 独立副本（元素可变，须隔离）
    assert out[-2] == forming          # 信号根 = 近收盘未收盘根
    assert out[:-1] == kl              # 前段原样


def test_screen_klines_ghost_copy_isolation():
    """幽灵为浅拷贝：改幽灵元素不影响原数组（缓存数据不被污染）"""
    kl = _klines()
    out = screen_klines(kl, "1h", _dt(14, 56))
    out[-1][4] = 999.0
    assert kl[-1][4] != 999.0


def test_screen_klines_passthrough_outside_window():
    """非尾窗：原样返回（等价旧行为，信号根=上一根已收盘）"""
    kl = _klines()
    assert screen_klines(kl, "1h", _dt(15, 30)) is kl
    assert screen_klines(kl, "1h", _dt(10, 0)) is kl


def test_screen_klines_empty_safe():
    """空数组安全：原样返回不崩"""
    assert screen_klines([], "1h", _dt(14, 56)) == []


# ── 图表K线尾窗强刷（H5，2026-10-12）─────────────────────────────────
# K线缓存按小时桶且桶内不刷新：尾窗内未收盘根随时在变，图表端必须强刷，
# 否则最后一根停在小时内首次取数的快照上（最坏差近 1h），与扫描/AI 不一致。


@pytest.fixture()
def chart_client(monkeypatch):
    """/api/scans/klines 测试客户端：内存 sqlite + K线源打桩记录 force_refresh"""
    import app.models  # noqa: F401
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.orm import sessionmaker

    import app.api.scan as scan_api
    from app.database import Base, get_db
    from app.main import app
    from app.models.system_config import SystemConfig

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1))
    session.commit()

    calls: list[dict] = []

    def _fake(self, symbol, interval="1h", limit=240, allow_stale=False, force_refresh=False):
        calls.append({"symbol": symbol, "interval": interval, "force_refresh": force_refresh})
        base = 1700000000000
        return [
            [base + i * 3_600_000, 100.0, 101.0, 99.0, 100.5, 10.0]
            for i in range(40)
        ]

    monkeypatch.setattr(scan_api.ExchangePool, "get_klines", _fake)

    def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), calls
    app.dependency_overrides.pop(get_db, None)
    session.close()


def _force_flag(monkeypatch, tail: bool):
    """把 market_data.in_forming_tail 固定为 tail（scan.py 经模块引用调用）"""
    import app.services.market_data as md

    monkeypatch.setattr(md, "in_forming_tail", lambda interval, now=None: tail)


def test_chart_klines_force_refresh_in_tail(chart_client, monkeypatch):
    """尾窗内强制强刷：最后一根不能停在小时内旧快照上"""
    client, calls = chart_client
    _force_flag(monkeypatch, True)
    resp = client.get("/api/scans/klines/BTCUSDT", params={"limit": 40})
    assert resp.status_code == 200
    assert calls and calls[-1]["force_refresh"] is True


def test_chart_klines_uses_cache_outside_tail(chart_client, monkeypatch):
    """非尾窗沿用缓存（不强刷，省请求，等价旧行为）"""
    client, calls = chart_client
    _force_flag(monkeypatch, False)
    resp = client.get("/api/scans/klines/BTCUSDT", params={"limit": 40})
    assert resp.status_code == 200
    assert calls and calls[-1]["force_refresh"] is False


def test_chart_klines_client_force_overrides_outside_tail(chart_client, monkeypatch):
    """客户端显式 force_refresh=true（图表「更新」按钮）：非尾窗也强刷"""
    client, calls = chart_client
    _force_flag(monkeypatch, False)
    resp = client.get(
        "/api/scans/klines/BTCUSDT", params={"limit": 40, "force_refresh": True}
    )
    assert resp.status_code == 200
    assert calls[-1]["force_refresh"] is True
