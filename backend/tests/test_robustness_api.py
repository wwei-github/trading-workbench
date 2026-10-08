# -*- coding: utf-8 -*-
"""P-12 稳健性扫描 API + 任务回归。

sqlite 内存库；任务级测试 monkeypatch _sample_signals/_load_klines（不触网
不触真采样链路），DSR/PBO 数学已由 test_robustness_stats 覆盖——本文件验证：
1. API：scan 源必须带 symbols / 非法网格键 400 / n_blocks 奇数 422 /
   正常提交 + 2h 内在跑 409 / 列表与详情
2. 任务端到端：合成信号 × 默认网格 12 变体 → DONE 落库、矩阵行过滤
   （open 剔除进 excluded_n）、PBO 样本不足走 insufficient 结论、
   逐变体统计形状（decided/win_rate/dsr）
3. 任务失败路径：空采样 → FAILED + error 落库
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
import app.tasks.backtest_tasks as bt  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.backtest import RobustnessScan  # noqa: E402

HOUR_MS = 3_600_000
T0 = 1_700_000_000_000


def _bar(i: int, kind: str) -> list:
    if kind == "rise":
        # 涨 13 根触 tp1/tp2，随后回落触发跟进轨——保证全部变体 decided
        # （纯单边上升会让剩余 0.25 跟进仓永不回踩 → open，无 PBO 矩阵行）
        o = (100.0 + i * 0.25) if i <= 12 else (103.0 - (i - 12) * 0.5)
        return [T0 + i * HOUR_MS, o, o + 0.5, o - 0.2, o + 0.3, 1000.0]
    if kind == "fall":
        o = 100.0 - i * 0.25
        return [T0 + i * HOUR_MS, o, o + 0.2, o - 0.5, o - 0.3, 1000.0]
    return [T0 + i * HOUR_MS, 100.0, 100.2, 99.8, 100.0, 1000.0]  # flat


BARS = {
    "RISEUSDT": [_bar(i, "rise") for i in range(40)],
    "FALLUSDT": [_bar(i, "fall") for i in range(40)],
    "FLATUSDT": [_bar(i, "flat") for i in range(40)],
}

SIGNALS = [
    {"symbol": "RISEUSDT", "direction": "long", "entry": 100.0, "stop": 99.0,
     "tp1": 101.5, "tp2": 103.0, "entry_time_ms": T0, "meta": {"src": "test"}},
    {"symbol": "FALLUSDT", "direction": "long", "entry": 100.0, "stop": 99.0,
     "tp1": 101.5, "tp2": 103.0, "entry_time_ms": T0, "meta": {"src": "test"}},
    {"symbol": "FLATUSDT", "direction": "long", "entry": 100.0, "stop": 99.0,
     "tp1": 101.5, "tp2": 103.0, "entry_time_ms": T0, "meta": {"src": "test"}},
]

NEED = {sym: (T0 - HOUR_MS, T0 + 40 * HOUR_MS) for sym in BARS}


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    import app.database as database_mod
    monkeypatch.setattr(database_mod, "SessionLocal", TestSession)
    # API 层 dependency override（TestClient 工作线程共享内存库）
    api_session = TestSession()

    def _override():
        yield api_session

    app.dependency_overrides[get_db] = _override
    # 任务层：任务记录/交易所池/采样/K线 全部本地化
    monkeypatch.setattr(bt, "open_task", lambda *a, **k: "tid")
    monkeypatch.setattr(bt, "close_task", lambda *a, **k: None)
    monkeypatch.setattr(bt, "ExchangePool", lambda: None)
    monkeypatch.setattr(bt, "run_robustness_scan_task", bt.run_robustness_scan_task,
                        raising=False)  # 保持可 .delay（API 测试打桩用）
    monkeypatch.setattr(
        bt, "_load_klines",
        lambda db, pool, symbol, interval, start_ms, end_ms:
        BARS.get(symbol, []) if interval == "1h" else [])
    yield TestClient(app), TestSession, monkeypatch
    app.dependency_overrides.pop(get_db, None)


# ── 采样公共分支（不打桩直调，防重构丢 import 的 NameError 回归）────────


def test_sample_signals_history_unpatched():
    """history 源空库：真实跑通（不 NameError）返回空信号。"""
    from app.tasks.backtest_tasks import _sample_signals

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    signals, need = _sample_signals(db, None, "history", [], 30, 24)
    assert signals == []
    assert need == {}


def test_sample_signals_scan_requires_config():
    """scan 源：SystemConfig 缺行时 resolve_scan_filters 走默认（不炸）。"""
    from app.tasks.backtest_tasks import _sample_signals

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    signals, need = _sample_signals(db, None, "scan", ["XUSDT"], 3, 24)
    assert signals == []                       # 无 K 线可拉（pool=None 会被 except 吞）
    assert "XUSDT" in need                     # need 覆盖请求币种


# ── API ───────────────────────────────────────────────────────────────


def test_post_rejects_scan_without_symbols(env):
    client, _, _ = env
    r = client.post("/api/backtests/robustness",
                    json={"source": "scan", "symbols": []})
    assert r.status_code == 400
    assert "symbols" in r.json()["detail"]


def test_post_rejects_bad_grid(env):
    client, _, _ = env
    r = client.post("/api/backtests/robustness",
                    json={"source": "scan", "symbols": ["BTCUSDT"],
                          "grid": {"fwd_bars": [24]}})
    assert r.status_code == 400
    assert "非法网格键" in r.json()["detail"]


def test_post_rejects_odd_blocks(env):
    client, _, _ = env
    r = client.post("/api/backtests/robustness",
                    json={"source": "scan", "symbols": ["BTCUSDT"],
                          "n_blocks": 7})
    assert r.status_code == 422


def test_post_ok_then_409(env):
    client, session, monkeypatch = env
    monkeypatch.setattr(bt.run_robustness_scan_task, "delay",
                        lambda sid: None, raising=False)
    r = client.post("/api/backtests/robustness",
                    json={"source": "scan", "symbols": ["BTCUSDT"], "days": 3})
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"
    # 2h 内已有 RUNNING → 409
    r2 = client.post("/api/backtests/robustness",
                     json={"source": "scan", "symbols": ["ETHUSDT"], "days": 3})
    assert r2.status_code == 409
    # 列表可见
    lst = client.get("/api/backtests/robustness").json()
    assert len(lst) == 1 and lst[0]["status"] == "RUNNING"


def test_get_detail_404(env):
    client, _, _ = env
    assert client.get("/api/backtests/robustness/00000000-0000-0000-0000-000000000000"
                      ).status_code == 404


# ── 任务端到端 ─────────────────────────────────────────────────────────


def test_task_end_to_end_insufficient_pbo(env):
    """3 合成信号 × 默认网格：2 decided + 1 open（被剔除）→ DONE；矩阵 2 行
    < 40 → PBO 跳过走 insufficient 结论；逐变体统计形状正确。"""
    client, TestSession, monkeypatch = env
    monkeypatch.setattr(bt, "_sample_signals",
                        lambda db, pool, source, symbols, days, fwd: (SIGNALS, NEED))
    session = TestSession()
    row = RobustnessScan(params={"source": "scan", "symbols": ["RISEUSDT"],
                                 "days": 3, "grid": None, "n_blocks": 8})
    session.add(row)
    session.commit()
    scan_id = str(row.id)
    session.close()

    out = bt.run_robustness_scan_task(scan_id)
    assert out["ok"] is True and out["signals"] == 3

    chk = TestSession()
    done = chk.get(RobustnessScan, row.id)
    chk.expire_on_commit = False
    assert done.status == "DONE"
    s = done.summary
    assert s["n_trials"] == 12
    assert s["n_signals"] == 3
    assert s["matrix_rows"] == 2          # FLAT open 被剔除
    assert s["excluded_n"] == 1
    assert s["pbo"] is None               # 2 行 < 8×5 → insufficient
    assert len(s["variants"]) == 12
    v1 = s["variants"][0]
    assert v1["decided"] == 2
    assert v1["win_rate"] == 0.5          # rise 赢 + fall 输
    assert v1["dsr"] is not None
    assert s["conclusion"]["verdict"] == "insufficient"
    assert s["best_is"] in {v["name"] for v in s["variants"]}
    # 引擎版本与数据范围落库
    assert s["engine_version"] == "fbe-1"
    assert "RISEUSDT" in s["data_range"]["symbols"]


def test_task_failed_on_empty_sample(env):
    client, TestSession, monkeypatch = env
    monkeypatch.setattr(bt, "_sample_signals",
                        lambda db, pool, source, symbols, days, fwd: ([], {}))
    session = TestSession()
    row = RobustnessScan(params={"source": "scan", "symbols": ["XUSDT"],
                                 "days": 3, "grid": None, "n_blocks": 8})
    session.add(row)
    session.commit()
    scan_id = str(row.id)
    session.close()

    with pytest.raises(Exception):
        bt.run_robustness_scan_task(scan_id)

    chk = TestSession()
    failed = chk.get(RobustnessScan, row.id)
    assert failed.status == "FAILED"
    assert "采样结果为空" in (failed.error or "")
