# -*- coding: utf-8 -*-
"""止盈二贴档保护回归（2026-10-13-9）。

TP2 与 TP1 间距过近（或反向/重合）时程序侧**静默撤档**（take_profit_2=0，
不进 violations、不触发 Reflection 重试）。间距下限
gap_min = max(TP2_MIN_GAP_ATR×ATR14, TP2_MIN_GAP_R×止损距离, 2×TAKER_FEE_RATE×入场价)。
覆盖：
- 间距达标 → 保留
- 间距不足 → 撤档为 0（ok=True、无 violations）
- TP2=TP1 重合 / TP2 反向（此前硬打回）→ 撤档为 0（ok=True）
- 空头方向对称
- 两腿配置都为 0 → 整条保护失效（含手续费地板）
- ATR 不可算（无 K 线）→ ATR 腿置 0，R 腿仍生效
- P0 提示词注入间距纪律与阈值
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.risk_guard import TradeDecision, validate_decision  # noqa: E402


def _klines(n=40, rng=2.0, base=100.0):
    """n 根 1h K 线，每根 high-low=rng → TR 恒为 rng → ATR(14)=rng"""
    return [
        [i, base, base + rng / 2, base - rng / 2, base, 10.0, 0, 0.0]
        for i in range(n)
    ]


def _dec(entry=100.0, stop=98.0, tp1=103.0, tp2=0.0, direction="long"):
    return TradeDecision(
        trade_decision="suggest", direction=direction, trade_type="trend_follow",
        entry_price=entry, stop_loss=stop, take_profit_1=tp1,
        take_profit_2=tp2, recommendation=60.0, analysis="r",
    )


def _signal(cur=100.0):
    return {"symbol": "TESTUSDT", "current_price": cur, "signal_type": None}


@pytest.fixture(autouse=True)
def _reset_cfg(monkeypatch):
    # ATR=2、止损距离=2 → ATR 腿 0.5×2=1.0、R 腿 0.5×2=1.0、手续费地板
    # 2×0.0005×100=0.1 → gap_min=1.0
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 2.0)
    monkeypatch.setattr(settings, "TP1_MAX_R", 0.0)
    monkeypatch.setattr(settings, "TP2_MIN_GAP_ATR", 0.5)
    monkeypatch.setattr(settings, "TP2_MIN_GAP_R", 0.5)


# ── 1. 间距达标 → 保留 ──────────────────────────────────────────────


def test_gap_far_enough_kept():
    """多头 TP2=104.5，间距 1.5 ≥ gap_min 1.0 → 保留"""
    ok, v, fixed = validate_decision(_dec(tp2=104.5), _signal(), _klines())
    assert ok, v
    assert not v
    assert fixed["take_profit_2"] == pytest.approx(104.5)


# ── 2. 间距不足 / 重合 / 反向 → 静默撤档 ────────────────────────────


def test_gap_too_close_withdrawn():
    """多头 TP2=103.5，间距 0.5 < 1.0 → 撤档为 0（无 violation、不重试）"""
    ok, v, fixed = validate_decision(_dec(tp2=103.5), _signal(), _klines())
    assert ok, v
    assert not v
    assert fixed["take_profit_2"] == 0


def test_gap_coincident_withdrawn():
    """TP2=TP1 重合 → 撤档为 0"""
    ok, v, fixed = validate_decision(_dec(tp2=103.0), _signal(), _klines())
    assert ok, v
    assert not v
    assert fixed["take_profit_2"] == 0


def test_gap_reversed_no_longer_rejected():
    """TP2<TP1（旧口径硬打回）→ 现改为静默撤档，ok=True 且无 violations"""
    ok, v, fixed = validate_decision(_dec(tp2=102.0), _signal(), _klines())
    assert ok, v
    assert not v
    assert fixed["take_profit_2"] == 0


# ── 3. 空头方向对称 ─────────────────────────────────────────────────


def test_short_gap_far_enough_kept():
    """空头：entry100 stop102 tp1 97，TP2=95.5 间距 1.5 ≥ 1.0 → 保留"""
    ok, v, fixed = validate_decision(
        _dec(stop=102.0, tp1=97.0, tp2=95.5, direction="short"), _signal(), _klines(),
    )
    assert ok, v
    assert fixed["take_profit_2"] == pytest.approx(95.5)


def test_short_gap_too_close_withdrawn():
    """空头贴近：TP2=96.5 间距 0.5 < 1.0 → 撤档"""
    ok, v, fixed = validate_decision(
        _dec(stop=102.0, tp1=97.0, tp2=96.5, direction="short"), _signal(), _klines(),
    )
    assert ok, v
    assert fixed["take_profit_2"] == 0


def test_short_gap_reversed_withdrawn():
    """空头反向（TP2>TP1）→ 撤档为 0、ok=True"""
    ok, v, fixed = validate_decision(
        _dec(stop=102.0, tp1=97.0, tp2=98.0, direction="short"), _signal(), _klines(),
    )
    assert ok, v
    assert not v
    assert fixed["take_profit_2"] == 0


# ── 4. 两腿配置都为 0 → 整条保护失效（含手续费地板） ────────────────


def test_both_legs_off_disables_guard(monkeypatch):
    """TP2_MIN_GAP_ATR=TP2_MIN_GAP_R=0 → 贴档也保留（含手续费地板一并失效）"""
    monkeypatch.setattr(settings, "TP2_MIN_GAP_ATR", 0.0)
    monkeypatch.setattr(settings, "TP2_MIN_GAP_R", 0.0)
    ok, v, fixed = validate_decision(_dec(tp2=103.2), _signal(), _klines())
    assert ok, v
    assert fixed["take_profit_2"] == pytest.approx(103.2)


# ── 5. ATR 不可算 → ATR 腿置 0，R 腿仍生效 ──────────────────────────


def test_atr_unavailable_uses_r_leg():
    """无 K 线（ATR 不可算）→ ATR 腿 0，R 腿 1.0 生效：TP2=103.5 间距 0.5 撤档"""
    ok, v, fixed = validate_decision(_dec(tp2=103.5), _signal(), [])
    assert ok, v
    assert fixed["take_profit_2"] == 0


# ── 6. 提示词注入间距纪律与阈值 ─────────────────────────────────────


def test_p0_prompt_injects_gap_thresholds():
    from app.services.ai_analyzer import render_system_prompt as p0

    p = p0()
    assert "间距 ≥ max(0.5×ATR, 0.5×止损距离)" in p
    assert "直接输出 take_profit_2=0" in p
