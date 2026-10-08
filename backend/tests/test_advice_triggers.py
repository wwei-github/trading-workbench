# -*- coding: utf-8 -*-
"""AI 持仓诊断触发判定回归（2026-09-26）：浮盈回撤触发 + 原有浮亏/恶化/滞留规则不回归。

rec 用 SimpleNamespace 桩替代 ORM（advice_due 仅读属性），免 DB 会话。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.position_advisor import (
    ADVICE_COOLDOWN_HOURS,
    ADVICE_WORSEN_R,
    PEAK_RETRACE_R,
    advice_due,
)

NOW = datetime(2026, 9, 26, 12, 0, 0)


@pytest.fixture(autouse=True)
def _ai_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")


def _rec(**kw):
    base = dict(
        ai_advice_at=None,
        ai_advice_pnl=None,
        peak_total_pnl=None,
        risk_amount=10.0,
        status="OPENED",
        opened_at=NOW - timedelta(hours=2),
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── #2 浮盈回撤触发（新增）──

def test_retrace_triggers_after_cooldown():
    """峰值 +2R 回撤到 +1.2R（0.8R ≥ 0.6R），冷却已过 → 触发"""
    rec = _rec(
        peak_total_pnl=20.0, total_ignored=None,
        ai_advice_at=NOW - timedelta(hours=ADVICE_COOLDOWN_HOURS + 0.1),
        ai_advice_pnl=20.0,
    )
    assert advice_due(rec, total=12.0, now=NOW) is True


def test_retrace_blocked_within_cooldown():
    """回撤达标但冷却内 → 不触发（防回撤平台期刷屏）"""
    rec = _rec(
        peak_total_pnl=20.0,
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=18.0,
    )
    assert advice_due(rec, total=12.0, now=NOW) is False


def test_retrace_below_threshold():
    """峰值 +2R 回撤到 +1.5R（0.5R < 0.6R）→ 不触发"""
    rec = _rec(
        peak_total_pnl=20.0,
        ai_advice_at=NOW - timedelta(hours=ADVICE_COOLDOWN_HOURS + 0.1),
        ai_advice_pnl=20.0,
    )
    assert advice_due(rec, total=15.0, now=NOW) is False


def test_retrace_no_peak_no_trigger():
    """从未有浮盈峰值（peak=0/NULL）→ 回撤分支不触发（冷却内浮亏也不触发）"""
    rec = _rec(
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=-0.5,
    )
    # -0.5 → -1.0：恶化 0.5 < 0.3R(3) 门槛，冷却内 → 仅浮亏分支不足以触发
    assert advice_due(rec, total=-1.0, now=NOW) is False


def test_retrace_still_triggers_when_total_negative():
    """峰值 +2R 跌回负值：浮亏分支（首次浮亏 last=None）先命中，语义一致"""
    rec = _rec(peak_total_pnl=20.0)
    assert advice_due(rec, total=-2.0, now=NOW) is True


# ── 原有规则不回归 ──

def test_first_floating_loss_triggers():
    assert advice_due(_rec(), total=-1.0, now=NOW) is True


def test_worsen_0_5r_triggers_within_cooldown():
    rec = _rec(
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=-5.0,
    )
    # -5 → -10.1：恶化 5.1 ≥ 0.3R(3) → 冷却内也触发
    assert advice_due(rec, total=-10.1, now=NOW) is True


def test_cooldown_blocks_mild_loss():
    rec = _rec(
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=-5.0,
    )
    # -5 → -6：恶化 1 < 3，冷却内 → 不触发
    assert advice_due(rec, total=-6.0, now=NOW) is False


# ── N-17 恶化线 0.5R → 0.3R（2026-10-08，JST 案例实证）──

def test_worsen_0_3r_boundary_triggers():
    """恶化恰 = 0.3R（3.0）→ 冷却内触发（JST 型阴跌单提前重评）"""
    rec = _rec(
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=-5.0,
    )
    assert advice_due(rec, total=-8.0, now=NOW) is True


def test_worsen_below_0_3r_blocked():
    """恶化 2.9 < 0.3R → 冷却内不触发（旧 0.5 线下同样不触发，仅边界更灵敏）"""
    rec = _rec(
        ai_advice_at=NOW - timedelta(hours=1),
        ai_advice_pnl=-5.0,
    )
    assert advice_due(rec, total=-7.9, now=NOW) is False


def test_stale_hold_triggers():
    rec = _rec(opened_at=NOW - timedelta(hours=25))
    assert advice_due(rec, total=1.0, now=NOW) is True


def test_no_risk_amount_retrace_skipped():
    """risk_amount 缺失 → 回撤/恶化分支跳过，不误触发"""
    rec = _rec(
        risk_amount=0,
        peak_total_pnl=20.0,
        ai_advice_at=NOW - timedelta(hours=1),
    )
    assert advice_due(rec, total=12.0, now=NOW) is False
