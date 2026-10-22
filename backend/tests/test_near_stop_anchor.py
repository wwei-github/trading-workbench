# -*- coding: utf-8 -*-
"""近锚回归（docs/23，2026-09-29；2026-10-12-5 距离分档扩展；2026-10-12-8 窗口口径）。

覆盖：
1. risk_guard.validate_decision：stop_anchor=near 时止损必须贴近金K窗口
   （最近5根已收盘K线共同极值，2026-10-12-8 恢复窗口口径）±0.5%——偏离过远
   打回；structure 不校验；2026-10-12-5 起校验与落库均扩展至全信号类型
2. fixed dict 落库：near 全信号类型有效（须通过一致性校验）；
   缺省/非法值回退 structure
3. 止损宽度下限豁免仍限突破类 near（非突破类金K锚不豁免）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
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
    """[-2] = 突破K（low/high 可控），[-1] 未收盘；[0] 极值不越出突破K极值 →
    窗口（最近5根已收盘，3 根序列即 [0]/[1]）共同极值 = 突破K极值"""
    return [
        [0, 99.2, 99.5, 99.2, 99.3, 10.0, 0, 10000.0],
        [1, 99.3, sig_high, sig_low, 99.7, 20.0, 0, 20000.0],
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


def test_near_anchor_window_lowest_wins():
    """C98USDT.P 回归（2026-10-12-8 恢复窗口口径）：窗口内前一根低点低于信号K →
    金K锚取 5 根共同极值，止损须越过它才算贴近；只越过信号K单根低点的旧口径
    被打回（旧口径止损落在近期波动区间内部，必被扫损）"""
    kl = _klines()
    kl[0][3] = 98.0  # 前一根低点低于信号K低点 99.0 → 窗口最低 = 98.0
    anchor = round(98.0 * 0.997, 8)
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=anchor,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(d, _signal(), kl)
    assert ok, violations
    assert fixed["stop_anchor"] == "near"

    # 旧单根口径止损（99.0×0.997≈98.703）距窗口锚（98.0×0.997≈97.706）≈1.0% → 打回
    d_old = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=round(99.0 * 0.997, 8),
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok2, violations2, _ = validate_decision(d_old, _signal(), kl)
    assert not ok2
    assert any("近锚" in v for v in violations2)


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


def test_near_on_pullback_type_lands_near():
    """回踩类 near（2026-10-12-5 距离分档后合法）：止损贴信号K极值 → 通过且落库 near"""
    anchor = round(99.0 * 0.997, 8)  # 信号K线最低价外 0.3%
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=anchor,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(
        d, _signal("trigger_pullback_confirm"), _klines(),
    )
    assert ok, violations
    assert fixed["stop_anchor"] == "near"


def test_near_on_pullback_type_far_stop_rejected():
    """回踩类 near 但止损远离信号K极值（挂到摆动低点）→ 一致性校验打回
    （2026-10-12-5 前：非突破类不校验、落库强制 structure；现校验与落库同步放开）"""
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=97.0,
        take_profit_1=105.0, take_profit_2=0.0,
        recommendation=60.0, analysis="r",
    )
    ok, violations, fixed = validate_decision(
        d, _signal("trigger_pullback_confirm"), _klines(),
    )
    assert not ok
    assert fixed == {}
    assert any("近锚" in v for v in violations)


def test_non_breakout_near_not_exempt_from_sl_floor(monkeypatch):
    """非突破类金K锚不豁免止损宽度下限（豁免仍限突破类近锚）：
    止损贴金K窗口极值但距入场 <0.3×ATR → 打回"""
    monkeypatch.setattr(settings, "SL_MIN_ATR", 0.3)
    kl = [  # 常规根 high-low=2 → ATR(14)=2 → 下限 0.6
        [i, 100.0, 101.0, 99.0, 100.0, 10.0, 0, 0.0] for i in range(40)
    ]
    # 窗口（最近 5 根已收盘 [34..38]）低点抬到 99.8：贴极值止损不触发近锚一致性
    # 违规，聚焦止损宽度下限（high-low=2 保持 TR=2、ATR 不变）
    for i in range(34, 39):
        kl[i] = [i, 100.0, 101.8, 99.8, 100.0, 10.0, 0, 0.0]
    anchor = round(99.8 * 0.997, 8)   # 99.5006，距入场 ≈0.4994 < 0.6
    d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        stop_anchor="near",
        entry_price=100.0, stop_loss=anchor,
        take_profit_1=100.8, take_profit_2=0.0,  # RR ≈1.6 够线，不引入其他违规
        recommendation=60.0, analysis="r",
    )
    ok, violations, _ = validate_decision(
        d, _signal("trigger_pullback_confirm"), kl,
    )
    assert not ok
    assert any("止损宽度过近" in v for v in violations)


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
