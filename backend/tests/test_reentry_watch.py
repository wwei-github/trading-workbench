# -*- coding: utf-8 -*-
"""止损后再入场检测与提醒（docs/需求文档/24，2026-10-31）：单测。

覆盖（spec §7）：
1. compute_structure：强多头 → structure_ok；同数据给空头 → 不对称（ema_ok=False）；
   不足 66 根 → None
2. register_reentry_watches：正式网 sl 单 + 结构 OK → 1 行 watching（θ=0，
   expires_at=closed_at+24h）；测试网/非 sl/excluded → 0；结构已破 → 1 行
   structure_broken；同一 source_trade_id 幂等；无 OPEN 止损快照 → 跳过
3. sweep_reentry_watches：触发（仅发 1 张卡、note 含免责声明、重跑不重发）/
   到期 ttl / 同币在跑仓 open_position / 结构破坏 structure_break /
   取价异常保持 watching / triggered 未通知下轮补发
4. API：GET 字段（seconds_left + 结构展平）；POST dismiss 正常置终态、终态再调 400
5. 回归：不产生 TradeRecord、不动 RULE_VERSION

sqlite 内存库 + StaticPool；行情/下单/飞书全部替身，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.trade import ReentryWatch, TradeEvent, TradeRecord  # noqa: E402
from app.services import binance_trader as bt_mod  # noqa: E402
from app.services import exchange_pool as ep_mod  # noqa: E402
from app.services import notify as notify_mod  # noqa: E402
from app.services.reentry import (  # noqa: E402
    BAR_MS,
    CARD_DISCLAIMER,
    compute_structure,
    register_reentry_watches,
    sweep_reentry_watches,
)

# 固定时钟：所有时间断言不依赖当前机器时间
NOW = datetime(2026, 10, 31, 12, 0, 0)
CLOSED_AT = NOW - timedelta(hours=2)

_OK_STRUCTURE = {
    "ema_ok": True, "ema144_used": True, "adx": 28.4,
    "adx_ok": True, "r2": 0.62, "r2_ok": True, "structure_ok": True,
}


# ── 替身与数据构造 ────────────────────────────────────────────────────


def _make_klines(closed_at: datetime, trend: str = "up", n_pre: int = 160) -> list[list]:
    """平仓前 n_pre 根 1h K 线：up=单边上行（结构完好）；range=震荡（结构已破）。"""
    t0_ms = int((closed_at.replace(tzinfo=None) - timedelta(hours=n_pre)).timestamp() * 1000)
    t0_ms = t0_ms // BAR_MS * BAR_MS
    kls, prev = [], 100.0
    for i in range(n_pre):
        c = 100.0 + i * 0.10 if trend == "up" else 100.0 + 4 * (i % 2 * 2 - 1)
        hi, lo = c + 0.3, c - 0.3
        kls.append([t0_ms + i * BAR_MS, prev, hi, lo, c, 100.0])
        prev = c
    return kls


class _FakePool:
    """ExchangePool 替身：get_klines_range / get_klines 返回同一组 K 线。"""

    def __init__(self, klines: list[list]):
        self.klines = klines

    def get_klines_range(self, symbol, interval, limit, end_time_ms):
        return self.klines

    def get_klines(self, symbol, interval, limit):
        return self.klines


def _add_closed_trade(s, tid, symbol, *, direction="long", entry=100.0, stop=95.0,
                      closed_at=CLOSED_AT, exit_reason="sl", testnet=False,
                      excluded=False, with_event=True):
    s.add(TradeRecord(
        id=tid, symbol=symbol, direction=direction, entry_price=entry,
        qty=10.0, risk_amount=100.0, stop_loss=stop, tp1=111.0,
        status="CLOSED", exit_reason=exit_reason, realized_pnl=-55.0,
        opened_at=closed_at - timedelta(hours=160), closed_at=closed_at,
        testnet=testnet, excluded=excluded,
    ))
    if with_event:
        s.add(TradeEvent(trade_record_id=tid, event_type="OPEN",
                         detail={"stop_loss": stop}))


def _add_watch(s, *, symbol="BTCUSDT", direction="long", status="watching",
               entry=100.0, stop=95.0, trigger=100.0,
               expires_at=NOW + timedelta(hours=10), structure=None,
               notified_at=None, triggered_at=None):
    w = ReentryWatch(
        source_trade_id=uuid.uuid4(), symbol=symbol, direction=direction,
        account_key=None, testnet=False, rule_version="v2026-10-30",
        entry_price=entry, stop_initial=stop, r_unit=abs(entry - stop),
        theta_r=0, trigger_price=trigger, status=status,
        structure_json=structure or dict(_OK_STRUCTURE),
        notified_at=notified_at, triggered_at=triggered_at,
        expires_at=expires_at,
    )
    s.add(w)
    return w


def _patch_sweep(monkeypatch, *, price=100.0, klines=None, price_raises=False):
    """替身行情/飞书：返回 (发送记录列表, price 调用记录)。"""
    sent: list[dict] = []
    prices: list[str] = []

    def _send(card, *, testnet=False):
        sent.append(card)
        return True

    def _price(self, symbol):
        prices.append(symbol)
        if price_raises:
            raise RuntimeError("ticker boom")
        return price

    monkeypatch.setattr(notify_mod, "send_feishu_card", _send)
    monkeypatch.setattr(bt_mod.BinanceTrader, "price", _price)
    kl = klines if klines is not None else _make_klines(NOW, "up")
    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(kl))
    return sent, prices


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


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


# ── 1. compute_structure ─────────────────────────────────────────────


def test_compute_structure_long_ok():
    """强上行 160 根：EMA 排列 / ADX≥20 / R²≥0.5 三件全过 → structure_ok"""
    v = compute_structure(_make_klines(NOW, "up"), "long")
    assert v is not None
    assert v["ema_ok"] is True and v["adx_ok"] is True and v["r2_ok"] is True
    assert v["structure_ok"] is True
    assert v["ema144_used"] is True  # 160 根 ≥ 144，e144 可用
    assert v["adx"] >= 20 and v["r2"] >= 0.5


def test_compute_structure_short_asymmetric():
    """同一组强上行数据给空头：EMA 排列不成立 → ema_ok=False（方向不对称）"""
    v = compute_structure(_make_klines(NOW, "up"), "short")
    assert v is not None
    assert v["ema_ok"] is False
    assert v["structure_ok"] is False


def test_compute_structure_insufficient_bars():
    """少于 66 根：数据不可判 → None"""
    assert compute_structure(_make_klines(NOW, "up", n_pre=60), "long") is None
    assert compute_structure([], "long") is None


# ── 2. register_reentry_watches ──────────────────────────────────────


def test_register_creates_watching(db, monkeypatch):
    """正式网 CLOSED+sl+结构 OK → 1 行 watching，trigger_price==entry（θ=0），
    expires_at==closed_at+24h；不产生新的 TradeRecord"""
    tid = uuid.uuid4()
    _add_closed_trade(db, tid, "BTCUSDT")
    db.commit()
    before = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_make_klines(CLOSED_AT, "up")))
    assert register_reentry_watches(db, now=NOW) == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.source_trade_id == tid
    assert row.status == "watching"
    assert row.testnet is False
    assert float(row.entry_price) == pytest.approx(100.0)
    assert float(row.stop_initial) == pytest.approx(95.0)
    assert float(row.r_unit) == pytest.approx(5.0)
    assert float(row.trigger_price) == pytest.approx(100.0)  # θ=0 → 回到开仓价
    assert row.expires_at == CLOSED_AT + timedelta(hours=24)
    assert row.structure_json["structure_ok"] is True
    assert row.resolve_reason is None and row.resolved_at is None

    after = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()
    assert after == before


def test_register_filters_testnet_reason_and_excluded(db, monkeypatch):
    """测试网单 / 非 sl 单 / excluded 单 → 0 行"""
    _add_closed_trade(db, uuid.uuid4(), "TUSDT", testnet=True)
    _add_closed_trade(db, uuid.uuid4(), "RUSDT", exit_reason="tp1_trail")
    _add_closed_trade(db, uuid.uuid4(), "XUSDT", excluded=True)
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_make_klines(CLOSED_AT, "up")))
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0


def test_register_structure_broken_at_close(db, monkeypatch):
    """结构已破 → 直接落 structure_broken 终态（供页面解释为何没提醒）"""
    _add_closed_trade(db, uuid.uuid4(), "RANGEUSDT")
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_make_klines(CLOSED_AT, "range")))
    assert register_reentry_watches(db, now=NOW) == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "structure_broken"
    assert row.resolve_reason == "structure_broken_at_close"
    assert row.resolved_at == NOW
    assert row.structure_json["structure_ok"] is False


def test_register_idempotent(db, monkeypatch):
    """同一 source_trade_id 连跑两次 → 仍只有 1 行（唯一约束 + 预查）"""
    _add_closed_trade(db, uuid.uuid4(), "BTCUSDT")
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_make_klines(CLOSED_AT, "up")))
    assert register_reentry_watches(db, now=NOW) == 1
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 1


def test_register_skips_without_open_stop_snapshot(db, monkeypatch):
    """无 OPEN 事件止损快照 → R 空间不可算，跳过不落行"""
    _add_closed_trade(db, uuid.uuid4(), "NOSNAPUSDT", with_event=False)
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_make_klines(CLOSED_AT, "up")))
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0


# ── 3. sweep_reentry_watches ─────────────────────────────────────────


def test_sweep_triggers_and_notifies_once(db, monkeypatch):
    """价格到达触发价 → triggered；只发 1 张卡且 note 含免责声明；重跑不重发"""
    w = _add_watch(db, trigger=100.0)
    db.commit()
    sent, _prices = _patch_sweep(monkeypatch, price=100.0)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 1

    db.refresh(w)
    assert w.status == "triggered"
    assert w.triggered_at == NOW
    assert w.notified_at is not None
    assert float(w.current_price) == pytest.approx(100.0)

    assert len(sent) == 1
    note = sent[0]["elements"][-1]
    assert note["elements"][0]["content"] == CARD_DISCLAIMER

    # 再跑一轮：已通知 → 不再发送
    counts2 = sweep_reentry_watches(db, now=NOW)
    assert counts2["triggered"] == 0
    assert len(sent) == 1


def test_sweep_expired_ttl(db, monkeypatch):
    """未触发且已过窗口 → expired / ttl"""
    _add_watch(db, trigger=100.0, expires_at=NOW - timedelta(hours=1))
    db.commit()
    sent, _prices = _patch_sweep(monkeypatch, price=90.0)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["expired"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "expired"
    assert row.resolve_reason == "ttl"
    assert row.resolved_at == NOW
    assert sent == []


def test_sweep_superseded_open_position(db, monkeypatch):
    """同 symbol 同账号已有在跑仓 → superseded / open_position"""
    _add_watch(db, symbol="ETHUSDT", trigger=100.0,
               expires_at=NOW + timedelta(hours=10))
    db.add(TradeRecord(
        symbol="ETHUSDT", direction="long", status="OPENED", testnet=False,
        entry_price=100.0, qty=1.0, opened_at=NOW - timedelta(hours=1),
    ))
    db.commit()
    sent, _prices = _patch_sweep(monkeypatch, price=90.0)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["superseded"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "superseded"
    assert row.resolve_reason == "open_position"
    assert row.resolved_at == NOW
    assert sent == []


def test_sweep_structure_broken(db, monkeypatch):
    """最新已收盘 1h 结构破坏 → structure_broken / structure_break"""
    _add_watch(db, trigger=100.0, expires_at=NOW + timedelta(hours=10))
    db.commit()
    sent, _prices = _patch_sweep(
        monkeypatch, price=90.0, klines=_make_klines(NOW, "range"))

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["broken"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "structure_broken"
    assert row.resolve_reason == "structure_break"
    assert row.resolved_at == NOW
    assert row.structure_json["structure_ok"] is False
    assert sent == []


def test_sweep_price_error_keeps_watching(db, monkeypatch):
    """取价异常 → 保持 watching，状态/巡检字段不变"""
    w = _add_watch(db, trigger=100.0, expires_at=NOW + timedelta(hours=10))
    db.commit()
    sent, prices = _patch_sweep(monkeypatch, price_raises=True)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0 and counts["expired"] == 0
    assert counts["broken"] == 0 and counts["superseded"] == 0
    assert prices == ["BTCUSDT"]  # 确实尝试取价

    db.refresh(w)
    assert w.status == "watching"
    assert w.current_price is None
    assert w.last_checked_at is None
    assert sent == []


def test_sweep_retries_notification_for_triggered(db, monkeypatch):
    """已 triggered 但 notified_at 为空（上轮发送失败）→ 本轮补发且仅 1 次"""
    _add_watch(db, status="triggered", trigger=100.0,
               triggered_at=NOW - timedelta(minutes=1), notified_at=None,
               expires_at=NOW + timedelta(hours=10))
    db.commit()
    sent, _prices = _patch_sweep(monkeypatch, price=100.0)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0  # 状态不变，只是补发
    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "triggered"
    assert row.notified_at is not None
    assert len(sent) == 1
    assert sent[0]["elements"][-1]["elements"][0]["content"] == CARD_DISCLAIMER


# ── 4. API ───────────────────────────────────────────────────────────


def test_api_list_seconds_left_and_structure(client):
    """GET 列表：watching 行 seconds_left>0、终态 0；结构字段展平可读"""
    http, s = client
    w_live = _add_watch(s, symbol="BTCUSDT", trigger=100.0,
                        expires_at=datetime(2030, 1, 1))
    w_done = _add_watch(s, symbol="ETHUSDT", status="expired", trigger=100.0,
                        expires_at=datetime(2030, 1, 1))
    w_done.resolve_reason = "ttl"
    s.commit()

    r = http.get("/api/trades/reentry-watches")
    assert r.status_code == 200, r.text
    data = {x["id"]: x for x in r.json()}

    live = data[str(w_live.id)]
    assert live["status"] == "watching"
    assert live["seconds_left"] > 0
    assert live["ema_ok"] is True and live["ema144_used"] is True
    assert live["adx"] == pytest.approx(28.4)
    assert live["adx_ok"] is True
    assert live["r2"] == pytest.approx(0.62)
    assert live["r2_ok"] is True
    assert live["structure_ok"] is True

    done = data[str(w_done.id)]
    assert done["seconds_left"] == 0
    assert done["status"] == "expired"
    assert done["resolve_reason"] == "ttl"


def test_api_dismiss_and_terminal_400(client):
    """POST dismiss：watching → dismissed/manual；终态再调 → 400"""
    http, s = client
    w = _add_watch(s, symbol="SOLUSDT", trigger=100.0,
                   expires_at=datetime(2030, 1, 1))
    s.commit()

    r = http.post(f"/api/trades/reentry-watches/{w.id}/dismiss")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "dismissed"

    dbrow = s.get(ReentryWatch, w.id)
    s.refresh(dbrow)
    assert dbrow.status == "dismissed" and dbrow.resolve_reason == "manual"
    assert dbrow.resolved_at is not None

    # 终态再调 → 400
    r2 = http.post(f"/api/trades/reentry-watches/{w.id}/dismiss")
    assert r2.status_code == 400
    assert r2.json()["detail"] == "该监控已结束"


# ── 5. 回归 ──────────────────────────────────────────────────────────


def test_regression_no_trade_and_rule_version(db, monkeypatch):
    """register + sweep 前后 TradeRecord 行数不变；RULE_VERSION 只读不变"""
    rv_before = settings.RULE_VERSION
    _add_closed_trade(db, uuid.uuid4(), "BTCUSDT")
    db.commit()
    before = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()

    _patch_sweep(monkeypatch, price=100.0,
                 klines=_make_klines(CLOSED_AT, "up"))
    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["registered"] == 1                # 注册路径真的跑到了
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 1

    after = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()
    assert after == before
    assert settings.RULE_VERSION == rv_before
