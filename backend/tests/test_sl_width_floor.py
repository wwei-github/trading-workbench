# -*- coding: utf-8 -*-
"""止损宽度下限回归（2026-10-12-2）。

止损距入场须 ≥ SL_MIN_ATR×ATR14（默认 0.3），防"止损过近"被噪音扫掉、
抑制 R 放大。覆盖：过近打回 / 达标通过 / 近锚豁免 / K线不足放行 / 关闭开关。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    NEAR_ANCHOR_BUFFER, TradeDecision, validate_decision,
)


def _klines(n=40, rng=2.0, base=100.0, sig_low=None):
    """n 根 1h K 线，每根 high-low=rng → ATR(14)≈rng；sig_low 覆盖金K窗口
    （2026-10-12-8 窗口口径：最近5根已收盘K线 klines[-6:-1]，含信号K [-2]）
    的共同最低价——high 取 sig_low+rng 保持 TR=rng 不污染 ATR 基准"""
    ks = [
        [i, base, base + rng / 2, base - rng / 2, base, 10.0, 0, 0.0]
        for i in range(n)
    ]
    if sig_low is not None:
        for c in ks[max(0, n - 6):n - 1]:
            c[3] = sig_low
            c[2] = sig_low + rng
    return ks


def _dec(entry=100.0, stop=99.5, tp1=101.5, anchor=None):
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=entry, stop_loss=stop, take_profit_1=tp1,
        take_profit_2=0.0, recommendation=60.0, analysis="r",
        stop_anchor=anchor,
    )


def _signal(signal_type=None):
    return {"symbol": "TESTUSDT", "current_price": 100.0, "signal_type": signal_type}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "SL_MIN_ATR", 0.3)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 2.0)
    monkeypatch.setattr(settings, "TP1_MAX_R", 0.0)


def test_stop_too_tight_rejected():
    """ATR=2 → 下限 0.3×2=0.6；止损距入场 0.5 过近 → 打回"""
    ok, v, _ = validate_decision(_dec(stop=99.5), _signal(), _klines())
    assert not ok
    assert any("止损宽度过近" in x for x in v)


def test_stop_at_floor_passes():
    """止损距入场 0.7 ≥ 0.6 → 通过"""
    ok, v, fixed = validate_decision(_dec(stop=99.3, tp1=101.5), _signal(), _klines())
    assert ok, v
    assert fixed["stop_loss"] == pytest.approx(99.3)


def test_near_anchor_exempt():
    """近锚（突破类 stop_anchor=near）：止损刻意贴金K窗口极值、低于下限也放行"""
    sig_low = 99.85  # 金K窗口（近5根）共同最低价
    anchor = sig_low * (1 - NEAR_ANCHOR_BUFFER)
    # TP1=101.2：缓冲 0.3%→0.4% 后止损距离略宽（≈0.55），RR≈2.2 仍达标
    d = _dec(stop=round(anchor, 6), tp1=101.2, anchor="near")
    ok, v, _ = validate_decision(
        d, _signal(signal_type="trigger_breakout"), _klines(sig_low=sig_low),
    )
    assert not any("止损宽度过近" in x for x in v)
    assert ok, v  # 近锚一致性校验也通过 → 整体放行


def test_same_tight_stop_not_exempt_without_near():
    """同一过近止损但非近锚 → 下限照常生效"""
    sig_low = 99.85  # 金K窗口（近5根）共同最低价
    anchor = sig_low * (1 - NEAR_ANCHOR_BUFFER)
    ok, v, _ = validate_decision(
        _dec(stop=round(anchor, 6), tp1=100.8), _signal(), _klines(sig_low=sig_low),
    )
    assert not ok
    assert any("止损宽度过近" in x for x in v)


def test_no_klines_skips_floor():
    """无 K 线（ATR 不可算）→ 放行"""
    ok, _, fixed = validate_decision(_dec(stop=99.9, tp1=101.5), _signal(), [])
    assert ok
    assert fixed["stop_loss"] == pytest.approx(99.9)


def test_floor_disabled(monkeypatch):
    """SL_MIN_ATR=0 → 关闭该闸"""
    monkeypatch.setattr(settings, "SL_MIN_ATR", 0.0)
    ok, _, _ = validate_decision(_dec(stop=99.9, tp1=101.5), _signal(), _klines())
    assert ok
