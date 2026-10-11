# -*- coding: utf-8 -*-
"""止盈一距离上限回归（2026-10-12）。

第一档止盈须"够近"：距入场 ≤ TP1_MAX_ATR×ATR14（R 腿 TP1_MAX_R 默认关），
且不低于 rr_min×止损距离（保证"入场±rr_min×止损距离"兜底永远可用、不会
打回死循环）。覆盖：
- 超限（结构位过远）→ 打回，violation 含距离与上限
- 上限内 → 通过
- 宽止损 + 兜底价（1.5R）→ 不被误杀（max 下限钳制生效）
- 无 K 线（ATR 不可算）→ 放行（旧行为）
- trail（s2）→ 豁免
- 两腿都关（TP1_MAX_ATR=TP1_MAX_R=0）→ 整条上限失效
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


def _dec(entry=100.0, stop=98.0, tp1=105.0, strategy=None, strat2=None):
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=entry, stop_loss=stop, take_profit_1=tp1,
        take_profit_2=0.0, recommendation=60.0, analysis="r",
        entry_strategy=strategy, strategy2_decision=strat2,
    )


def _signal(touch_ref=None):
    return {"symbol": "TESTUSDT", "current_price": 100.0, "touch_ref": touch_ref}


@pytest.fixture(autouse=True)
def _reset_caps(monkeypatch):
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 2.0)
    monkeypatch.setattr(settings, "TP1_MAX_R", 0.0)


def test_tp1_too_far_rejected():
    """ATR=2 → 上限 2×2=4；TP1 距入场 5（2.5R）超限 → 打回"""
    ok, v, _ = validate_decision(_dec(tp1=105.0), _signal(), _klines())
    assert not ok
    assert any("止盈一距离过远" in x for x in v)


def test_tp1_within_cap_passes():
    """TP1 距入场 3.5 ≤ 4（且 RR 1.75 ≥ 1.5）→ 通过"""
    ok, v, fixed = validate_decision(_dec(tp1=103.5), _signal(), _klines())
    assert ok, v
    assert fixed["take_profit_1"] == pytest.approx(103.5)


def test_fallback_with_wide_stop_not_killed():
    """宽止损（距 10）：兜底价=1.5R=15 > ATR 腿 4，但上限取 max(4, 15)=15
    → 兜底价恰好不超限（防打回死循环）"""
    ok, v, fixed = validate_decision(
        _dec(stop=90.0, tp1=115.0), _signal(), _klines(),
    )
    assert ok, v
    assert fixed["take_profit_1"] == pytest.approx(115.0)


def test_no_klines_skips_gate():
    """无 K 线（ATR 不可算）且 R 腿关 → 放行（旧行为不变）"""
    ok, _, fixed = validate_decision(_dec(tp1=105.0), _signal(), [])
    assert ok
    assert fixed["take_profit_1"] == pytest.approx(105.0)


def test_s2_trail_exempt():
    """trail（s2，TP1 仅趋势参考价、不挂单）：RR<1.5 保持 s2 → 距离上限豁免"""
    ok, v, fixed = validate_decision(
        _dec(tp1=102.0, strategy="s2", strat2="suggest"),
        _signal(touch_ref="ema21"), _klines(),
    )
    assert ok, v
    assert fixed["tp_mode"] == "trail"


def test_both_legs_off_disables_gate(monkeypatch):
    """两腿都关 → 整条上限失效（远 TP1 也放行）"""
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 0.0)
    monkeypatch.setattr(settings, "TP1_MAX_R", 0.0)
    ok, _, fixed = validate_decision(_dec(tp1=105.0), _signal(), _klines())
    assert ok
    assert fixed["take_profit_1"] == pytest.approx(105.0)


def test_r_leg_blocks_narrow_stop_high_rr(monkeypatch):
    """R 腿开启（=3）：窄止损（距 0.5）2×ATR=4 但 3R=1.5 → 上限 max(1.5, 0.75)=1.5，
    TP1 距 3 超限打回（演示"3-4R 硬上限"的可选口径）"""
    monkeypatch.setattr(settings, "TP1_MAX_R", 3.0)
    ok, v, _ = validate_decision(_dec(stop=99.5, tp1=103.0), _signal(), _klines())
    assert not ok
    assert any("止盈一距离过远" in x for x in v)
