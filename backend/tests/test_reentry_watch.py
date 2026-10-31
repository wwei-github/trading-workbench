# -*- coding: utf-8 -*-
"""止损后再入场检测与提醒（docs/需求文档/24，2026-10-31）：单测。

覆盖（spec §7 + 2026-10-31 增补口径）：
1. compute_structure：强多头 → structure_ok；同数据给空头 → 不对称（ema_ok=False）；
   不足 66 根 → None
2. compute_stop_wick：止损根长下影 → 合格；下影不足 → 不合格；空头上影对称；
   整点边界归前一根；找不到止损根 → None
3. register_reentry_watches：正式网 sl 单 + 影线/结构都过 → 1 行 watching
   （θ=0，expires_at=closed_at+24h）；影线不足 → wick_miss；影线过但结构破 →
   structure_broken；测试网/非 sl/excluded → 0；同一 source_trade_id 幂等；
   无 OPEN 止损快照 → 跳过
4. sweep_reentry_watches：小时收盘触发（多头 high / 空头 low）→ 只发 1 张卡、
   note 含免责声明、重跑不重发；到期 ttl；同币在跑仓 open_position；结构破坏
   structure_break；止损那根不参与触发；窗口外收盘根不认；last_checked_at
   按新根增量补扫；K 线拉取异常保持 watching；triggered 未通知下轮补发
5. API：GET 字段（seconds_left + 结构/影线展平）；POST dismiss 正常置终态、
   终态再调 400
6. 回归：不产生 TradeRecord、不动 RULE_VERSION

sqlite 内存库 + StaticPool；行情/飞书全部替身，不触网、不读 ticker。
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
from app.services import exchange_pool as ep_mod  # noqa: E402
from app.services import notify as notify_mod  # noqa: E402
from app.services.reentry import (  # noqa: E402
    BAR_MS,
    CARD_DISCLAIMER,
    compute_stop_wick,
    compute_structure,
    register_reentry_watches,
    sweep_reentry_watches,
)

# 固定时钟：所有时间断言不依赖当前机器时间
NOW = datetime(2026, 10, 31, 12, 0, 0)
CLOSED_AT = NOW - timedelta(hours=2)  # 整点止损 → 止损根 = 09:00-10:00

_OK_STRUCTURE = {
    "ema_ok": True, "ema144_used": True, "adx": 28.4,
    "adx_ok": True, "r2": 0.62, "r2_ok": True, "structure_ok": True,
}

# 多头止损根：下影 6.2 / 实体 0.2 / 全幅 6.5 → 31×实体、95% 全幅（合格）
_LONG_WICK_BAR = (115.9, 116.0, 109.5, 115.7)
# 多头止损根：下影 0（收在最低附近）→ 不合格
_LONG_WEAK_BAR = (115.9, 116.0, 115.4, 115.3)


# ── 替身与数据构造 ────────────────────────────────────────────────────


def _utc_ms(dt: datetime) -> int:
    """固定时钟按 UTC 解释（与生产 _ms 口径一致，不随宿主时区漂移）。"""
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _make_klines(end: datetime, trend: str = "up", n: int = 160) -> list[list]:
    """n 根 1h K 线（时间升序，最后一根收盘 = end）。

    up=单边上行（结构完好）；down=单边下行（空头结构完好）；range=震荡（结构已破）。
    open=上一根收盘。
    """
    end_ms = _utc_ms(end)
    start_ms = (end_ms - n * BAR_MS) // BAR_MS * BAR_MS
    kls, prev = [], 100.0
    for i in range(n):
        if trend == "up":
            c = 100.0 + i * 0.10
        elif trend == "down":
            c = 100.0 - i * 0.10
        else:
            c = 100.0 + 4 * (i % 2 * 2 - 1)
        kls.append([start_ms + i * BAR_MS, prev, c + 0.3, c - 0.3, c, 100.0])
        prev = c
    return kls


def _stop_bar_open(closed_at: datetime) -> datetime:
    """包含 closed_at 的那根 1h 的开盘时间（整点边界归前一根）。"""
    ms = _utc_ms(closed_at)
    offset = (ms % BAR_MS) or BAR_MS  # 整点 → 前一根
    return closed_at.replace(tzinfo=None) - timedelta(milliseconds=offset)


def _register_klines(trend: str = "up", stop_bar=_LONG_WICK_BAR,
                     closed_at: datetime = CLOSED_AT) -> list[list]:
    """止损根之前 160 根 + 止损根（收盘 = closed_at 那根）。"""
    stop_open = _stop_bar_open(closed_at)
    bars = _make_klines(stop_open, trend, 160)
    if stop_bar is not None:
        o, h, l, c = stop_bar
        bars.append([_utc_ms(stop_open), o, h, l, c, 100.0])
    return bars


class _FakePool:
    """ExchangePool 替身：get_klines_range / get_klines 返回同一组 K 线。"""

    def __init__(self, klines: list[list]):
        self.klines = klines

    def get_klines_range(self, symbol, interval, limit, end_time_ms):
        return self.klines

    def get_klines(self, symbol, interval, limit):
        return self.klines


class _RaisingPool:
    """拉 K 线全部失败：巡检应保持 watching。"""

    def get_klines_range(self, *a, **kw):
        return []

    def get_klines(self, *a, **kw):
        raise RuntimeError("kline boom")


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
               notified_at=None, triggered_at=None, last_checked_at=None):
    w = ReentryWatch(
        source_trade_id=uuid.uuid4(), symbol=symbol, direction=direction,
        account_key=None, testnet=False, rule_version="v2026-10-30",
        entry_price=entry, stop_initial=stop, r_unit=abs(entry - stop),
        theta_r=0, trigger_price=trigger, status=status,
        structure_json=structure or dict(_OK_STRUCTURE),
        notified_at=notified_at, triggered_at=triggered_at,
        last_checked_at=last_checked_at,
        expires_at=expires_at,
    )
    s.add(w)
    return w


def _patch_sweep(monkeypatch, *, klines=None, raise_kline=False):
    """替身行情/飞书：返回发送记录列表。"""
    sent: list[dict] = []

    def _send(card, *, testnet=False):
        sent.append(card)
        return True

    monkeypatch.setattr(notify_mod, "send_feishu_card", _send)
    if raise_kline:
        monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _RaisingPool())
    else:
        kl = klines if klines is not None else _make_klines(NOW, "up")
        monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(kl))
    return sent


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
    assert compute_structure(_make_klines(NOW, "up", n=60), "long") is None
    assert compute_structure([], "long") is None


# ── 2. compute_stop_wick ─────────────────────────────────────────────


_BAR0 = _utc_ms(datetime(2026, 10, 31, 0, 0))
_BAR0_CLOSED = _BAR0 + BAR_MS


def _wick_bars(o, h, l, c, open_ms=_BAR0):
    return [[open_ms, o, h, l, c, 100.0]]


def test_stop_wick_long_pass():
    """多头看下影：6.2 ≥ 2×0.2 实体且 ≥ 40% 全幅 → 合格"""
    v = compute_stop_wick(_wick_bars(*_LONG_WICK_BAR), "long", _BAR0_CLOSED)
    assert v is not None and v["ok"] is True
    assert v["pct"] >= 0.4 and v["mult"] >= 2.0


def test_stop_wick_long_fail():
    """多头下影≈0（收在最低附近）→ 不合格"""
    v = compute_stop_wick(_wick_bars(*_LONG_WEAK_BAR), "long", _BAR0_CLOSED)
    assert v is not None and v["ok"] is False


def test_stop_wick_short_pass():
    """空头看上涨影：上影 5.8 ≥ 2×0.2 且 ≥ 40% 全幅 → 合格"""
    v = compute_stop_wick(_wick_bars(100.0, 106.0, 99.9, 100.2), "short", _BAR0_CLOSED)
    assert v is not None and v["ok"] is True


def test_stop_wick_boundary_belongs_to_previous_bar():
    """止损恰在整点：归前一根（open 09:00 收盘 10:00 的那根）"""
    open_ms = _utc_ms(datetime(2026, 10, 31, 9, 0))
    closed_ms = open_ms + BAR_MS
    bars = [*_wick_bars(*_LONG_WEAK_BAR, open_ms=open_ms - BAR_MS),
            *_wick_bars(*_LONG_WICK_BAR, open_ms=open_ms)]
    v = compute_stop_wick(bars, "long", closed_ms)
    assert v is not None and v["bar_open_ms"] == open_ms
    assert v["ok"] is True  # 命中的是 09:00 那根长下影，而非前一根


def test_stop_wick_missing_bar():
    """K 线未覆盖止损根 → None（不可判）"""
    assert compute_stop_wick([], "long", 0) is None
    open_ms = _utc_ms(datetime(2026, 10, 31, 9, 0))
    assert compute_stop_wick(_wick_bars(*_LONG_WICK_BAR, open_ms=open_ms),
                             "long", open_ms + 2 * BAR_MS) is None


# ── 3. register_reentry_watches ──────────────────────────────────────


def test_register_creates_watching(db, monkeypatch):
    """正式网 CLOSED+sl+影线/结构都过 → 1 行 watching，trigger_price==entry
    （θ=0），expires_at==closed_at+24h；structure_json 带 stop_wick；不建 TradeRecord"""
    tid = uuid.uuid4()
    _add_closed_trade(db, tid, "BTCUSDT")
    db.commit()
    before = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()

    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(_register_klines()))
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
    assert row.structure_json["stop_wick"]["ok"] is True
    assert row.structure_json["stop_wick"]["pct"] >= 0.4
    assert row.resolve_reason is None and row.resolved_at is None

    after = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()
    assert after == before


def test_register_wick_miss(db, monkeypatch):
    """止损根下影不足 → wick_miss 终态（不等价格回开仓价，也不再看结构）"""
    _add_closed_trade(db, uuid.uuid4(), "WICKUSDT")
    db.commit()

    monkeypatch.setattr(
        ep_mod, "ExchangePool",
        lambda: _FakePool(_register_klines(stop_bar=_LONG_WEAK_BAR)))
    assert register_reentry_watches(db, now=NOW) == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "wick_miss"
    assert row.resolve_reason == "stop_wick_weak"
    assert row.resolved_at == NOW
    assert row.structure_json["stop_wick"]["ok"] is False


def test_register_filters_testnet_reason_and_excluded(db, monkeypatch):
    """测试网单 / 非 sl 单 / excluded 单 → 0 行"""
    _add_closed_trade(db, uuid.uuid4(), "TUSDT", testnet=True)
    _add_closed_trade(db, uuid.uuid4(), "RUSDT", exit_reason="tp1_trail")
    _add_closed_trade(db, uuid.uuid4(), "XUSDT", excluded=True)
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(_register_klines()))
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0


def test_register_structure_broken_at_close(db, monkeypatch):
    """影线合格但结构已破 → structure_broken 终态（供页面解释为何没提醒）"""
    _add_closed_trade(db, uuid.uuid4(), "RANGEUSDT")
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(_register_klines(trend="range")))
    assert register_reentry_watches(db, now=NOW) == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "structure_broken"
    assert row.resolve_reason == "structure_broken_at_close"
    assert row.resolved_at == NOW
    assert row.structure_json["structure_ok"] is False
    assert row.structure_json["stop_wick"]["ok"] is True


def test_register_idempotent(db, monkeypatch):
    """同一 source_trade_id 连跑两次 → 仍只有 1 行（唯一约束 + 预查）"""
    _add_closed_trade(db, uuid.uuid4(), "BTCUSDT")
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(_register_klines()))
    assert register_reentry_watches(db, now=NOW) == 1
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 1


def test_register_skips_without_open_stop_snapshot(db, monkeypatch):
    """无 OPEN 事件止损快照 → R 空间不可算，跳过不落行"""
    _add_closed_trade(db, uuid.uuid4(), "NOSNAPUSDT", with_event=False)
    db.commit()

    monkeypatch.setattr(ep_mod, "ExchangePool", lambda: _FakePool(_register_klines()))
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0


def test_register_skips_without_stop_bar(db, monkeypatch):
    """K 线未覆盖止损根 → 影线不可判，跳过不落行"""
    _add_closed_trade(db, uuid.uuid4(), "NOBARUSDT")
    db.commit()

    monkeypatch.setattr(
        ep_mod, "ExchangePool",
        lambda: _FakePool(_register_klines(stop_bar=None)))
    assert register_reentry_watches(db, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0


def test_register_skips_unclosed_stop_bar(db, monkeypatch):
    """止损发生在当前小时（止损根未收盘）→ 本轮跳过，下一小时再建。

    否则会用形成中的 K 线判影线/结构：low 可能继续下探、close 还在变，
    快照会失真。register 的 now 是整点后 :06，故只需等下一轮。
    """
    tid = uuid.uuid4()
    stop_at = datetime(2026, 10, 31, 12, 2, 0)
    _add_closed_trade(db, tid, "FORMUSDT", closed_at=stop_at)
    db.commit()
    monkeypatch.setattr(
        ep_mod, "ExchangePool",
        lambda: _FakePool(_register_klines(closed_at=stop_at)))

    assert register_reentry_watches(db, now=datetime(2026, 10, 31, 12, 6, 0)) == 0
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 0

    # 13:06 时止损根（12:00-13:00）已收盘 → 正常建监控
    assert register_reentry_watches(db, now=datetime(2026, 10, 31, 13, 6, 0)) == 1
    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "watching"


# ── 4. sweep_reentry_watches ─────────────────────────────────────────


def test_sweep_triggers_on_closed_bar(db, monkeypatch):
    """已收盘 1h 的 high 越过触发价 → triggered；只发 1 张卡且含免责声明；
    重跑不重发"""
    w = _add_watch(db, trigger=100.0)
    db.commit()
    sent = _patch_sweep(monkeypatch)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 1

    db.refresh(w)
    assert w.status == "triggered"
    assert w.triggered_at == NOW
    assert w.notified_at is not None
    assert float(w.current_price) == pytest.approx(115.8)  # 最后一根已收盘 close

    assert len(sent) == 1
    note = sent[0]["elements"][-1]
    assert note["elements"][0]["content"] == CARD_DISCLAIMER

    counts2 = sweep_reentry_watches(db, now=NOW)
    assert counts2["triggered"] == 0
    assert len(sent) == 1


def test_sweep_stop_bar_not_used_for_trigger(db, monkeypatch):
    """止损那根自身 high 越过触发价也不算触发（根内先后顺序不可知）"""
    kl = _make_klines(NOW, "range")
    stop_idx = 145  # 止损根 = NOW-15h 开仓那根
    assert kl[stop_idx][0] == _utc_ms(NOW - timedelta(hours=15))
    kl[stop_idx] = [kl[stop_idx][0], kl[stop_idx][1], 120.0, kl[stop_idx][3],
                    kl[stop_idx][4], 100.0]
    _add_watch(db, trigger=110.0)
    db.commit()
    sent = _patch_sweep(monkeypatch, klines=kl)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status in ("watching", "structure_broken")
    assert sent == []


def test_sweep_ignores_bars_after_expiry(db, monkeypatch):
    """已收盘根触碰触发价，但该根收盘已过窗口 → 不触发，按到期收敛"""
    kl = _make_klines(NOW, "range")
    kl[-2] = [kl[-2][0], kl[-2][1], 120.0, kl[-2][3], kl[-2][4], 100.0]  # 最后一根已收盘
    _add_watch(db, trigger=110.0, expires_at=NOW - timedelta(hours=2))
    db.commit()
    sent = _patch_sweep(monkeypatch, klines=kl)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0
    assert counts["expired"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "expired" and row.resolve_reason == "ttl"
    assert sent == []


def test_sweep_incremental_scan(db, monkeypatch):
    """last_checked_at 已覆盖的根不再重扫；更早的 last_checked_at 会补扫"""
    kl = _make_klines(NOW, "up")
    kl[-2] = [kl[-2][0], kl[-2][1], 130.0, kl[-2][3], kl[-2][4], 100.0]

    w = _add_watch(db, trigger=130.0, last_checked_at=NOW - timedelta(hours=1))
    db.commit()
    sent = _patch_sweep(monkeypatch, klines=kl)
    sweep_reentry_watches(db, now=NOW)
    db.refresh(w)
    assert w.status == "watching"
    assert sent == []

    w2 = _add_watch(db, symbol="ETHUSDT", trigger=130.0,
                    last_checked_at=NOW - timedelta(hours=5))
    db.commit()
    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 1
    db.refresh(w2)
    assert w2.status == "triggered"


def test_sweep_expired_ttl(db, monkeypatch):
    """未触发且已过窗口 → expired / ttl"""
    _add_watch(db, trigger=130.0, expires_at=NOW - timedelta(hours=1))
    db.commit()
    sent = _patch_sweep(monkeypatch)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["expired"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "expired"
    assert row.resolve_reason == "ttl"
    assert row.resolved_at == NOW
    assert sent == []


def test_sweep_superseded_open_position(db, monkeypatch):
    """同 symbol 同账号已有在跑仓 → superseded / open_position"""
    _add_watch(db, symbol="ETHUSDT", trigger=130.0,
               expires_at=NOW + timedelta(hours=10))
    db.add(TradeRecord(
        symbol="ETHUSDT", direction="long", status="OPENED", testnet=False,
        entry_price=100.0, qty=1.0, opened_at=NOW - timedelta(hours=1),
    ))
    db.commit()
    sent = _patch_sweep(monkeypatch)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["superseded"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "superseded"
    assert row.resolve_reason == "open_position"
    assert row.resolved_at == NOW
    assert sent == []


def test_sweep_structure_broken(db, monkeypatch):
    """最新已收盘 1h 结构破坏 → structure_broken / structure_break"""
    _add_watch(db, trigger=130.0, expires_at=NOW + timedelta(hours=10))
    db.commit()
    sent = _patch_sweep(monkeypatch, klines=_make_klines(NOW, "range"))

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["broken"] == 1

    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "structure_broken"
    assert row.resolve_reason == "structure_break"
    assert row.resolved_at == NOW
    assert row.structure_json["structure_ok"] is False
    assert sent == []


def test_sweep_keeps_wick_snapshot(db, monkeypatch):
    """巡检更新结构快照时必须保留止损根影线读数（页面/卡片要用）"""
    w = _add_watch(db, trigger=130.0, expires_at=NOW + timedelta(hours=10),
                   structure={**_OK_STRUCTURE,
                              "stop_wick": {"ok": True, "pct": 0.95, "mult": 31.0}})
    db.commit()
    _patch_sweep(monkeypatch)

    sweep_reentry_watches(db, now=NOW)
    db.refresh(w)
    assert w.structure_json["stop_wick"]["pct"] == pytest.approx(0.95)
    assert w.structure_json["ema_ok"] is True


def test_sweep_kline_error_keeps_watching(db, monkeypatch):
    """拉 K 线异常 → 保持 watching，状态/巡检字段不变"""
    w = _add_watch(db, trigger=130.0, expires_at=NOW + timedelta(hours=10))
    db.commit()
    sent = _patch_sweep(monkeypatch, raise_kline=True)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0 and counts["expired"] == 0
    assert counts["broken"] == 0 and counts["superseded"] == 0

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
    sent = _patch_sweep(monkeypatch)

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 0  # 状态不变，只是补发
    row = db.execute(select(ReentryWatch)).scalars().one()
    assert row.status == "triggered"
    assert row.notified_at is not None
    assert len(sent) == 1
    assert sent[0]["elements"][-1]["elements"][0]["content"] == CARD_DISCLAIMER


def test_sweep_short_triggers_on_low_break(db, monkeypatch):
    """空头：已收盘 1h 的 low ≤ 触发价 → triggered（与多头镜像）"""
    w = _add_watch(db, direction="short", trigger=100.0)
    db.commit()
    sent = _patch_sweep(monkeypatch, klines=_make_klines(NOW, "down"))

    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["triggered"] == 1
    db.refresh(w)
    assert w.status == "triggered"
    assert len(sent) == 1


# ── 5. API ───────────────────────────────────────────────────────────


def test_api_list_seconds_left_and_flattened(client):
    """GET 列表：watching 行 seconds_left>0、终态 0；结构/影线字段展平可读"""
    http, s = client
    w_live = _add_watch(
        s, symbol="BTCUSDT", trigger=100.0, expires_at=datetime(2030, 1, 1),
        structure={**_OK_STRUCTURE,
                   "stop_wick": {"ok": True, "pct": 0.95, "mult": 31.0}},
    )
    w_done = _add_watch(s, symbol="ETHUSDT", status="wick_miss", trigger=100.0,
                        expires_at=datetime(2030, 1, 1))
    w_done.resolve_reason = "stop_wick_weak"
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
    assert live["stop_wick_ok"] is True
    assert live["stop_wick_pct"] == pytest.approx(0.95)
    assert live["stop_wick_mult"] == pytest.approx(31.0)

    done = data[str(w_done.id)]
    assert done["seconds_left"] == 0
    assert done["status"] == "wick_miss"
    assert done["resolve_reason"] == "stop_wick_weak"
    assert done["stop_wick_ok"] is None  # 无影线快照的行不臆造读数


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


# ── 6. 回归 ──────────────────────────────────────────────────────────


def test_regression_no_trade_and_rule_version(db, monkeypatch):
    """register + sweep 前后 TradeRecord 行数不变；RULE_VERSION 只读不变"""
    rv_before = settings.RULE_VERSION
    _add_closed_trade(db, uuid.uuid4(), "BTCUSDT")
    db.commit()
    before = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()

    _patch_sweep(monkeypatch, klines=_register_klines())
    counts = sweep_reentry_watches(db, now=NOW)
    assert counts["registered"] == 1  # 注册路径真的跑到了
    assert db.execute(select(func.count()).select_from(ReentryWatch)).scalar_one() == 1

    after = db.execute(select(func.count()).select_from(TradeRecord)).scalar_one()
    assert after == before
    assert settings.RULE_VERSION == rv_before
