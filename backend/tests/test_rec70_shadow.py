# -*- coding: utf-8 -*-
"""评分门槛前向对照影子（docs/14 §七 CARD-09，2026-10-07）：rec70_shadow 单测。

覆盖：
1. _rec70_stats：空组 / 胜率与 avg_r 读数（win=pnl>0、risk 缺失行不计 avg_r）
2. _tag_rec70（经 tag_trade_close）：ai_snapshot.recommendation 分桶
   （65→rec60_69 / 75→rec70plus）、无快照与 <60 跳过、testnet 进 decision_json、
   幂等覆盖、与 CARD-05/06 K 线打标事务独立（K 线全失败不影响 rec70 落标）
3. build_report CARD-09 分支：前向窗口过滤（REC70_START 前平仓不计入）、
   正式网主判定/测试网分层、avg_r 读取、sufficient 判定
4. registry：CARD-09 注册元数据、compute 占位恒 None

sqlite 内存库 + StaticPool，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, __file__.rsplit("/", 2)[0])

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.shadow import ShadowMark  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import shadow as shadow_mod  # noqa: E402
from app.services.shadow import REC70_START, _rec70_stats  # noqa: E402


# ── 1. 统计函数 ───────────────────────────────────────────────────────


def test_rec70_stats_empty_and_rounding():
    assert _rec70_stats([]) == {"n": 0, "counted": 0, "win_rate": None, "avg_r": None}
    rows = [
        {"win": True, "r": 1.5},
        {"win": False, "r": -0.5},
        {"win": False, "r": None},   # risk 缺失：不计 avg_r 分母
        {"win": False},              # r 键缺失：同上
    ]
    st = _rec70_stats(rows)
    assert st["n"] == 4 and st["counted"] == 4
    assert st["win_rate"] == pytest.approx(0.25)
    assert st["avg_r"] == pytest.approx(0.5)  # (1.5-0.5)/2


# ── 2. 打标 ──────────────────────────────────────────────────────────


class _FakePool:
    """get_klines_range 恒空：CARD-05/06 分支静默跳过（证 rec70 事务独立）"""

    def get_klines_range(self, *a, **k):
        return []


@pytest.fixture()
def shadow_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(shadow_mod, "SessionLocal", maker, raising=False)
    from app import database as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", maker, raising=False)
    from app.services import exchange_pool as ep_mod
    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool())
    return maker


def _add_trade(s, tid, sym="OKUSDT", testnet=False, rec_val=None,
               pnl=-55.0, risk=100.0, closed=None):
    snap = {"recommendation": rec_val} if rec_val is not None else None
    s.add(TradeRecord(
        id=tid, symbol=sym, direction="long", entry_price=100.0,
        qty=10.0, risk_amount=risk, stop_loss=95.0, tp1=111.0,
        status="CLOSED", exit_reason="sl", realized_pnl=pnl,
        opened_at=(closed or datetime(2026, 10, 9, 10, 0)) - timedelta(hours=5),
        closed_at=closed or datetime(2026, 10, 9, 10, 0),
        testnet=testnet, ai_snapshot=snap,
    ))


def test_tag_trade_close_rec70_bucketing(shadow_db):
    """分桶：65→rec60_69 / 75→rec70plus；无快照与 <60 跳过；
    testnet 进 decision_json；value_json 存 rec/r/win/closed_at"""
    maker = shadow_db
    s = maker()
    t_65, t_75, t_nosnap, t_low = (uuid.uuid4() for _ in range(4))
    _add_trade(s, t_65, rec_val=65)
    _add_trade(s, t_75, rec_val=75, pnl=120.0)
    _add_trade(s, t_nosnap, rec_val=None)
    _add_trade(s, t_low, rec_val=48)  # 开仓闸下不该出现，出现也不打
    s.commit()
    s.close()

    assert shadow_mod.tag_trade_close(list(maker().execute(
        select(TradeRecord)).scalars())) == 2

    db = maker()
    marks = {m.ref_id: m for m in db.execute(
        select(ShadowMark).where(
            ShadowMark.feature_key == "rec70_shadow")).scalars()}
    assert set(marks) == {t_65, t_75}
    v65 = marks[t_65].value_json
    assert v65["bucket"] == "rec60_69" and v65["recommendation"] == 65.0
    assert v65["r"] == -0.55 and v65["win"] is False
    assert marks[t_65].decision_json["testnet"] is False
    v75 = marks[t_75].value_json
    assert v75["bucket"] == "rec70plus" and v75["r"] == 1.2 and v75["win"] is True
    assert marks[t_75].symbol == "OKUSDT"
    db.close()


def test_tag_trade_close_rec70_testnet_and_idempotent(shadow_db):
    """测试网 testnet=True 落 decision_json；重跑覆盖不新增行（uq 幂等）"""
    maker = shadow_db
    s = maker()
    tid = uuid.uuid4()
    _add_trade(s, tid, sym="TNUSDT", testnet=True, rec_val=62, pnl=-30.0)
    s.commit()
    s.close()
    trades = list(maker().execute(select(TradeRecord)).scalars())

    assert shadow_mod.tag_trade_close(trades) == 1
    assert shadow_mod.tag_trade_close(trades) == 1  # 幂等：覆盖非新增

    db = maker()
    rows = list(db.execute(select(ShadowMark)).scalars())
    assert len(rows) == 1
    assert rows[0].decision_json["testnet"] is True
    db.close()


# ── 3. build_report CARD-09 分支 ─────────────────────────────────────


def _mark(s, tid, symbol, testnet, rec_val, pnl, closed_day):
    win = pnl > 0
    s.add(ShadowMark(
        feature_key="rec70_shadow", ref_type="trade_record",
        ref_id=tid, symbol=symbol,
        value_json={
            "recommendation": rec_val,
            "bucket": "rec60_69" if rec_val < 70 else "rec70plus",
            "r": pnl / 100.0, "win": win,
            "closed_at": f"{closed_day}T10:00:00",
        },
        decision_json={"stage": "trade_close", "testnet": testnet},
    ))


def test_build_report_rec70(shadow_db):
    """前向窗口过滤（REC70_START 前不计入）+ 正式网/测试网分层 + avg_r 读数"""
    maker = shadow_db
    s = maker()
    # 正式网 rec60_69：2 亏（前向窗内）+ 1 笔窗口前（应被过滤）
    _mark(s, uuid.uuid4(), "A1USDT", False, 65, -60.0, "2026-10-09")
    _mark(s, uuid.uuid4(), "A2USDT", False, 68, -40.0, "2026-10-12")
    _mark(s, uuid.uuid4(), "OLDUSDT", False, 66, -70.0, "2026-10-01")  # 窗口前
    # 正式网 rec70plus：1 赢
    _mark(s, uuid.uuid4(), "A3USDT", False, 75, 130.0, "2026-10-11")
    # 测试网对照：窗口内 1 笔 + 窗口前 1 笔
    _mark(s, uuid.uuid4(), "T1USDT", True, 63, -20.0, "2026-10-10")
    _mark(s, uuid.uuid4(), "T0USDT", True, 72, 50.0, "2026-10-02")     # 窗口前
    s.commit()
    s.close()

    report = shadow_mod.build_report(maker(), window_days=30)
    rep = next(r for r in report if r["feature"] == "rec70_shadow")
    assert rep["card"] == "CARD-09" and rep["min_sample"] == 15
    base = rep["baseline"]
    assert base["n"] == 3  # 窗口前 OLDUSDT 被滤掉
    buckets = {b["bucket"]: b for b in rep["buckets"]}
    assert buckets["rec60_69"]["n"] == 2
    assert buckets["rec60_69"]["win_rate"] == 0.0
    assert buckets["rec60_69"]["avg_r"] == pytest.approx(-0.5)  # (-0.6-0.4)/2
    assert buckets["rec70plus"]["n"] == 1
    assert buckets["rec70plus"]["win_rate"] == 1.0
    assert buckets["rec70plus"]["avg_r"] == pytest.approx(1.3)
    assert all(b.get("insufficient") for b in buckets.values())  # n<30
    tbn = rep["testnet_baseline"]
    assert tbn["n"] == 1 and tbn["win_rate"] == 0.0   # T0USDT 窗口前被滤
    assert tbn["buckets"] == {"rec60_69": 1}
    assert rep["sufficient"] is False  # 3 < 15
    # 窗口前存量仍打标（过滤在评估侧）——VERIFIED：marks 共 6 行
    db = maker()
    assert len(list(db.execute(select(ShadowMark)).scalars())) == 6
    db.close()


def test_build_report_rec70_empty_start():
    """REC70_START 常量即预注册前向窗口起点（锁定不可回调）"""
    assert REC70_START == "2026-10-08"


# ── 4. registry ──────────────────────────────────────────────────────


def test_registry_metadata():
    """CARD-09 注册元数据：compute 占位恒 None、min_sample=15"""
    feat = shadow_mod.SHADOW_FEATURES["rec70_shadow"]
    assert feat.card == "CARD-09" and feat.min_sample == 15
    assert feat.compute([]) is None
