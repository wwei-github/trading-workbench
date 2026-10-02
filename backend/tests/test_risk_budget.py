# -*- coding: utf-8 -*-
"""止损金额模式（2026-09-27）回归：pct/fixed 双口径贯通 提示词渲染 / 仓位复算 / resolver。

固定预算链路：system_config.risk_budget_mode=fixed → ai_tasks.resolve_risk_budget
→ 两管线 risk_label 注入提示词 + risk_budget 传 validate_decision →
position_pct=None（分析期无钱包不折算）/ pct 模式维持 RISK_BUDGET_PCT÷止损距离。
开仓引擎侧 budget 取值逻辑在 trade_engine（不在此测）。
"""
from types import SimpleNamespace

from app.config import settings
from app.services.risk_budget import resolve_risk_budget
from app.services.risk_guard import validate_decision


def _decision(**kw):
    base = dict(
        trade_decision="suggest", skip_reason="", direction="long",
        trade_type="trend_follow", entry_price=100.0, stop_loss=98.0,
        take_profit_1=104.0, take_profit_2=0.0, risk_reward_ratio=2.0,
        position_pct=50.0, recommendation=70, analysis="1. ok",
        stop_anchor=None,  # docs/23：决策契约新增，缺省=结构锚
        tactic_name=None, tactic_reason=None,  # 2026-10-02 战法标注，缺省未命中
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── resolver ──

def test_resolver_default_pct():
    mode, amount, label = resolve_risk_budget(None)
    assert mode == "pct" and amount == settings.RISK_BUDGET_PCT
    assert "3%" in label


def test_resolver_fixed():
    cfg = SimpleNamespace(risk_budget_mode="fixed", risk_fixed_amount=5)
    mode, amount, label = resolve_risk_budget(cfg)
    assert mode == "fixed" and amount == 5.0 and "5 USDT" in label


def test_resolver_fixed_none_amount_falls_back():
    cfg = SimpleNamespace(risk_budget_mode="fixed", risk_fixed_amount=None)
    mode, amount, _ = resolve_risk_budget(cfg)
    assert mode == "fixed" and amount == 3.0


def test_resolver_risk_pct_override():
    """pct 模式比例页面覆盖（2026-10-02）：system_config.risk_pct 覆盖 env 默认"""
    cfg = SimpleNamespace(risk_budget_mode="pct", risk_pct=5.0)
    mode, amount, label = resolve_risk_budget(cfg)
    assert mode == "pct" and amount == 5.0 and "5%" in label


def test_resolver_risk_pct_none_falls_back():
    """risk_pct 列为 NULL（未配置）回落 env RISK_BUDGET_PCT"""
    cfg = SimpleNamespace(risk_budget_mode="pct", risk_pct=None)
    mode, amount, _ = resolve_risk_budget(cfg)
    assert mode == "pct" and amount == settings.RISK_BUDGET_PCT


# ── 仓位复算 ──

def test_pct_mode_position_pct_unchanged():
    """pct 模式（risk_budget=None）：仓位 = 3 ÷ 2%×100 = 150%（止损距离 2%），历史行为不变"""
    ok, _, fixed = validate_decision(_decision(), {}, [])
    assert ok
    assert fixed["position_pct"] == 150.0


def test_fixed_mode_position_pct_none():
    """fixed 模式：预算实额传入 → position_pct=None（无钱包不折算），卡片显示 -"""
    ok, _, fixed = validate_decision(_decision(), {}, [], risk_budget=3.0)
    assert ok
    assert fixed["position_pct"] is None


def test_fixed_mode_zero_budget_falls_back_pct():
    """fixed 预算 ≤0 视为未配置 → 回落 pct 口径（防脏配置静默失效）"""
    ok, _, fixed = validate_decision(_decision(), {}, [], risk_budget=0)
    assert ok
    assert fixed["position_pct"] == 150.0


def test_fixed_mode_rr_rule_still_enforced():
    """fixed 模式下 RR 铁律照常生效（预算口径不影响价格校验）"""
    ok, violations, _ = validate_decision(
        _decision(take_profit_1=101.0), {}, [], risk_budget=3.0,  # RR=0.5 < 1.5
    )
    assert not ok and any("盈亏比" in v for v in violations)


# ── 提示词渲染 ──

def test_p0_prompt_fixed_label():
    from app.services.ai_analyzer import render_system_prompt
    p = render_system_prompt("固定 3 USDT")
    assert "单笔止损预算=固定 3 USDT" in p
    # 默认渲染（None）回落百分比口径
    assert "账户风险基数 × 3%" in render_system_prompt()


def test_p1_prompt_fixed_label():
    from app.services.ai_agent import render_system_prompt
    p = render_system_prompt("固定 5 USDT")
    assert "单笔止损预算=固定 5 USDT" in p and "预算 ÷ 止损距离" in p


# ── 盈亏比铁律下限页面覆盖（2026-10-02 system_config.ai_rr_min）──────

def test_rr_min_override_tightens():
    """cfg 覆盖 rr_min=2.5：默认放行（RR=2.0）的单子变打回"""
    ok_default, _, _ = validate_decision(_decision(), {}, [])  # RR = 4/2 = 2.0
    assert ok_default
    ok_tight, violations, _ = validate_decision(_decision(), {}, [], rr_min=2.5)
    assert not ok_tight and any("盈亏比" in v for v in violations)


def test_rr_min_override_relaxes():
    """cfg 覆盖 rr_min=1.2：RR=1.2 的单子从默认 1.5 门槛打回变放行"""
    d = _decision(take_profit_1=102.4)  # RR = 2.4/2 = 1.2
    ok_default, _, _ = validate_decision(d, {}, [])
    assert not ok_default  # 默认 1.5 打回
    ok_relaxed, _, _ = validate_decision(d, {}, [], rr_min=1.2)
    assert ok_relaxed
