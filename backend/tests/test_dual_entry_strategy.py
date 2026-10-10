# -*- coding: utf-8 -*-
"""双入场策略（2026-10-10-5，docs/20）：策略二 EMA21 金K跟踪

覆盖：
1. risk_guard——s2 资格程序闸（仅 touch_ref=ema21）、强制 trail + TP2 清零、
   RR 铁律豁免、优先级闸（2026-10-10-5b 拍板改标式：s2 复算 RR 已达标 → 程序
   自动改标 s1 落库，不打回）、审计交叉一致性闸、skip 分支审计字段透传
2. normalize_raw——非法值回退 s1/None（存量行为不变的安全默认）
3. 提示词渲染——P0/P1 双策略块、submit_decision schema 新参数
4. trade_engine——s2 跳过现价 RR 复算（s1 同参数仍拦），开仓快照携带 entry_strategy
"""
import sys
from pathlib import Path
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    TradeDecision, normalize_raw, validate_decision,
)
from app.services import market_data as md  # noqa: E402


def _d(entry_strategy=None, tp_mode=None, entry=100.0, stop=96.0, tp1=103.0, tp2=0.0,
       s2_decision=None):
    """rr = |tp1-entry| / |entry-stop| = 3/4 = 0.75（低于 1.5 铁律下限）"""
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        tp_mode=tp_mode, entry_strategy=entry_strategy,
        entry_price=entry, stop_loss=stop, take_profit_1=tp1,
        take_profit_2=tp2, recommendation=80,
        strategy2_decision=s2_decision or ("suggest" if entry_strategy == "s2" else "skip"),
        strategy2_reason="EMA21 金K质量达标" if entry_strategy == "s2" else "触点非ema21",
    )


_SIG_EMA21 = {"touch_ref": "ema21"}


# ── 1. risk_guard：s2 主路径 ──────────────────────────────────────


def test_s2_passes_without_rr_floor():
    """s2 + ema21 触点：RR 0.75 < 1.5 放行，落库 entry_strategy=s2、复算值如实"""
    ok, violations, fixed = validate_decision(_d(entry_strategy="s2"), _SIG_EMA21, [], rr_min=1.5)
    assert ok, violations
    assert fixed["entry_strategy"] == "s2"
    assert fixed["risk_reward_ratio"] == 0.75


def test_s2_forces_trail_and_zeroes_tp2():
    """s2 语义强制：tp_mode 强制 trail（AI 未声明也置）、TP2 硬清零（排序检查随之跳过）"""
    d = _d(entry_strategy="s2", tp2=106.0)  # tp2>tp1 若不清零会撞排序校验
    ok, violations, fixed = validate_decision(d, _SIG_EMA21, [], rr_min=1.5)
    assert ok, violations
    assert fixed["tp_mode"] == "trail"
    assert fixed["take_profit_2"] == 0


def test_s1_unaffected_by_s2_fields():
    """entry_strategy=None（缺省）按 s1：同 RR 0.75 照常打回（铁律不豁免）"""
    ok, violations, _ = validate_decision(_d(), {}, [], rr_min=1.5)
    assert not ok
    assert any("盈亏比复算" in v for v in violations)


# ── 2. risk_guard：两道程序闸 ─────────────────────────────────────


def test_s2_gate_rejects_non_ema21_touch():
    """资格闸：触点 swing ≠ ema21 → 打回（AI 谎报 s2 资格）"""
    ok, violations, _ = validate_decision(
        _d(entry_strategy="s2"), {"touch_ref": "swing"}, [], rr_min=1.5,
    )
    assert not ok
    assert any("策略二仅适用" in v for v in violations)


def test_s2_gate_rejects_missing_touch():
    """资格闸：无触点信号（{}）同样不可用 s2——缺证从严"""
    ok, violations, _ = validate_decision(_d(entry_strategy="s2"), {}, [], rr_min=1.5)
    assert not ok
    assert any("策略二仅适用" in v for v in violations)


def test_s2_priority_relabels_to_s1_when_rr_already_passes():
    """优先级闸（2026-10-10-5b 拍板改标式）：s2 复算 RR=2.0 ≥ 1.5 → 同三价按 s1
    规则同样合法，程序直接改标 s1 落库（不打回——打回会诱发跳档凑 RR 或把仅 s2
    满足的单误杀成 skip）；trail 保留（合法 s1 选择）、strategy2_decision=suggest
    为改标痕迹，复盘可过滤"""
    ok, violations, fixed = validate_decision(
        _d(entry_strategy="s2", tp1=108.0), _SIG_EMA21, [], rr_min=1.5,
    )
    assert ok, violations
    assert fixed["entry_strategy"] == "s1"
    assert fixed["strategy2_decision"] == "suggest"
    assert fixed["tp_mode"] == "trail"


def test_strategy2_suggest_requires_ema21_touch():
    """一致性闸：strategy2_decision=suggest 但触点非 ema21 → 打回（审计字段防脏）。
    s1 主体（RR=2.0 达标）也拦——矛盾组合不论主决策为何都不落库"""
    ok, violations, _ = validate_decision(
        _d(tp1=108.0, s2_decision="suggest"), {"touch_ref": "swing"}, [], rr_min=1.5,
    )
    assert not ok
    assert any("strategy2_decision=suggest" in v for v in violations)


def test_s2_winner_requires_strategy2_suggest():
    """一致性闸：entry_strategy=s2 但 strategy2_decision=skip → 语义矛盾打回
    （s2 胜出必须以策略二建议为前提）"""
    ok, violations, _ = validate_decision(
        _d(entry_strategy="s2", s2_decision="skip"), _SIG_EMA21, [], rr_min=1.5,
    )
    assert not ok
    assert any("须以策略二建议为前提" in v for v in violations)


# ── 3. skip 分支与 normalize ──────────────────────────────────────


def test_skip_branch_passes_strategy2_fields():
    """两策略均 skip：审计字段透传落库（entry_strategy=None）"""
    d = TradeDecision(
        trade_decision="skip", skip_reason="[RR不足]1. 策略一到结构位空间不足",
        strategy2_decision="skip", strategy2_reason="金K实体占比不足",
        recommendation=10,
    )
    ok, violations, fixed = validate_decision(d, {}, [], rr_min=1.5)
    assert ok, violations
    assert fixed["entry_strategy"] is None
    assert fixed["strategy2_decision"] == "skip"
    assert fixed["strategy2_reason"] == "金K实体占比不足"


def test_forced_skip_carries_none_strategy_fields():
    from app.services.risk_guard import build_forced_skip
    fixed = build_forced_skip("重试耗尽")
    assert fixed["entry_strategy"] is None
    assert fixed["strategy2_decision"] is None


def test_normalize_invalid_strategy_values_fall_back():
    """非法值归 None（按 s1 理解）——LLM 缺省/乱报时存量行为完全不变"""
    d = normalize_raw({
        "trade_decision": "suggest",
        "entry_strategy": "strategy-two",
        "strategy2_decision": "maybe",
        "strategy2_reason": "  依据  ",
    })
    assert d.entry_strategy is None
    assert d.strategy2_decision is None
    assert d.strategy2_reason == "依据"


# ── 4. 提示词渲染与工具 schema ────────────────────────────────────


def test_p0_prompt_contains_dual_strategy_block():
    from app.services.ai_analyzer import render_system_prompt
    prompt = render_system_prompt(risk_label="测试预算")
    assert "双入场策略裁决" in prompt
    assert "entry_strategy" in prompt
    assert "策略二" in prompt
    assert "EMA21 金K跟踪" in prompt


def test_p1_prompt_contains_dual_strategy_rule():
    from app.services.ai_agent import render_system_prompt as render_agent_prompt
    prompt = render_agent_prompt(risk_label="测试预算")
    assert "12.6 双入场策略裁决" in prompt
    assert "entry_strategy" in prompt


def test_submit_schema_has_strategy_args():
    from app.services.ai_agent import TOOLS_SCHEMA
    fn = next(t["function"] for t in TOOLS_SCHEMA if t["function"]["name"] == "submit_decision")
    props = fn["parameters"]["properties"]
    assert props["entry_strategy"]["enum"] == ["s1", "s2"]
    assert props["strategy2_decision"]["enum"] == ["suggest", "skip"]
    assert "strategy2_reason" in props


# ── 5. trade_engine：s2 现价 RR 复算豁免 ──────────────────────────


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 0)   # 隔离急跌闸门
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 0)
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_cache():
    md._cache.pop("market_wide_regime", None)
    md._cache.pop("btc_crash_active", None)
    yield
    md._cache.pop("market_wide_regime", None)
    md._cache.pop("btc_crash_active", None)


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models.system_config import SystemConfig
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


def _analysis(db, entry_strategy=None):
    from app.models.scan import AIAnalysis
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction="long",
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=90, entry_strategy=entry_strategy, tp_mode=None,
        created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


class _S2PenetrationTrader:
    """闸门穿透替身：前置闸门数据齐备，RR 复算之后第一个 trader 调用即哨兵报错。
    __getattr__ 兜底——RR 闸后（bracket_mmr）及以后任何方法调用均抛「替身终点」，
    断言该异常即证明现价 RR 复算闸已放行。"""

    account_key = "testnet-1"
    testnet = True

    def listed_symbols(self):
        return {"TESTUSDT"}

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": 1000.0, "available": 900.0}

    def price(self, symbol):
        # entry=100 / sl=98 / tp1=104：现价 100.5 → rr_now = 3.5/2.5 = 1.4 < 1.5
        return 100.5

    def __getattr__(self, name):
        raise RuntimeError("替身终点")


@pytest.fixture(autouse=True)
def _no_market_regime(monkeypatch):
    monkeypatch.setattr(md, "market_wide_regime", lambda client, pause_pct=None: None)


def test_s1_still_blocked_by_current_price_rr(db):
    """s1（含缺省）：现价复算 1.4 < 1.5 → 拦截，open_block_reason 记录死因"""
    a = _analysis(db)  # entry_strategy=None → 按 s1
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _S2PenetrationTrader(), a.id) is False
    assert "盈亏比按现价复算" in (a.open_block_reason or "")


def test_s2_skips_current_price_rr_check(db):
    """s2：现价 RR 复算豁免——穿透到 RR 闸之后的 bracket_mmr（哨兵异常被
    _attempt_open 统一兜底捕获，以 open_block_reason 携带哨兵文案为证）"""
    a = _analysis(db, entry_strategy="s2")
    from app.services.trade_engine import try_open_for_analysis
    assert try_open_for_analysis(db, _S2PenetrationTrader(), a.id) is False
    assert "替身终点" in (a.open_block_reason or "")


def test_ai_snapshot_carries_entry_strategy(db):
    """开仓快照随 ai_snapshot 自持 entry_strategy，复盘按策略分组"""
    from app.services.trade_engine import _ai_snapshot
    a = _analysis(db, entry_strategy="s2")
    snap = _ai_snapshot(a)
    assert snap["entry_strategy"] == "s2"
