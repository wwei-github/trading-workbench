# -*- coding: utf-8 -*-
"""P-08 早停宣告落锁单测（2026-10-09）。

背景：CARD-07 序贯判读（SPRT p0=0.40/p1=0.50，α=0.05/β=0.20）原先每轮
现算不固化——昨天跨过 h1 上界显示「早停」、今天窗口内进来未续走样本 LLR
跌回线下又显示「继续观察」，状态回摆违反预注册语义（首次跨界即终局宣告）。

修复：_early_stop_verdict 落锁封装——首次跨界（h1/h0）写 shadow_early_stops
（card 唯一），此后固定用宣告快照；continue 不落锁继续现算；落锁异常降级
现算不炸评估。

覆盖：
1. helper 级：continue 不落锁 / h1 落锁 / 宣告后短路现算 / 落锁写库失败降级
2. 集成级（build_report + sqlite 夹具）：跨界落锁、宣告后窗口数据翻转仍用
   宣告快照（不回摆）、h0 同样落锁

数值核算（每胜 +ln(1.25)=+0.2231，每负 +ln(0.5/0.6)=−0.4055，
上界 ln(0.8/0.05)=+2.7726，下界 ln(0.2/0.95)=−1.5581）：
- 18胜2负 n=20 → LLR=+3.206 ≥ +2.7726 → h1
-  2胜18负 n=20 → LLR=−6.852 ≤ −1.5581 → h0
-  8胜4负 n=12 → LLR=+0.163 → continue

sqlite 内存库，不触网。
"""
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.database import Base  # noqa: E402
from app.models.shadow import ShadowEarlyStop, ShadowMark  # noqa: E402
from app.services.shadow import _early_stop_verdict, build_report  # noqa: E402

# sprt_bernoulli 引擎返回（p0=0.40/p1=0.50 预注册参数下的三态快照形状）


def _seq(wins: int, n: int) -> dict:
    """现算替身：与 sequential_verdict 同形状（helper 只透传，不重算）"""
    return {"verdict": "h1" if wins >= 18 else ("h0" if wins <= 2 else "continue"),
            "llr": round(wins * 0.22314 - (n - wins) * 0.405465, 4),
            "upper": 2.7726, "lower": -1.5581, "wins": wins, "n": n,
            "p0": 0.40, "p1": 0.50,
            "posterior": {"mean": wins / n, "ci95_low": 0.0, "ci95_high": 1.0, "n": n}}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


# ── 1. helper 级 ─────────────────────────────────────────────────────


def test_continue_not_locked(db):
    """未跨界（continue）→ 返回现算、不写宣告、无 locked 标记"""
    out = _early_stop_verdict(db, "CARD-07", lambda: _seq(8, 12))
    assert out["verdict"] == "continue"
    assert "locked" not in out and "declared_at" not in out
    assert db.query(ShadowEarlyStop).count() == 0


def test_h1_cross_boundary_locks(db):
    """首次跨 h1 上界 → 落宣告（locked + declared_at + detail 快照），DB 一条"""
    out = _early_stop_verdict(db, "CARD-07", lambda: _seq(18, 20))
    assert out["verdict"] == "h1"
    assert out["locked"] is True
    assert out["declared_at"]
    row = db.query(ShadowEarlyStop).one()
    assert row.card == "CARD-07" and row.verdict == "h1"
    assert row.detail["wins"] == 18 and row.detail["n"] == 20


def test_declared_short_circuits_compute(db):
    """已有宣告 → 直接返回宣告快照，现算函数不再被调用（落锁语义核心）"""
    db.add(ShadowEarlyStop(card="CARD-07", verdict="h1",
                           detail={**_seq(18, 20)}))
    db.commit()

    calls: list[int] = []

    def compute():
        calls.append(1)
        return _seq(2, 20)  # 若被调用会给出 h0——宣告后不允许翻

    out = _early_stop_verdict(db, "CARD-07", compute)
    assert calls == []
    assert out["verdict"] == "h1"
    assert out["wins"] == 18 and out["n"] == 20
    assert out["locked"] is True and out["declared_at"]


def test_lock_write_failure_degrades(db, monkeypatch):
    """落锁写库失败 → 降级返回现算读数，评估不炸、不残留半提交数据"""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", _boom)
    out = _early_stop_verdict(db, "CARD-07", lambda: _seq(18, 20))
    assert out["verdict"] == "h1"          # 现算读数照常返回
    assert "locked" not in out             # 但未落锁（下次仍会尝试）
    assert db.query(ShadowEarlyStop).count() == 0


# ── 2. 集成级（build_report 全链路）──────────────────────────────────


def _mark(continued: bool) -> ShadowMark:
    """CARD-07 打标行（正式网：decision_json 无 testnet 标）"""
    return ShadowMark(
        feature_key="reentry_precond", ref_type="trade", ref_id=uuid.uuid4(),
        symbol="TESTUSDT", testnet=False,
        value_json={"bucket": "structure_ok", "continued_24h": continued},
        decision_json={},
    )


def _card07_seq(report: list[dict]) -> dict | None:
    for item in report:
        if item["card"] == "CARD-07":
            return item.get("sequential")
    return None


def test_build_report_locks_and_freezes(db):
    """跨 h1 落锁后，窗口数据翻转也固定用宣告快照（复现并锁死回摆 bug）"""
    for _ in range(18):
        db.add(_mark(True))
    for _ in range(2):
        db.add(_mark(False))
    db.commit()

    first = _card07_seq(build_report(db, window_days=30))
    assert first["verdict"] == "h1" and first["locked"] is True
    assert first["wins"] == 18 and first["n"] == 20
    declared_at = first["declared_at"]
    assert db.query(ShadowEarlyStop).count() == 1

    # 窗口翻转：全部变成未续走（现算必是 h0）——宣告后不得回摆
    for m in db.query(ShadowMark).all():
        m.value_json = {"bucket": "structure_ok", "continued_24h": False}
    db.commit()

    second = _card07_seq(build_report(db, window_days=30))
    assert second["verdict"] == "h1"                    # 仍是宣告方向
    assert second["wins"] == 18 and second["n"] == 20   # 仍是宣告快照
    assert second["declared_at"] == declared_at         # 宣告时刻不变
    assert db.query(ShadowEarlyStop).count() == 1       # 不产生第二条宣告


def test_build_report_h0_locks(db):
    """跨 h0 下界同样落锁（早停 h0 = 卡关闭方向的终局宣告）"""
    for _ in range(2):
        db.add(_mark(True))
    for _ in range(18):
        db.add(_mark(False))
    db.commit()

    seq = _card07_seq(build_report(db, window_days=30))
    assert seq["verdict"] == "h0" and seq["locked"] is True
    row = db.query(ShadowEarlyStop).one()
    assert row.verdict == "h0" and row.detail["wins"] == 2


def test_build_report_continue_stays_live(db):
    """未跨界 → 现算继续观察、不落锁（新样本仍会推进读数）"""
    for _ in range(8):
        db.add(_mark(True))
    for _ in range(4):
        db.add(_mark(False))
    db.commit()

    seq = _card07_seq(build_report(db, window_days=30))
    assert seq["verdict"] == "continue"
    assert "locked" not in seq
    assert db.query(ShadowEarlyStop).count() == 0
