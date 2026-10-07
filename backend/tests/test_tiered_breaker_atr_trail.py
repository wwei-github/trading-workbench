# -*- coding: utf-8 -*-
"""周/月回撤分层熔断 + ATR 跟进止损单测（2026-10-04，docs/13 #2/#5）

覆盖：
1. 分层熔断闸门：月回撤 ≥25% 基数停开仓（ack 前拦 / ack 后放行 / 跨月重武装）、
   周回撤 ≥15% 基数在跑/同向上限减半（2026-10-07 改口径，原单笔预算减半；
   不拦开仓 + WARNING 日志）、窗口边界
2. breaker_status_by_net 分层字段：week/month 状态与 ack 语义
3. ATR 跟进（Chandelier）：_chandelier_sl 多/空腿计算 + 开仓前 K 线排除 +
   数据不可得退化；_move_sl_trailing 两腿取紧 + 只收紧铁律（ATR 腿松于现 SL 不放松）
4. ack API：POST /scans/config/monthly-breaker-ack 写确认时刻

sqlite 内存库 create_all；trader/K线池以替身注入，不触网。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import trade_engine as te  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _chandelier_sl,
    _closed_pnl_since,
    _move_sl_trailing,
    breaker_status_by_net,
    try_open_for_analysis,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    """固定配置基线：分层阈值显式给定，其余闸门全部关掉（只测本特性路径）"""
    monkeypatch.setattr(settings, "WEEKLY_DD_STOP_PCT", 15.0)
    monkeypatch.setattr(settings, "MONTHLY_DD_STOP_PCT", 25.0)
    monkeypatch.setattr(settings, "ATR_TRAIL_K", 3.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0.0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "SPREAD_MAX_PCT", 1.0)
    monkeypatch.setattr(settings, "TAKER_FEE_RATE", 0.0005)
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "TRADING_RISK_PCT", 3.0)
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_PCT", 9.0)
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_ENABLED", True)
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.7)
    # 普跌防护 stub（2026-10-07）：不 stub 会真实请求币安，当天 BTC 恰逢
    # 3 根 1h 累计急跌时该闸抢在所有被测闸门前拦截（与 test_open_gates_v2 同款）
    from app.services import market_data as md
    monkeypatch.setattr(md, "btc_crash_active", lambda client, crash_pct=None: False)


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在独立线程跑请求，
    # 共享同一内存库连接（test_adopt_account_scope 同款惯例）
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1))
    session.commit()
    yield session
    session.close()


def _yesterday_noon() -> datetime:
    """昨天中午（UTC）：无论当前几点都落在昨日——跨 UTC 日界不会误入当日窗口"""
    return datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)


def _is_monday_utc() -> bool:
    """UTC 周一：当周尚无「昨日」可用——周窗口与日窗口无法隔离（周回撤
    本就从今天 0 点重算，昨日亏损不计入是正确产品行为），窗口类测试跳过"""
    return datetime.utcnow().weekday() == 0


def _is_month_first_utc() -> bool:
    """UTC 当月 1 日：昨日属上月，月窗口无法用昨日数据构造触发（同理正确）"""
    return datetime.utcnow().day == 1


def _closed(db, pnl, closed_at, account_key="testnet-1", testnet=True):
    rec = TradeRecord(
        symbol="AAAUSDT", direction="long", status="CLOSED",
        account_key=account_key, testnet=testnet,
        opened_at=closed_at - timedelta(hours=1), closed_at=closed_at,
        realized_pnl=pnl, risk_amount=10.0,
    )
    db.add(rec)
    db.commit()
    return rec


def _make_analysis(db, rec=90, direction="long"):
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction=direction,
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=rec, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _FakeTrader:
    """闸门链替身：price 抛错 = 前置闸门全部放行、穿透到 _attempt_open
    （open_block_reason 落「开仓失败」）"""
    account_key = "testnet-1"
    testnet = True

    def __init__(self, account=None):
        self.account = account  # breaker_status_by_net 按 BinanceTrader(account) 构造

    def listed_symbols(self):
        return set()  # 空集合=放行（同「拉取失败放行」语义）

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": 1000.0, "available": 900.0}

    def price(self, symbol):
        raise RuntimeError("替身不提供价格（前置闸门应在此前拦截）")


# ── _closed_pnl_since 窗口口径 ────────────────────────────────────────


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_closed_pnl_since_window(db):
    """窗口聚合：仅 since 起的 CLOSED 行；未平仓行排除"""
    now = datetime.utcnow()
    _closed(db, -80.0, _yesterday_noon())
    _closed(db, -75.0, now)
    _closed(db, 30.0, now)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    assert _closed_pnl_since(db, "testnet-1", True, week_start) == pytest.approx(-80.0 - 75.0 + 30.0)


# ── 月熔断：拦截 / ack 恢复 / 跨月重武装 ──────────────────────────────


def _month_hit_data(db):
    """月触发且日/周判定不抢跑：昨天 -185 + 今天 -70 →
    月 -255 ≤ -250（基数 1000×25%）、日 -70 > -90（日熔断不触发）、
    周 -255 ≤ -150（周触发但月先判）"""
    _closed(db, -185.0, _yesterday_noon())
    _closed(db, -70.0, datetime.utcnow())


@pytest.mark.skipif(_is_month_first_utc(), reason="UTC 月 1 日当月无昨日，月窗口无法用昨日数据构造")
def test_month_breaker_blocks(db, caplog):
    _month_hit_data(db)
    a = _make_analysis(db)
    with caplog.at_level("WARNING"):
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "月度回撤熔断" in (a.open_block_reason or "")
    assert "人工复盘确认" in (a.open_block_reason or "")
    assert any("月度回撤熔断拦截开仓" in r.message for r in caplog.records)


@pytest.mark.skipif(_is_month_first_utc(), reason="UTC 月 1 日当月无昨日，月窗口无法用昨日数据构造")
def test_month_breaker_ack_resumes(db):
    """ack 时刻在当前 UTC 月内 → 放行（穿透到 _attempt_open，落「开仓失败」）"""
    _month_hit_data(db)
    cfg = db.get(SystemConfig, 1)
    cfg.monthly_breaker_ack_at = datetime.utcnow()
    db.commit()
    a = _make_analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "熔断" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")


@pytest.mark.skipif(_is_month_first_utc(), reason="UTC 月 1 日当月无昨日，月窗口无法用昨日数据构造")
def test_month_breaker_ack_stale_next_month(db):
    """ack 是上月的（跨月重武装）→ 仍拦截"""
    _month_hit_data(db)
    cfg = db.get(SystemConfig, 1)
    cfg.monthly_breaker_ack_at = datetime.utcnow().replace(day=1) - timedelta(days=1)
    db.commit()
    a = _make_analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "月度回撤熔断" in (a.open_block_reason or "")


@pytest.mark.skipif(_is_month_first_utc(), reason="UTC 月 1 日当月无昨日，月窗口无法用昨日数据构造")
def test_month_breaker_disabled_at_zero(db, monkeypatch):
    """月阈值 0 = 关闭该层；同数据穿透（日/周也未触发）"""
    monkeypatch.setattr(settings, "MONTHLY_DD_STOP_PCT", 0.0)
    monkeypatch.setattr(settings, "WEEKLY_DD_STOP_PCT", 0.0)
    _month_hit_data(db)
    a = _make_analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "熔断" not in (a.open_block_reason or "")
    assert "减速" not in (a.open_block_reason or "")


# ── 周减速：上限减半不拦截 ────────────────────────────────────────────


def _week_hit_data(db):
    """周触发且日/月未触发：昨天 -80 + 今天 -75 → 周 -155 ≤ -150（15%），
    日 -75 > -90、月 -155 > -250"""
    _closed(db, -80.0, _yesterday_noon())
    _closed(db, -75.0, datetime.utcnow())


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_no_block(db, caplog):
    """周回撤触发：不拦开仓（穿透 _attempt_open）+ WARNING 减速日志"""
    _week_hit_data(db)
    a = _make_analysis(db)
    with caplog.at_level("WARNING"):
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "开仓失败" in (a.open_block_reason or "")
    assert any("周度回撤减速" in r.message for r in caplog.records)
    assert any("减半" in r.message for r in caplog.records)


def test_week_not_triggered_under_threshold(db, caplog):
    """周净亏 -80 > -150 阈值：无减速日志、直接穿透"""
    _closed(db, -80.0, _yesterday_noon())
    a = _make_analysis(db)
    with caplog.at_level("WARNING"):
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "开仓失败" in (a.open_block_reason or "")
    assert not any("周度回撤减速" in r.message for r in caplog.records)


class _TwoPosTrader(_FakeTrader):
    """两个在跑仓位（initial_margin 供余额风控闸门用）"""

    def positions(self):
        return {"ETHUSDT": {"initial_margin": 10.0},
                "SOLUSDT": {"initial_margin": 10.0}}


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_halves_slot_limit(db):
    """2026-10-07 改口径：周减速 → 在跑上限减半（5 → 2）。在跑 2 单（常规
    放行穿透）在减速期被单数闸门拦下（标记「周减速减半」）；ack 后同数据恢复放行"""
    cfg = db.get(SystemConfig, 1)
    cfg.max_open_trades = 5
    db.commit()
    a = _make_analysis(db)
    assert try_open_for_analysis(db, _TwoPosTrader(), a.id) is False
    assert "开仓失败" in (a.open_block_reason or "")  # 常规口径：2 < 5 放行穿透

    _week_hit_data(db)
    a2 = _make_analysis(db)
    assert try_open_for_analysis(db, _TwoPosTrader(), a2.id) is False
    assert "在跑单子已达上限 2（周减速减半）" in (a2.open_block_reason or "")

    cfg.weekly_breaker_ack_at = datetime.utcnow()
    db.commit()
    a3 = _make_analysis(db)
    assert try_open_for_analysis(db, _TwoPosTrader(), a3.id) is False
    assert "开仓失败" in (a3.open_block_reason or "")  # ack 后恢复常规上限


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_halves_same_dir_cap(db):
    """2026-10-07 改口径：周减速 → 同向上限减半（4 → 2）。同向 2 单（常规
    放行穿透）在减速期被同向闸门拦下；max_open=0 不限单数 + 热度 CAP=0 隔离同向路径"""
    cfg = db.get(SystemConfig, 1)
    cfg.same_direction_cap = 4
    cfg.max_open_trades = 0  # 不限单数，隔离同向闸门
    db.commit()
    for sym in ("ETHUSDT", "SOLUSDT"):
        db.add(TradeRecord(symbol=sym, direction="long", status="OPENED",
                           account_key="testnet-1", testnet=True,
                           opened_at=datetime.utcnow(), risk_amount=10.0))
    db.commit()
    a = _make_analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "开仓失败" in (a.open_block_reason or "")  # 常规：2 < 4 放行穿透

    _week_hit_data(db)
    a2 = _make_analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a2.id) is False
    assert "long 方向在跑 2 单 ≥ 2（周减速减半）" in (a2.open_block_reason or "")


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_logs_each_attempt(db, caplog):
    """减速作用于在跑/同向上限（2026-10-07 改口径）：同一窗口连续两次
    尝试各自记录减速日志（窗口状态无漂移、判定幂等）"""
    _week_hit_data(db)
    with caplog.at_level("WARNING"):
        for _ in range(2):
            a = _make_analysis(db)
            try_open_for_analysis(db, _FakeTrader(), a.id)
    assert sum(1 for r in caplog.records if "周度回撤减速" in r.message) == 2


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_ack_resumes(db, caplog):
    """周减速人工复盘确认（2026-10-07）：ack 在当前 UTC 周内 → 本周不再减半"""
    _week_hit_data(db)
    cfg = db.get(SystemConfig, 1)
    cfg.weekly_breaker_ack_at = datetime.utcnow()
    db.commit()
    a = _make_analysis(db)
    with caplog.at_level("WARNING"):
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert "开仓失败" in (a.open_block_reason or "")   # 仍穿透到 _attempt_open
    assert not any("周度回撤减速" in r.message for r in caplog.records)


@pytest.mark.skipif(_is_monday_utc(), reason="UTC 周一当周无昨日，周窗口无法与日窗口隔离")
def test_week_derate_ack_stale_next_week(db, caplog):
    """ack 是上周一的（下周一重武装）→ 仍减半"""
    _week_hit_data(db)
    cfg = db.get(SystemConfig, 1)
    cfg.weekly_breaker_ack_at = (
        datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
    db.commit()
    a = _make_analysis(db)
    with caplog.at_level("WARNING"):
        assert try_open_for_analysis(db, _FakeTrader(), a.id) is False
    assert any("周度回撤减速" in r.message for r in caplog.records)


# ── breaker_status_by_net 分层字段 ───────────────────────────────────


@pytest.mark.skipif(_is_monday_utc() or _is_month_first_utc(),
                    reason="周一/月 1 日当窗口无昨日数据，周月字段无法构造触发")
def test_breaker_status_week_month_fields(db, monkeypatch):
    """week/month 状态：月触发未确认 active=True；确认后 active=False/acked=True"""
    accounts = [SimpleNamespace(key="testnet-1", testnet=True)]
    import app.services.account_registry as reg
    import app.services.binance_trader as bt
    monkeypatch.setattr(reg, "auto_trade_accounts", lambda db: accounts)
    monkeypatch.setattr(bt, "BinanceTrader", _FakeTrader)
    te._wallet_cache.clear()

    cfg = SimpleNamespace(
        daily_loss_stop_enabled=True, daily_loss_stop_pct=9,
        weekly_dd_stop_pct=15.0, monthly_dd_stop_pct=25.0,
        monthly_breaker_ack_at=None, weekly_breaker_ack_at=None,
    )
    # 月触发：-255 ≤ -250（wallet 1000 → 基数 1000）
    _month_hit_data(db)
    out = breaker_status_by_net(db, cfg)
    assert out["testnet"]["week"]["active"] is True       # -255 ≤ -150
    assert out["testnet"]["week"]["hit"] is True
    assert out["testnet"]["week"]["acked"] is False
    assert out["testnet"]["month"]["hit"] is True
    assert out["testnet"]["month"]["active"] is True      # 触发且未确认
    assert out["testnet"]["month"]["acked"] is False

    # 确认后：month.active=False（acked=True），week 不受月 ack 影响
    cfg.monthly_breaker_ack_at = datetime.utcnow()
    out = breaker_status_by_net(db, cfg)
    assert out["testnet"]["month"]["acked"] is True
    assert out["testnet"]["month"]["active"] is False
    assert out["testnet"]["week"]["active"] is True

    # 周 ack 后：week.active=False（acked=True，2026-10-07 人工通道）
    cfg.weekly_breaker_ack_at = datetime.utcnow()
    out = breaker_status_by_net(db, cfg)
    assert out["testnet"]["week"]["acked"] is True
    assert out["testnet"]["week"]["active"] is False
    assert out["testnet"]["week"]["hit"] is True


def test_breaker_status_layers_null_when_no_accounts(db, monkeypatch):
    """该网无自动交易账号 → week/month 均为 None（不误报）"""
    import app.services.account_registry as reg
    monkeypatch.setattr(reg, "auto_trade_accounts", lambda db: [])
    cfg = SimpleNamespace(
        daily_loss_stop_enabled=True, daily_loss_stop_pct=9,
        weekly_dd_stop_pct=15.0, monthly_dd_stop_pct=25.0,
        monthly_breaker_ack_at=None, weekly_breaker_ack_at=None,
    )
    out = breaker_status_by_net(db, cfg)
    assert out["live"]["week"] is None and out["live"]["month"] is None


# ── ATR 跟进（Chandelier Exit） ──────────────────────────────────────


def _h1_bar(t0_ms: int, i: int, high: float, low: float) -> list:
    """一根 1h K（index 0=开市时间 ms，2=high，3=low）"""
    return [t0_ms + i * 3_600_000, 100.0, high, low, (high + low) / 2.0, 10.0] + [0.0] * 4


def _d1_bars(n: int, high=100.0, low=90.0) -> list:
    """n 根日 K：TR 恒定 = high-low → ATR(22) = 10（h=100/l=90 时）"""
    t0 = 1_700_000_000_000
    return [[t0 + i * 86_400_000, 95.0, high, low, 95.0, 10.0] + [0.0] * 4
            for i in range(n)]


class _FakeKlinePool:
    """1h/1d K 线替身：1h 序列 = pre_bars（开仓前）+ h1_bars；1d 返回 _d1_bars"""

    def __init__(self, h1_bars, pre_bars=None, d1_bars=None):
        self._h1 = list(pre_bars or []) + list(h1_bars)
        self._d1 = d1_bars or _d1_bars(30)

    def get_klines(self, symbol, interval, n):
        if interval == "1h":
            return self._h1[-n:] if len(self._h1) > n else self._h1
        return self._d1[-n:] if len(self._d1) > n else self._d1


def _rec(direction="long", opened_hours_ago=5.0, stop_loss=100.0,
         status="TP1_HIT", tp1=104.0):
    """TP1_HIT 默认：避开 _trailing_stop/_move_sl_trailing 的 TP2 保本夹紧
    （夹紧会把 SL 抬到 tp1，干扰本组只收紧断言——夹紧行为在既有用例覆盖）"""
    opened = datetime.utcnow() - timedelta(hours=opened_hours_ago)
    return SimpleNamespace(
        symbol="TESTUSDT", direction=direction, opened_at=opened,
        stop_loss=stop_loss, status=status, tp1=tp1, raw={},
    )


def test_chandelier_long_leg(monkeypatch):
    """多单：开仓后计入高点 111、ATR=10、k=3 → SL = 111 - 30 = 81。
    开仓前那根 K（high=120）被 opened 过滤排除——漏排除会算出 120-30=90"""
    opened = datetime.utcnow() - timedelta(hours=5)
    # t0 = opened - 3h（UTC 毫秒；naive utcnow 补 UTC tz，勿用本地时区 timestamp）
    t0_ms = int((opened - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    pool = _FakeKlinePool(
        # 6 根 h1 + 2 根 pre = 8 根：n=7 窗口切掉最旧 pre 根 → [:-1] 剔最新 i=5
        # → opened=t0+3h 过滤计入 i=3(109.5)/i=4(111)，max=111；pre 的 120/118 被排除
        h1_bars=[_h1_bar(t0_ms, i, 105.0 + 1.5 * i, 90.0) for i in range(6)],
        pre_bars=[_h1_bar(t0_ms, -2, 120.0, 95.0), _h1_bar(t0_ms, -1, 118.0, 95.0)],
    )
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)
    rec = _rec(opened_hours_ago=5.0)
    val = _chandelier_sl(rec, None, 3.0)
    assert val == pytest.approx(111.0 - 30.0, abs=0.5)


def test_chandelier_short_leg(monkeypatch):
    """空单：开仓后低点 90、ATR=10、k=3 → SL = 90 + 30 = 120"""
    opened = datetime.utcnow() - timedelta(hours=5)
    t0_ms = int((opened - timedelta(hours=3)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    pool = _FakeKlinePool(h1_bars=[_h1_bar(t0_ms, i, 110.0, 90.0) for i in range(8)])
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)
    rec = _rec(direction="short", opened_hours_ago=5.0)
    val = _chandelier_sl(rec, None, 3.0)
    assert val == pytest.approx(120.0, abs=0.5)


def test_chandelier_unavailable_degrades(monkeypatch):
    """K 线拉取失败 / 开仓不足 1h 无已收盘根 → None（退化摆动腿）"""
    class _Boom:
        def get_klines(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(te, "ExchangePool", lambda: _Boom())
    assert _chandelier_sl(_rec(opened_hours_ago=5.0), None, 3.0) is None

    pool = _FakeKlinePool(h1_bars=[])  # 无已收盘根
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)
    assert _chandelier_sl(_rec(opened_hours_ago=0.2), None, 3.0) is None


def test_move_sl_takes_tighter_of_two_legs(monkeypatch):
    """两腿取更紧（多单含 cur 取 max）：摆动腿 104 < ATR 腿 107 → SL=107"""
    klines = [[i * 1e6, 100.0, 106.0, 104.0 if i >= 1 else 105.0, 105.0, 10.0] + [0.0] * 4
              for i in range(5)]
    pool = SimpleNamespace(get_klines=lambda s, iv, n: klines)
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)
    monkeypatch.setattr(te, "_chandelier_sl", lambda rec, trader, k: 107.0)

    captured = {}
    monkeypatch.setattr(te, "_replace_sl",
                        lambda db, trader, rec, open_ids, new_sl, reason=None:
                        captured.update(sl=new_sl))
    trader = SimpleNamespace(round_price=lambda s, p: round(p, 2),
                             price=lambda s: 110.0)
    rec = _rec(stop_loss=100.0)
    _move_sl_trailing(db, trader, rec, "1h", set(), atr_k=3.0)
    assert captured["sl"] == 107.0


def test_move_sl_never_loosens(monkeypatch):
    """只收紧铁律：ATR 腿 80 松于现 SL 100 → 聚合含 cur → SL 不动（不调 _replace_sl）"""
    klines = [[i * 1e6, 100.0, 101.0, 99.5, 100.0, 10.0] + [0.0] * 4 for i in range(5)]
    pool = SimpleNamespace(get_klines=lambda s, iv, n: klines)
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)
    monkeypatch.setattr(te, "_chandelier_sl", lambda rec, trader, k: 80.0)

    called = []
    monkeypatch.setattr(te, "_replace_sl",
                        lambda db, trader, rec, open_ids, new_sl, reason=None:
                        called.append(new_sl))
    trader = SimpleNamespace(round_price=lambda s, p: round(p, 2),
                             price=lambda s: 110.0)
    rec = _rec(stop_loss=100.0)
    _move_sl_trailing(db, trader, rec, "1h", set(), atr_k=3.0)
    assert not called


def test_move_sl_swing_only_when_k_zero(monkeypatch):
    """atr_k=0 关闭 ATR 腿：_chandelier_sl 不被调用，仅摆动腿"""
    klines = [[i * 1e6, 100.0, 106.0, 103.0, 105.0, 10.0] + [0.0] * 4 for i in range(5)]
    pool = SimpleNamespace(get_klines=lambda s, iv, n: klines)
    monkeypatch.setattr(te, "ExchangePool", lambda: pool)

    def _boom(rec, trader, k):
        raise AssertionError("atr_k=0 时不应调用 ATR 腿")

    monkeypatch.setattr(te, "_chandelier_sl", _boom)
    captured = {}
    monkeypatch.setattr(te, "_replace_sl",
                        lambda db, trader, rec, open_ids, new_sl, reason=None:
                        captured.update(sl=new_sl))
    trader = SimpleNamespace(round_price=lambda s, p: round(p, 2),
                             price=lambda s: 110.0)
    rec = _rec(stop_loss=100.0)
    _move_sl_trailing(db, trader, rec, "1h", set(), atr_k=0)
    # 摆动腿 = max(现SL 100, 近3根 low 103) = 103（收紧）
    assert captured["sl"] == 103.0


# ── ack API ──────────────────────────────────────────────────────────


def test_ack_monthly_breaker_api(db):
    """POST /scans/config/monthly-breaker-ack：写确认时刻（当月内）。
    TestClient 不用 with（不触发 lifespan 连真实库，test_market_api 同款惯例）"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        resp = client.post("/api/scans/config/monthly-breaker-ack")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        app.dependency_overrides.pop(get_db, None)

    cfg = db.get(SystemConfig, 1)
    assert cfg.monthly_breaker_ack_at is not None
    assert cfg.monthly_breaker_ack_at <= datetime.utcnow() + timedelta(seconds=5)
