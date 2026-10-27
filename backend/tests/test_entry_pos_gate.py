# -*- coding: utf-8 -*-
"""入场位置闸 + 入场诊断快照单测（docs/需求文档/15-实盘止损归因与上线包 B1/O1，2026-10-27）

覆盖：
1. `entry_diagnostics` 纯函数：bar_position / ATR / room_r / planned_rr / snapshot 降级
2. `_attempt_open` 入场位置闸：
   - pos < ENTRY_POS_MIN_RATIO → blocked + open_block_reason 记原因（走不到下单序列）
   - pos ≥ 阈值 → 放行（穿透到下游下单序列）
   - 手动开单 force_market → 按用户意图放行并记日志
   - ENTRY_POS_MIN_RATIO=0 → 闸门关闭（旧行为）
   - 诊断失败（快照 {}）→ 放行，绝不因观测故障停单

sqlite 内存库 create_all 建表，BinanceTrader 以替身注入，不触网；
K 线请求同样以替身注入（`_entry_snapshot` 打桩），保证不触网。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.services import trade_engine  # noqa: E402
from app.services.entry_diagnostics import (  # noqa: E402
    average_true_range,
    bar_position,
    planned_rr,
    room_r,
    snapshot,
)
from app.services.trade_engine import _attempt_open  # noqa: E402


# ── 纯函数 ──────────────────────────────────────────────────────────


def _bars(n=30, high=110.0, low=90.0, close=105.0):
    """同形 K 线：便于手算（ATR=high−low，room/pos 可心算）"""
    return [[i * 3_600_000, 100.0, high, low, close, 1.0] for i in range(n)]


def test_bar_position_uses_last_closed_bar():
    # 末根（未收盘）故意给极端值，验证被排除
    klines = _bars(20) + [[99 * 3_600_000, 100, 500, 1, 105, 1]]
    assert bar_position(klines, 108.0) == pytest.approx(0.9)
    # 越过 K 线上下沿时给出区间外值（限价单/跳空成交）
    assert bar_position(_bars(20), 130.0) == pytest.approx(2.0)
    assert bar_position(_bars(20), 80.0) == pytest.approx(-0.5)


def test_bar_position_degenerate():
    """零幅 K 线/缺价/空列表 → None（不得抛异常）"""
    assert bar_position([[0, 1, 100, 100, 100, 1], [1, 1, 100, 100, 100, 1]], 100.0) is None
    assert bar_position(_bars(5), None) is None
    assert bar_position([], 100.0) is None
    assert bar_position(None, 100.0) is None


def test_average_true_range_and_room():
    klines = _bars(20)
    assert average_true_range(klines) == pytest.approx(20.0)
    # 多单看前高：110 − 108 = 2，止损距离 1 → 2R
    assert room_r(klines, 108.0, 1.0, "long") == pytest.approx(2.0)
    # 空单看前低：108 − 90 = 18 → 18R
    assert room_r(klines, 108.0, 1.0, "short") == pytest.approx(18.0)
    # 入场已越过前高 → 负值（突破追单）
    assert room_r(klines, 115.0, 1.0, "long") == pytest.approx(-5.0)
    # 止损距离缺失/方向非法 → None
    assert room_r(klines, 108.0, 0.0, "long") is None
    assert room_r(klines, 108.0, 1.0, "flat") is None


def test_planned_rr_and_snapshot_degrade():
    assert planned_rr(100.0, 98.0, 104.0) == pytest.approx(2.0)
    assert planned_rr(100.0, 100.0, 104.0) is None
    assert planned_rr(None, 98.0, 104.0) is None
    # 无 K 线（诊断失败）时仍返回结构完整的快照，数值项为 None
    snap = snapshot(klines=None, price=100.0, stop_loss=98.0, direction="long",
                    tp1=104.0, entry_price=100.0)
    assert snap["pos"] is None and snap["stop_atr"] is None and snap["room_r"] is None
    assert snap["planned_rr"] == pytest.approx(2.0)
    assert snap["quality_total"] is None and snap["range_flag"] is False


def test_snapshot_carries_quality_parts():
    snap = snapshot(
        klines=_bars(20), price=108.0, stop_loss=106.0, direction="long", tp1=112.0,
        entry_price=107.0, entry_path="market",
        quality_parts={"total": 96, "range": -8, "shadow": 0},
        signal_type="pullback_confirm", interval="1h",
    )
    assert snap["pos"] == pytest.approx(0.9)
    assert snap["entry_path"] == "market"
    assert snap["signal_type"] == "pullback_confirm"
    assert snap["quality_total"] == pytest.approx(96.0)
    assert snap["range_flag"] is True
    assert snap["shadow"] == 0
    assert snap["stop_dist"] == pytest.approx(1.0)


# ── 闸门（_attempt_open）────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.6)
    monkeypatch.setattr(settings, "ENTRY_DIAG_ENABLED", True)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0.0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "SPREAD_MAX_PCT", 0.003)
    monkeypatch.setattr(settings, "TAKER_FEE_RATE", 0.0005)
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "TRADING_RISK_PCT", 3.0)
    monkeypatch.setattr(settings, "PENDING_ENTRY_ENABLED", True)


def _analysis(db, direction="long"):
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction=direction,
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=65, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _Trader:
    """走到下单序列的替身：闸门链全给最小可用值，setup_leverage 作为
    「已放行到下单序列」的哨兵（抛错终止，测试只关心闸门是否拦截）"""
    account_key = "live-1"
    testnet = False

    def __init__(self):
        self.setup_calls = []

    def listed_symbols(self):
        return set()

    def price(self, symbol):
        return 100.0

    def bracket_mmr(self, symbol, notional=None):
        return 0.005

    def bracket_info(self, symbol, notional):
        return None

    def filters(self, symbol):
        return {"step": 0.001, "tick": 0.01, "min_qty": 0.001,
                "min_notional": 5.0, "max_qty": 1e18, "market_max_qty": 1e18}

    def round_qty(self, symbol, qty):
        return round(qty, 3)

    def round_price(self, symbol, price):
        return round(price, 2)

    def market_slippage_estimate(self, symbol, side, qty):
        return None

    def book_ticker(self, symbol):
        return 99.95, 100.05

    def setup_leverage(self, symbol, leverage):
        self.setup_calls.append((symbol, leverage))
        raise RuntimeError("替身在 setup_leverage 终止（闸门放行哨兵）")


def _patch_snapshot(monkeypatch, result):
    calls = []

    def _fake(cfg, a, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(trade_engine, "_entry_snapshot", _fake)
    return calls


def test_low_pos_blocks(db, monkeypatch):
    """pos=0.3 < 0.6 → 拦下，原因落 open_block_reason，未进入下单序列"""
    trader = _Trader()
    a = _analysis(db)
    calls = _patch_snapshot(monkeypatch, {"pos": 0.3, "stop_dist": 2.0})
    assert _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {}) == "blocked"
    assert "入场位置" in (a.open_block_reason or "")
    assert trader.setup_calls == []
    # 市价路径下快照以现价为口径
    assert calls and calls[0]["entry_path"] == "market"


def test_high_pos_passes(db, monkeypatch):
    """pos=0.75 ≥ 0.6 → 放行，穿透到下单序列（替身在此抛错）"""
    trader = _Trader()
    a = _analysis(db)
    _patch_snapshot(monkeypatch, {"pos": 0.75, "stop_dist": 2.0})
    _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {})
    assert trader.setup_calls  # 确实走到下单序列（替身在此抛错被引擎兜住）
    assert "入场位置" not in (a.open_block_reason or "")


def test_manual_force_market_bypasses(db, monkeypatch):
    """手动开单：pos 不合规也按用户意图放行"""
    trader = _Trader()
    a = _analysis(db)
    _patch_snapshot(monkeypatch, {"pos": 0.1, "stop_dist": 2.0})
    _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {},
                  force_market=True)
    assert trader.setup_calls
    assert "入场位置" not in (a.open_block_reason or "")


def test_gate_disabled_by_default(db, monkeypatch):
    """ENTRY_POS_MIN_RATIO=0 → 闸门关闭（旧行为），低 pos 照常放行"""
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.0)
    trader = _Trader()
    a = _analysis(db)
    _patch_snapshot(monkeypatch, {"pos": 0.05, "stop_dist": 2.0})
    _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {})
    assert trader.setup_calls
    assert "入场位置" not in (a.open_block_reason or "")


def test_snapshot_failure_passes(db, monkeypatch):
    """诊断失败（快照空 dict）→ 放行，不因观测故障停单"""
    trader = _Trader()
    a = _analysis(db)
    _patch_snapshot(monkeypatch, {})
    _attempt_open(db, trader, db.get(SystemConfig, 1), a, 1000.0, {})
    assert trader.setup_calls
    assert "入场位置" not in (a.open_block_reason or "")


def test_entry_snapshot_exception_is_swallowed(monkeypatch):
    """_entry_snapshot 内部异常 → 返回 {}（真实函数，不打桩；K 线源抛错）"""
    def _boom(*_a, **_kw):
        raise RuntimeError("交易所瞬断")

    monkeypatch.setattr("app.services.exchange_pool.ExchangePool", lambda: SimpleNamespace(
        get_klines=_boom))
    a = SimpleNamespace(symbol="TESTUSDT", entry_price=100.0, scan_result=None)
    assert trade_engine._entry_snapshot(
        SimpleNamespace(kline_interval=None), a,
        price=100.0, stop_loss=98.0, direction="long", tp1=104.0, entry_path="market",
    ) == {}
