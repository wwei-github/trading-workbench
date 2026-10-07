# -*- coding: utf-8 -*-
"""虚拟账本周期任务单测（docs/16 P-07 步骤3）。

覆盖：推进任务终局落库（loss / be_exit）、未终局保持 OPEN、账龄超期 EXPIRED
（outcome/r 保持 NULL）、AI 标注 sweep 补挂、回填去重。
K 线喂构造数据（monkeypatch ledger_tasks._load），sqlite 内存库。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.tasks.ledger_tasks as lt  # noqa: E402
from app.models.ledger import VirtualLedger  # noqa: E402
from app.models.scan import AIAnalysis, ScanResult  # noqa: E402

HOUR_MS = 3_600_000
_T0 = datetime(2026, 9, 1, 0, 0, 0)
T0_MS = int((_T0 - datetime(1970, 1, 1)).total_seconds() * 1000)


def _kl(*specs, t0_ms=None):
    """1h K 线：spec=(high, low, close?)，open_time 从入场锚起。"""
    base = t0_ms if t0_ms is not None else T0_MS + HOUR_MS  # 默认=入场锚
    out = []
    for i, sp in enumerate(specs):
        hi, lo = float(sp[0]), float(sp[1])
        c = float(sp[2]) if len(sp) > 2 else (hi + lo) / 2
        out.append([base + i * HOUR_MS, (hi + lo) / 2, hi, lo, c, 100.0])
    return out


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(
        bind=engine,
        tables=[VirtualLedger.__table__, ScanResult.__table__, AIAnalysis.__table__],
    )
    # 任务体内 `from app.database import SessionLocal` 延迟导入 → patch 源模块
    import app.database as database_mod
    monkeypatch.setattr(database_mod, "SessionLocal", lambda: Session(bind=engine))
    monkeypatch.setattr(lt, "open_task", lambda *a, **k: "t1")
    closed = {}
    monkeypatch.setattr(lt, "close_task",
                        lambda tid, status, **k: closed.setdefault(tid, status))
    return Session(bind=engine)


def _row(db, **kw):
    defaults = dict(
        symbol="BTCUSDT", direction="long", source="live",
        signal_type="trigger_pullback_confirm", quality_score=70,
        entry_price=100.0, stop_price=99.25, tp1_price=101.125,
        tp2_price=102.5, r_unit=0.75, entry_bar_open=_T0,
        status="OPEN",
    )
    defaults.update(kw)
    r = VirtualLedger(**defaults)
    db.add(r)
    db.commit()
    return r


def _run(db, kl_1h, kl_d1=None, monkeypatch=None):
    monkeypatch.setattr(lt, "_load",
                        lambda d, p, sym, itv, s, e: kl_1h if itv == "1h" else (kl_d1 or []))
    monkeypatch.setattr(lt, "ExchangePool", lambda: object())
    return lt.run_ledger_advance_task()


def test_advance_loss(db, monkeypatch):
    r = _row(db)
    _run(db, _kl((100.4, 99.0, 99.1)), monkeypatch=monkeypatch)  # 首根深损
    db.refresh(r)
    assert r.status == "DONE" and r.outcome == "loss"
    assert r.r is not None and -1.14 < r.r < -1.0  # 全损含费 ≈ -1.13R
    assert r.decided_at is not None and r.result_json["legs"]


def test_advance_tp1_then_tighter_trail(db, monkeypatch):
    """TP1 触发 → 次根摆动跟进（比保本更紧）截住剩余仓 → win_tp1（生产语义）"""
    r = _row(db)
    _run(db, _kl((101.3, 100.2, 100.6),   # 触 TP1=101.125（50% 落袋+武装）
                 (100.4, 99.9, 100.0)),   # 次根回踩：摆动轨 100.1 先于保本成交
         monkeypatch=monkeypatch)
    db.refresh(r)
    assert r.status == "DONE" and r.outcome == "win_tp1"
    assert r.r is not None and r.r > 0  # TP1 半仓利润 + 跟进腿保本附近


def test_advance_stays_open(db, monkeypatch):
    """未终局保持 OPEN——用近期入场（否则账龄>30 天会正确地 EXPIRED）"""
    recent = datetime.utcnow() - timedelta(hours=2)
    recent_ms = int((recent - datetime(1970, 1, 1)).total_seconds() * 1000)
    r = _row(db, entry_bar_open=recent)
    _run(db, _kl((100.4, 99.6, 100.0), (100.5, 99.7, 100.1),
                 t0_ms=recent_ms + HOUR_MS),
         monkeypatch=monkeypatch)  # 窄幅未触任何位
    db.refresh(r)
    assert r.status == "OPEN" and r.outcome is None and r.r is None


def test_advance_expired_old_row(db, monkeypatch):
    """账龄 > 30 天且有数据佐证仍 open → EXPIRED（不进 decided 分母）"""
    old = _T0 - timedelta(days=31)
    old_ms = int((old - datetime(1970, 1, 1)).total_seconds() * 1000)
    r = _row(db, entry_bar_open=old)
    _run(db, _kl((100.4, 99.6, 100.0), t0_ms=old_ms + HOUR_MS),
         monkeypatch=monkeypatch)
    db.refresh(r)
    assert r.status == "EXPIRED"
    assert r.outcome is None and r.r is None and r.decided_at is not None


def test_advance_no_bars_leaves_open(db, monkeypatch):
    """K线拉取失败（bars 空）不误判 EXPIRED，保持 OPEN 下轮再推"""
    old = _T0 - timedelta(days=31)
    r = _row(db, entry_bar_open=old)
    _run(db, [], monkeypatch=monkeypatch)
    db.refresh(r)
    assert r.status == "OPEN"


def test_sweep_ai_labels(db, monkeypatch):
    r = _row(db, scan_result_id=uuid.uuid4())  # NULL join 不了 AIAnalysis
    db.add(AIAnalysis(symbol="BTCUSDT", scan_result_id=r.scan_result_id,
                      trade_decision="skip", recommendation=40.0))
    db.commit()
    _run(db, _kl((100.4, 99.0, 99.1)), monkeypatch=monkeypatch)
    db.refresh(r)
    assert r.ai_decision == "skip" and r.ai_recommendation == 40.0


def test_backfill_dedup(db):
    """源B 回填：插入 + 同键跳过（可重入）+ 与 live 行同键跳过"""
    sig = {
        "symbol": "ETHUSDT", "direction": "long",
        "entry": 100.0, "stop": 99.25, "tp1": 101.125, "tp2": 102.5,
        "r_unit": 0.75,
        "entry_time_ms": T0_MS + HOUR_MS,  # 收盘即入场 → entry_bar_open=_T0
        "meta": {"src": "scan", "signal_type": "trigger_pullback_confirm",
                 "touch_ref": "ema21", "quality_score": 75, "votes_net": 4},
    }
    assert lt.insert_signal(db, sig) is True
    assert lt.insert_signal(db, sig) is False  # 回填重跑同键跳过
    row = db.query(VirtualLedger).one()
    assert row.source == "backfill" and row.scan_result_id is None
    assert row.tp2_price == 102.5 and row.quality_score == 75
    # 与 live 行同 (symbol, signal_type, entry_bar_open) → 跳过保留先到行
    _row(db, symbol="BTCUSDT")  # live 行 entry_bar_open=_T0
    sig_live = dict(sig, symbol="BTCUSDT")
    assert lt.insert_signal(db, sig_live) is False
    assert db.query(VirtualLedger).count() == 2
