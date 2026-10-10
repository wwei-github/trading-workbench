"""trail RR 条件豁免（2026-10-10 拍板 B，docs/20 §四）回归

背景：trail 哲学 = 不设固定目标、让利润奔跑；固定目标 RR 是 ladder 语义的
代理指标。BTWUSDT 2026-10-10 21:55 实测：入场≈1.585 / 止损 1.5154（4.4%）/
前高 1.660 → RR 1.09，1.5×ATR 延伸也仅 1.39——强趋势+宽止损盘面全部被
1.5R 铁律堵死。拍板：tp_mode=trail 且 4h 排列 medium/strong/extreme 且
柱线同向时豁免（资格与取舍提示行共用 RR_TRADEOFF_PHASES 单一来源）；
宽止损风险由 max_stop_pct 硬上限 + 固定亏损法缩仓独立兜底。
"""
from app.services.risk_guard import TradeDecision, validate_decision
from app.services.strategy.technical import RR_TRADEOFF_PHASES

# 资格 dict 直传（analyze_htf_background 返回值的等价最小形态）
_BG_STRONG = {"phase": "strong", "momentum": "same"}
_BG_WEAK = {"phase": "weak", "momentum": "same"}
_BG_FLIP = {"phase": "strong", "momentum": "opposite"}
_BG_EXHAUSTED = {"phase": "exhausted", "momentum": "opposite"}


def _d(tp_mode="trail", entry=100.0, stop=96.0, tp1=103.0):
    """rr = |tp1-entry| / |entry-stop| = 3/4 = 0.75，低于任何常规铁律下限"""
    return TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        tp_mode=tp_mode, entry_price=entry, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=0.0, recommendation=80,
    )


# ── 1. 豁免路径 ──────────────────────────────────────────────────


def test_trail_exempt_in_strong_same_bg():
    """trail + 强排列同向 → RR 0.75 放行，落库复算值如实为 0.75"""
    ok, violations, fixed = validate_decision(
        _d(), {}, [], rr_min=1.5, htf_bg=_BG_STRONG,
    )
    assert ok, violations
    assert fixed["tp_mode"] == "trail"
    assert fixed["risk_reward_ratio"] == 0.75  # 复算值落库，不虚报


def test_trail_exempt_all_qualifying_phases():
    """medium/strong/extreme 三档同向全豁免（与取舍提示行同一相位集合）"""
    for phase in RR_TRADEOFF_PHASES:
        ok, violations, _ = validate_decision(
            _d(), {}, [], rr_min=1.5, htf_bg={"phase": phase, "momentum": "same"},
        )
        assert ok, (phase, violations)


# ── 2. 不豁免路径（缺证从严）─────────────────────────────────────


def test_trail_not_exempt_weak_bg():
    ok, violations, _ = validate_decision(
        _d(), {}, [], rr_min=1.5, htf_bg=_BG_WEAK,
    )
    assert not ok
    assert any("盈亏比复算" in v for v in violations)


def test_trail_not_exempt_opposite_momentum():
    ok, violations, _ = validate_decision(
        _d(), {}, [], rr_min=1.5, htf_bg=_BG_FLIP,
    )
    assert not ok


def test_trail_not_exempt_exhausted():
    ok, _, _ = validate_decision(
        _d(), {}, [], rr_min=1.5, htf_bg=_BG_EXHAUSTED,
    )
    assert not ok


def test_trail_not_exempt_without_htf_bg():
    """htf_bg=None（调用方无 4h 数据）→ 不豁免"""
    ok, violations, _ = validate_decision(_d(), {}, [], rr_min=1.5)
    assert not ok
    assert any("盈亏比复算" in v for v in violations)


def test_trail_rejection_hint_mentions_qualification():
    """trail 被拒的回炉反馈点明豁免资格，便于模型修正（Reflection）"""
    _, violations, _ = validate_decision(
        _d(), {}, [], rr_min=1.5, htf_bg=_BG_WEAK,
    )
    assert any("trail 仅在" in v and "豁免" in v for v in violations)


# ── 3. 豁免仅放宽 RR，其余校验不受影响 ───────────────────────────


def test_ladder_never_exempt():
    """ladder + 强背景 → 照常打回（豁免只对 trail）"""
    ok, _, _ = validate_decision(
        _d(tp_mode="ladder"), {}, [], rr_min=1.5, htf_bg=_BG_STRONG,
    )
    assert not ok


def test_none_tp_mode_falls_back_ladder_not_exempt():
    ok, _, _ = validate_decision(
        _d(tp_mode=None), {}, [], rr_min=1.5, htf_bg=_BG_STRONG,
    )
    assert not ok


def test_price_order_check_still_enforced_when_exempt():
    """豁免只放宽 RR——多单价格序矛盾（TP1<entry）在强背景下照样拒绝"""
    d = _d(tp_mode="trail", entry=100.0, stop=96.0, tp1=95.0)
    ok, violations, _ = validate_decision(
        d, {}, [], rr_min=1.5, htf_bg=_BG_STRONG,
    )
    assert not ok
    assert any("价格序矛盾" in v for v in violations)
