# -*- coding: utf-8 -*-
"""AI 三价 RR 铁律 / 止盈一距离上限的容差带回归（2026-10-14）。

背景：10-12 起 RR 与 TP1 上限两道校验零容差（1e-9），交易所报价粒度（tick）
取整后的「兜底 1.5R 价」普遍落在 1.49~1.51R，每个擦边单都白烧 2 次 Reflection
重试后强制 skip（10-14 实测占当日强制 skip 一半）。容差带 ai_rr_tol（默认 0.02R，
原仅开仓现价闸使用）统一覆盖三处口径：
- AI 三价 RR 铁律：rr ≥ rr_min - tol
- TP1 距离上限：tp1_R ≤ cap_R + tol（R 单位绝对容差，擦边 1.50R 放行、1.55R 仍拦）
- 开仓现价 RR（trade_engine，既有）

无 K 线场景：RR 铁律仍生效，TP1 ATR/R 腿需要 klines 或 settings 配置支撑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    TradeDecision,
    parse_and_validate,
    validate_decision,
)


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "AI_RR_TOL", 0.02)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 0.0)  # 无 K 线时本就跳过，显式关
    monkeypatch.setattr(settings, "TP1_MAX_R", 0.0)


def _decision(entry=100.0, stop=99.0, tp1=101.49):
    """止损距离 1.0；tp1 参数直接决定 R 倍数（多单：SL<entry<TP1）"""
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=entry, stop_loss=stop, take_profit_1=tp1,
        take_profit_2=0.0, recommendation=60.0, analysis="r",
    )


# ── AI 三价 RR 铁律容差 ──

def test_rr_edge_within_default_tolerance_passes():
    """RR=1.49 ∈ [1.48, 1.5)：tick 取整擦边，默认 tol=0.02 放行（不再白烧重试）"""
    ok, violations, fixed = validate_decision(_decision(tp1=101.49), {}, [])
    assert ok, violations
    assert fixed["risk_reward_ratio"] == pytest.approx(1.49, abs=0.001)


def test_rr_below_tolerance_still_rejected():
    """RR=1.47 < 1.5-0.02=1.48：容差带外仍打回"""
    ok, violations, _ = validate_decision(_decision(tp1=101.47), {}, [])
    assert not ok
    assert any("盈亏比" in v for v in violations)


def test_zero_tolerance_reverts_to_strict():
    """显式 rr_tol=0（页面严格模式）：1.49 < 1.5 依旧拦截，消息保留铁律措辞"""
    ok, violations, _ = validate_decision(_decision(tp1=101.49), {}, [], rr_tol=0.0)
    assert not ok
    assert any("盈亏比" in v and "交易系统铁律" in v for v in violations)


def test_parse_and_validate_threads_rr_tol():
    """P0 管线入口 parse_and_validate 透传 rr_tol（严格模式 1.49 被拦）"""
    raw = {
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow",
        "entry_price": 100.0, "stop_loss": 99.0, "take_profit_1": 101.49,
        "take_profit_2": 0, "recommendation": 60, "analysis": "r",
    }
    ok, violations, _ = parse_and_validate(raw, {}, [], rr_tol=0.0)
    assert not ok
    assert any("盈亏比" in v for v in violations)


# ── TP1 距离上限容差（R 单位同带） ──

def test_tp1_cap_edge_rounding_passes_within_tolerance(monkeypatch):
    """R 腿上限恰为 1.5R：TP1=1.51R（tick 擦边超 0.01R）放行；
    RR=1.51 本身也在容差带内（≥1.48），两关都不拦"""
    monkeypatch.setattr(settings, "TP1_MAX_R", 1.5)
    ok, violations, fixed = validate_decision(_decision(tp1=101.51), {}, [])
    assert ok, violations
    assert fixed["take_profit_1"] == pytest.approx(101.51)


def test_tp1_cap_genuinely_far_still_rejected(monkeypatch):
    """TP1=1.55R > 1.5+0.02=1.52：真·过远仍打回（容差不是放开，只吞 tick 噪音）"""
    monkeypatch.setattr(settings, "TP1_MAX_R", 1.5)
    ok, violations, _ = validate_decision(_decision(tp1=101.55), {}, [])
    assert not ok
    assert any("止盈一距离过远" in v for v in violations)
