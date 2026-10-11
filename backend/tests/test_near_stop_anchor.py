# -*- coding: utf-8 -*-
"""近锚降级链回归（docs/23，2026-09-29）。

覆盖：
1. risk_guard.validate_decision：stop_anchor=near 时止损必须贴近突破K线
   （klines[-2]）极值 ±0.5%——偏离过远打回；structure/非突破类不校验
2. fixed dict 落库：near 仅突破类有效，回踩类误报按 structure 落库防分组污染；
   缺省/非法值回退 structure
3. ai_agent._handle_submit：stop_ref=signal_low/signal_high → 程序推导
   stop_anchor=near（不依赖 AI 自报）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.ai_agent import _handle_submit  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    TradeDecision,
    normalize_raw,
    validate_decision,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "RISK_BUDGET_PCT", 3.0)


def _signal(signal_type="trigger_breakout", cur=100.0):
    return {
        "symbol": "TESTUSDT", "signal_type": signal_type,
        "current_price": cur,
        "recent_swings": {
            "highs": [{"price": 105.0, "label": "HH"}],
            "lows": [{"price": 97.0, "label": "HL"}],
        },
    }


def _klines(sig_low=99.0, sig_high=99.8):
    """[-2] = 突破K（low/high 可控），[-1] 未收盘"""
    return [
        [0, 98.0, 98.5, 97.5, 98.2, 10.0, 0, 10000.0],
        [1, 99.0, sig_high, sig_low, 99.7, 20.0, 0, 20000.0],
        [2, 99.8, 100.2, 99.6, 100.0, 5.0, 0, 5000.0],
    ]


def _decision(stop_loss, stop_anchor="near", signal_type="trigger_breakout"):
    """做多：entry=现价 100，TP1=105（RR 由止损距离决定）"""
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor=stop_anchor,
        entry_price=100.0, stop_loss=stop_loss,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    ), _signal(signal_type), _klines()


# ── near 一致性硬校验 ─────────────────────────────────────────────────


def test_near_anchor_within_tolerance_passes():
    """near 正例：止损=突破K最低×(1-0.3%)，偏差≈0 → 通过，落库 near"""
    anchor = 99.0 * 0.997
    d, sig, kl = _decision(round(anchor, 8))
    ok, violations, fixed = validate_decision(d, sig, kl)
    assert ok, violations
    assert fixed["stop_anchor"] == "near"


def test_near_anchor_far_stop_rejected():
    """near 反例：止损挂到摆动低点（远离突破K极值锚）→ 打回"""
    d, sig, kl = _decision(97.0)
    ok, violations, fixed = validate_decision(d, sig, kl)
    assert not ok
    assert fixed == {}
    assert any("近锚" in v for v in violations)


def test_structure_anchor_not_checked():
    """structure 锚不参与近锚校验：止损在摆动低点照常通过"""
    d, sig, kl = _decision(97.0, stop_anchor="structure")
    ok, violations, fixed = validate_decision(d, sig, kl)
    assert ok, violations
    assert fixed["stop_anchor"] == "structure"


def test_near_anchor_short_side():
    """空单对称：near 止损=突破K最高×(1+0.3%)，TP1 下方结构位"""
    anchor = round(99.8 * 1.003, 8)
    d = TradeDecision(
        trade_decision="suggest", direction="short", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=99.0, stop_loss=anchor,
        take_profit_1=94.5, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(d, _signal(cur=99.0), _klines())
    assert ok, violations
    assert fixed["stop_anchor"] == "near"


def test_near_anchor_missing_klines_passes():
    """突破K数据不足时放行（防数据异常锁死开单），不产生违规"""
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=98.5,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(d, _signal(), [_klines()[0]])
    assert ok, violations
    assert fixed["stop_anchor"] == "near"


# ── 落库口径 ──────────────────────────────────────────────────────────


def test_near_on_pullback_type_lands_structure():
    """回踩类 AI 误报 near → 校验跳过（非突破类），落库强制 structure 防分组污染"""
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=98.5,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(
        d, _signal("trigger_pullback_confirm"), _klines(),
    )
    assert ok, violations
    assert fixed["stop_anchor"] == "structure"


def test_missing_stop_anchor_lands_structure():
    """AI 未输出 stop_anchor（旧格式/回踩类）→ 落库回退 structure"""
    d, sig, kl = _decision(98.5, stop_anchor=None)
    ok, violations, fixed = validate_decision(d, sig, kl)
    assert ok, violations
    assert fixed["stop_anchor"] == "structure"


# ── 扫描尾窗（2026-10-12）：信号根未走完 → 近锚校验跳过 ─────────────


def test_near_anchor_skipped_in_tail_window():
    """尾窗标记：信号根是未走完的近收盘根快照、极值未定型 → 近锚一致性校验
    跳过（同一"远离极值"止损在非尾窗下被 test_near_anchor_far_stop_rejected 打回）"""
    d, sig, kl = _decision(97.0)
    sig["signal_bar_provisional"] = True
    ok, violations, fixed = validate_decision(d, sig, kl)
    assert ok, violations
    assert fixed["stop_anchor"] == "near"


def test_provisional_false_keeps_near_check():
    """显式 False（非尾窗）不回退校验：远离突破K极值仍被打回"""
    d, sig, kl = _decision(97.0)
    sig["signal_bar_provisional"] = False
    ok, violations, _ = validate_decision(d, sig, kl)
    assert not ok
    assert any("近锚" in v for v in violations)


def test_tail_window_loses_sl_floor_exemption(monkeypatch):
    """尾窗内近锚不再豁免止损宽度下限：极值未定型 + 无一致性校验，宁宽勿紧"""
    monkeypatch.setattr(settings, "SL_MIN_ATR", 0.3)
    kl = [  # 每根 high-low=2 → ATR(14)≈2 → 下限 0.6
        [i, 100.0, 101.0, 99.0, 100.0, 10.0, 0, 0.0] for i in range(40)
    ]
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=99.5,       # 距入场 0.5 < 0.6
        take_profit_1=100.8, take_profit_2=0.0,  # RR 1.6 够线，不引入其他违规
        recommendation=60.0, analysis="r",
    )
    sig = _signal()
    sig["signal_bar_provisional"] = True
    ok, violations, _ = validate_decision(d, sig, kl)
    assert not ok
    assert any("止损宽度过近" in v for v in violations)


def test_normalize_raw_rejects_bogus_anchor():
    """非法 stop_anchor 值（AI 幻觉）→ normalize 阶段回退 None"""
    d = normalize_raw({
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow", "stop_anchor": "bogus",
        "entry_price": "100", "stop_loss": "98.5",
        "take_profit_1": "105", "recommendation": "60",
    })
    assert d.stop_anchor is None


def test_normalize_raw_empty_anchor_to_none():
    """prompt 教的空串（非突破类「输出空字符串」）→ 归 None 不撞 Literal 校验
    （2026-10-04：白名单曾放行 ""，TradeDecision literal_error 白烧一次 Reflection）"""
    d = normalize_raw({
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow", "stop_anchor": "",
        "entry_price": "100", "stop_loss": "98.5",
        "take_profit_1": "105", "recommendation": "60",
    })
    assert d.stop_anchor is None  # 曾在此抛 ValidationError


# ── Agent 管线：stop_ref → stop_anchor 程序推导 ───────────────────────


def _submit_args(stop_ref, stop_offset=-0.3):
    return {
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow",
        "entry_ref": "market", "entry_offset_pct": 0,
        "stop_ref": stop_ref, "stop_offset_pct": stop_offset,
        "tp1_ref": "resistance_zone_low", "tp1_offset_pct": 0,
        "recommendation": 70,
        "reason": "1. r",
    }


def test_agent_signal_low_ref_derives_near():
    """stop_ref=signal_low → stop_anchor=near（程序推导，非 AI 自报）"""
    done, _, fixed = _handle_submit(
        _submit_args("signal_low"), _signal(), _klines(),
    )
    assert done
    assert fixed["stop_anchor"] == "near"


def test_agent_structure_ref_derives_structure():
    """stop_ref=结构锚 → stop_anchor=structure；近锚校验不触发"""
    done, _, fixed = _handle_submit(
        _submit_args("support", stop_offset=-0.3), _signal(), _klines(),
    )
    assert done
    assert fixed["stop_anchor"] == "structure"


# ── 近锚单风险折价（trade_engine 开仓侧）──────────────────────────────


def test_near_anchor_budget_discounted():
    """near 单风险预算 ×0.7：30 → 21 USDT（敞口折价，1000 账户 ×3% 例）"""
    from app.services.trade_engine import _near_discounted_budget

    assert _near_discounted_budget(30.0, "near") == pytest.approx(21.0)


def test_structure_anchor_budget_untouched():
    """structure / 缺省 / 旧行（NULL）单不受折价影响"""
    from app.services.trade_engine import _near_discounted_budget

    for anchor in ("structure", None):
        assert _near_discounted_budget(30.0, anchor) == 30.0


def test_near_discount_factor_configurable(monkeypatch):
    """系数可配：复盘验证后调回 1.0 即关闭折价"""
    from app.services.trade_engine import _near_discounted_budget

    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 1.0)
    assert _near_discounted_budget(30.0, "near") == 30.0
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.5)
    assert _near_discounted_budget(30.0, "near") == 15.0
