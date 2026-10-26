# -*- coding: utf-8 -*-
"""命中历史接口回归（/api/scans/hit-history，2026-10-15，docs/22）。

sqlite 内存库 + dependency_overrides 注入会话，不触真库不触网。

覆盖：
1. 默认窗口（最近 7 天）与显式 start/end 全量查询
2. 命中口径=ScanResult 全量；include_repeat=False 过滤重复命中
3. 币种精确匹配
4. 触碰K线反查（pullback_pendings.confirmed_scan_result_id）
5. 开单关联（trade_records.scan_result_id，正式网主单优先）
6. 方向派生（position 优先：support→多/resistance→空；旧行回退 votes_json.net 符号）
7. 信号K线开盘时间派生（signal_created_at 所在整点 - 1h）
8. 服务端分页
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
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.scan import PullbackPending, ScanRecord, ScanResult  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402


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
        yield session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), session
    app.dependency_overrides.pop(get_db, None)
    session.close()


def _mk_result(db, rec_id, symbol, created_at, *, is_repeat=False, net=3):
    r = ScanResult(
        scan_record_id=rec_id, symbol=symbol, signal_type="trigger_pullback_confirm",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.0, quality_score=70, is_repeat=is_repeat,
        pattern="hammer", touch_ref="ema21", position_ref="回踩EMA21",
        votes_json={"net": net, "bull_n": 5, "bear_n": 1, "bull": [], "bear": [], "voted": 6},
        created_at=created_at,
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture()
def seeded(client):
    """造 3 条命中（AAA 近2h 正式网单+触碰 / AAA 近1h 重复命中 / BBB 20天前）、
    1 条触碰K线关联、2 笔开单（同命中：测试网+正式网）"""
    http, db = client
    rec = ScanRecord(scan_type="scheduled")
    db.add(rec)
    db.flush()
    now = datetime.utcnow()

    recent = _mk_result(db, rec.id, "AAAUSDT", now - timedelta(hours=2))
    repeat = _mk_result(db, rec.id, "AAAUSDT", now - timedelta(hours=1),
                        is_repeat=True, net=-4)
    old = _mk_result(db, rec.id, "BBBUSDT", now - timedelta(days=20))

    touch = now - timedelta(hours=3)
    db.add(PullbackPending(
        symbol="AAAUSDT", direction=1, stage1_candle_open=touch,
        status="confirmed", confirmed_scan_result_id=recent.id,
    ))
    t_test = TradeRecord(
        symbol="AAAUSDT", direction="long", scan_result_id=recent.id,
        status="CLOSED", testnet=True, entry_price=100.0, stop_loss=98.0, tp1=105.0,
        realized_pnl=5.0, opened_at=now - timedelta(hours=1, minutes=30),
        closed_at=now, created_at=now,
    )
    t_live = TradeRecord(
        symbol="AAAUSDT", direction="long", scan_result_id=recent.id,
        status="OPENED", testnet=False, entry_price=100.5, stop_loss=100.0,  # 现值=已上移的止损
        created_at=now + timedelta(seconds=1),
    )
    db.add_all([t_test, t_live])
    db.flush()
    # OPEN 事件快照：初始止损 98（rec.stop_loss 已被 SL_MOVE 改成 100）
    db.add(TradeEvent(
        trade_record_id=t_live.id, event_type="OPEN",
        detail={"entry_price": 100.5, "stop_loss": 98.0, "tp1": 105.0},
        created_at=now,
    ))
    db.commit()
    return {"recent": recent, "repeat": repeat, "old": old, "touch": touch, "t_live": t_live}


def test_default_window_excludes_old(client, seeded):
    """默认窗口=最近 7 天：20 天前的命中不返回；窗口内两条重复命中全展示"""
    http, _ = client
    res = http.get("/api/scans/hit-history").json()
    assert res["total"] == 2
    symbols = [it["symbol"] for it in res["items"]]
    assert symbols == ["AAAUSDT", "AAAUSDT"]  # created_at 倒序：1h 前 → 2h 前
    assert all(it["is_repeat"] is (i == 0) for i, it in enumerate(res["items"]))


def test_all_time_returns_full_history(client, seeded):
    """all_time=true（前端清空日期）= 不限下界：与默认 7 天窗口对照，返回全量 3 条"""
    http, _ = client
    res = http.get("/api/scans/hit-history", params={"all_time": True}).json()
    assert res["total"] == 3
    # 上界仍生效：end 早于全部命中时按 end 过滤（默认 end=now，此处显式收窄验证）
    end = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
    res2 = http.get("/api/scans/hit-history",
                    params={"all_time": True, "end": end}).json()
    assert res2["total"] == 0


def test_explicit_range_returns_all(client, seeded):
    """显式 start/end（带时区 ISO）覆盖全量：三条命中"""
    http, _ = client
    now = datetime.utcnow()
    start = (now - timedelta(days=30)).isoformat() + "Z"
    end = (now + timedelta(minutes=1)).isoformat() + "Z"
    res = http.get("/api/scans/hit-history", params={"start": start, "end": end}).json()
    assert res["total"] == 3


def test_include_repeat_and_symbol_filter(client, seeded):
    """include_repeat=False 过滤重复命中；symbol 精确匹配"""
    http, _ = client
    res = http.get("/api/scans/hit-history", params={"include_repeat": False}).json()
    assert res["total"] == 1
    assert res["items"][0]["id"] == str(seeded["recent"].id)

    res2 = http.get("/api/scans/hit-history", params={"symbol": "BBBUSDT",
                                                      "start": (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"}).json()
    assert res2["total"] == 1 and res2["items"][0]["symbol"] == "BBBUSDT"


def test_touch_and_trade_association(client, seeded):
    """触碰K线反查 + 开单关联（正式网主单优先）+ 方向派生"""
    http, _ = client
    res = http.get("/api/scans/hit-history", params={
        "start": (datetime.utcnow() - timedelta(hours=6)).isoformat() + "Z"}).json()
    by_id = {it["id"]: it for it in res["items"]}

    recent = by_id[str(seeded["recent"].id)]
    # 触碰K线来自 pullback_pendings.stage1_candle_open
    assert recent["stage1_candle_open"] == seeded["touch"].isoformat()
    # 方向派生：net=3 → 多
    assert recent["direction"] == 1
    # 开单取正式网主单
    assert recent["trade"]["trade_id"] == str(seeded["t_live"].id)
    assert recent["trade"]["testnet"] is False
    assert recent["trade"]["status"] == "OPENED"
    # 止损双口径：stop_loss=OPEN 事件快照（初始 98）、current_stop_loss=rec 现值（移动后 100）
    assert recent["trade"]["stop_loss"] == 98.0
    assert recent["trade"]["current_stop_loss"] == 100.0

    # 重复命中方向：net=-4 → 空；无触碰/无开单
    repeat = by_id[str(seeded["repeat"].id)]
    assert repeat["direction"] == -1
    assert repeat["stage1_candle_open"] is None
    assert repeat["trade"] is None


def test_direction_prefers_position_and_signal_candle_open(client):
    """方向派生 position 优先（逆方向票也按 position）+ 信号K线开盘时间派生

    回归线上 RENDERUSDT 空信号被 votes.net 标成看多的问题（2026-10-26）：
    信号根 09:00–10:00（北京）看跌吞没、position=resistance，但 1h 方向票净票为正。
    """
    http, db = client
    rec = ScanRecord(scan_type="scheduled")
    db.add(rec)
    db.flush()
    ts = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

    # resistance + 方向票偏多（net=2）→ 仍看空
    bear = _mk_result(db, rec.id, "RENDERUSDT", ts, net=2)
    bear.position = "resistance"
    bear.signal_created_at = ts
    # support + 方向票偏空（net=-4）→ 看多
    bull = _mk_result(db, rec.id, "AAAUSDT", ts - timedelta(minutes=1), net=-4)
    bull.position = "support"
    bull.signal_created_at = ts - timedelta(minutes=1)
    # 旧行：position 为空 → 回退 votes.net
    legacy = _mk_result(db, rec.id, "BBBUSDT", ts - timedelta(minutes=2), net=-4)
    db.commit()

    res = http.get("/api/scans/hit-history", params={
        "start": (ts - timedelta(days=1)).isoformat() + "Z",
        "end": (ts + timedelta(minutes=1)).isoformat() + "Z",
    }).json()
    by_id = {it["id"]: it for it in res["items"]}

    assert by_id[str(bear.id)]["direction"] == -1
    assert by_id[str(bull.id)]["direction"] == 1
    assert by_id[str(legacy.id)]["direction"] == -1
    # 信号K线 = 扫描时刻所在整点 - 1h（最后一根已收盘根）
    assert by_id[str(bear.id)]["signal_candle_open"] == (ts - timedelta(hours=1)).isoformat()
    assert by_id[str(bull.id)]["signal_candle_open"] == (ts - timedelta(hours=2)).isoformat()


def test_pagination(client, seeded):
    """服务端分页：total 不受分页影响，两页各返回 1 条且不重复"""
    http, _ = client
    res = http.get("/api/scans/hit-history", params={
        "start": (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z",
        "page": 1, "page_size": 1}).json()
    assert res["total"] == 3
    assert len(res["items"]) == 1
    assert res["page"] == 1 and res["page_size"] == 1

    res2 = http.get("/api/scans/hit-history", params={
        "start": (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z",
        "page": 2, "page_size": 1}).json()
    assert len(res2["items"]) == 1
    assert res2["items"][0]["id"] != res["items"][0]["id"]
