"""扫描尾窗机制：把近收盘未收盘根视作已收盘参与筛选（2026-10-11-4）。

2026-10-12 定时扫描改整点 :00 后尾窗关闭（派生宽度 0），机制保留；2026-10-13
新增第二触发条件「末根已收盘」（整点取数竞态：新周期根尚未出现在响应里、末根
实为刚收盘根——该根就是应评的信号根，不补幽灵会被当未收盘剔除而错位漏评）。
幽灵复制使 [-1] 仍为「未收盘」（被管线剔除）、[-2] 变为信号根，全管线 [:-1]
剔尾约定零改动即完成信号根对齐。

夹具时间口径（2026-10-13 重做）：末根开盘须对齐注入的 now（旧夹具时间戳取
2023 常量，与 now 脱节，新判定下会被误判「末根已收盘」）。
"""
from datetime import datetime, timezone

import pytest

from app.services.market_data import (
    FORMING_TAIL_MINUTES,
    in_forming_tail,
    screen_klines,
)


def _dt(h, m, s=0):
    return datetime(2026, 10, 11, h, m, s, tzinfo=timezone.utc)


# ── in_forming_tail 边界（2026-10-12：beat 改整点 :00，尾窗关闭）──

def test_tail_disabled_at_hourly_beat():
    """SCAN_BEAT_MINUTE=0（整点触发）→ 派生宽度 0 → 1h/4h 恒不在尾窗：
    筛选口径回「看上一根已收盘根」，screen_klines 全时点原样透传"""
    assert FORMING_TAIL_MINUTES == 0
    for h, m in [(14, 54), (14, 55), (14, 59), (15, 0), (15, 30)]:
        assert in_forming_tail("1h", _dt(h, m)) is False
    for h, m in [(14, 55), (15, 55), (16, 0)]:
        assert in_forming_tail("4h", _dt(h, m)) is False


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
    # 整点触发（0）时尾窗关闭（0），其余为「距收盘剩余分钟数」
    expect = 0 if settings.SCAN_BEAT_MINUTE == 0 else 60 - settings.SCAN_BEAT_MINUTE
    assert FORMING_TAIL_MINUTES == expect
    assert 0 <= FORMING_TAIL_MINUTES < 60


# ── screen_klines 幽灵尾根（尾窗 ∨ 末根已收盘）──

def _tail_on(monkeypatch, minutes=5):
    """临时打开尾窗（覆盖派生宽度）：机制本身仍受测，与当前 beat 分钟数解耦
    （2026-10-12 整点触发后宽度为 0，故须显式打开才走尾窗分支）"""
    import app.services.market_data as md

    monkeypatch.setattr(md, "FORMING_TAIL_MINUTES", minutes)


def _klines(n=40, last_open=None, step_min=60):
    """构造 n 根 K线：末根开盘于 last_open（默认 2026-10-11 14:00 UTC），
    前推 step_min 分钟/根。末根开盘时刻是对齐 now 的唯一时间锚"""
    last_ms = int((last_open or _dt(14, 0)).timestamp() * 1000)
    step = step_min * 60_000
    return [
        [last_ms - (n - 1 - i) * step, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
        for i in range(n)
    ]


def test_screen_klines_ghost_in_window(monkeypatch):
    """尾窗内：追加幽灵尾根（复制未收盘根）——[-2] 成为视作已收盘的信号根"""
    _tail_on(monkeypatch)
    kl = _klines()
    forming = kl[-1]
    out = screen_klines(kl, "1h", _dt(14, 56))
    assert len(out) == len(kl) + 1
    assert out[-1] == forming          # 幽灵 = 未收盘根快照复制
    assert out[-1] is not forming      # 独立副本（元素可变，须隔离）
    assert out[-2] == forming          # 信号根 = 近收盘未收盘根
    assert out[:-1] == kl              # 前段原样


def test_screen_klines_ghost_copy_isolation(monkeypatch):
    """幽灵为浅拷贝：改幽灵元素不影响原数组（缓存数据不被污染）"""
    _tail_on(monkeypatch)
    kl = _klines()
    out = screen_klines(kl, "1h", _dt(14, 56))
    out[-1][4] = 999.0
    assert kl[-1][4] != 999.0


def test_screen_klines_passthrough_when_last_forming():
    """末根未走完且非尾窗：原样返回（正常取数口径，信号根=上一根已收盘）"""
    kl = _klines(last_open=_dt(15, 0))
    assert screen_klines(kl, "1h", _dt(15, 30)) is kl
    kl = _klines(last_open=_dt(14, 0))
    assert screen_klines(kl, "1h", _dt(14, 30)) is kl


def test_screen_klines_passthrough_when_tail_disabled():
    """尾窗关闭（当前生产口径，宽度 0）：:56 时点、末根未收盘 → 原样返回"""
    kl = _klines()
    assert screen_klines(kl, "1h", _dt(14, 56)) is kl


def test_screen_klines_ghost_when_last_closed():
    """整点取数竞态（2026-10-13）：末根已收盘（开盘+1h ≤ now）即视作信号根——
    新周期根尚未出现在响应里，不补幽灵会把它当未收盘剔除、信号根错位到前一根"""
    kl = _klines(last_open=_dt(14, 0))              # 末根 = 14:00 根（15:00 收盘）
    out = screen_klines(kl, "1h", _dt(15, 0, 30))   # 15:00 扫描拿到竞态快照
    assert len(out) == len(kl) + 1
    assert out[-1] == kl[-1] and out[-1] is not kl[-1]
    assert out[-2] == kl[-1]                        # 信号根 = 刚收盘根（不再错位）
    assert out[:-1] == kl


def test_screen_klines_closed_boundary_exact_hour():
    """边界：now 恰等于开盘+周期（<= 取等）→ 判已收盘，补幽灵"""
    kl = _klines(last_open=_dt(14, 0))
    out = screen_klines(kl, "1h", _dt(15, 0, 0))
    assert len(out) == len(kl) + 1 and out[-2] == kl[-1]


def test_screen_klines_last_forming_one_sec_before_close():
    """距收盘 1 秒（末根=当前周期根，数据新鲜）→ 透传（信号根=上一根）"""
    kl = _klines(last_open=_dt(14, 0))
    assert screen_klines(kl, "1h", _dt(14, 59, 59)) is kl


def test_screen_klines_closed_4h_interval():
    """周期非 1h：按 interval 分钟数判定（4h 根 12:00 开盘 → 16:00 收盘）"""
    kl = _klines(last_open=_dt(12, 0), step_min=240)
    assert screen_klines(kl, "4h", _dt(15, 59, 59)) is kl       # 未收盘
    out = screen_klines(kl, "4h", _dt(16, 0, 1))                # 竞态：4h 根已收盘
    assert len(out) == len(kl) + 1 and out[-2] == kl[-1]


def test_screen_klines_naive_now_as_utc():
    """naive now 按 UTC 理解（生产恒传 aware；不得落到本机时区致判定漂移）"""
    kl = _klines(last_open=_dt(14, 0))
    out = screen_klines(kl, "1h", datetime(2026, 10, 11, 15, 0, 30))
    assert len(out) == len(kl) + 1 and out[-2] == kl[-1]


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
