"""质量分关键位修饰项单测（2026-09-27 接入筛选）。

覆盖：key_levels 缺省回落、同侧 flipped/intact 加分、双锚共振、
前方无锚惩罚（broken/invalid 不计锚、超 2×ATR 计惩罚）、空头对称。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy.technical import compute_quality_score  # noqa: E402


def _votes(net: int = 3) -> dict:
    """最小票数集：无 4h/RSI/OBV/布林修饰干扰（基础分 = 50 + net×7）"""
    return {
        "bull": ["MACD"] if net > 0 else [], "bear": [] if net > 0 else ["MACD"],
        "bull_n": max(net, 0), "bear_n": max(-net, 0),
        "net": net, "voted": 7,
        "results": [["MACD", {"dir": 1 if net > 0 else -1}]],
    }


def _klines(cur: float = 100.0) -> list[list]:
    """最小 K 线：klines[-2][4] 为信号K线收盘价（关键位修饰的参照价）"""
    return [[0, cur, cur, cur, cur, 0, 0], [1, cur, cur, cur, cur, 0, 0]]


def _lv(price: float, role: str, state: str = "intact") -> dict:
    return {"price": price, "sources": ["swing_1h"], "touches": 2,
            "rejections": 1, "kind": "low", "strength": 2, "state": state,
            "role": role, "flip_direction": None, "bars_since_event": 1,
            "distance_pct": 0.0}


def test_no_key_levels_baseline_unchanged():
    """key_levels 缺省（开关关闭/数据不足）→ 纯指标口径，行为不变"""
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=_klines()) == 71


def test_flipped_same_side_within_1atr_bonus():
    """同侧翻转已确认位 ≤1×ATR +8（上方另有 1.5×ATR intact 压力作前锚免疫惩罚）"""
    kls = _klines(100.0)
    levels = [_lv(99.5, "support", "flipped_confirmed"), _lv(101.5, "resistance")]
    score = compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                  klines=kls, key_levels=levels, atr=1.0)
    assert score == 71 + 8


def test_intact_same_side_within_1atr_bonus():
    """同侧仅 intact 位 ≤1×ATR +5（flipped 缺位时的次档）"""
    kls = _klines(100.0)
    levels = [_lv(99.5, "support"), _lv(101.5, "resistance")]
    score = compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                  klines=kls, key_levels=levels, atr=1.0)
    assert score == 71 + 5


def test_dual_touch_confluence_bonus():
    """双锚共振 +4：位表无同侧近位（仅远位），dual_touch=True"""
    kls = _klines(100.0)
    levels = [_lv(105.0, "resistance")]  # ahead 5×ATR → 惩罚 -6
    score = compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                  klines=kls, key_levels=levels, atr=1.0,
                                  dual_touch=True)
    assert score == 71 + 4 - 6


def test_no_ahead_anchor_penalty_and_broken_excluded():
    """前方 2×ATR 内无可锚位 -6：broken 位禁作止盈锚不计；intact 超距也不计"""
    kls = _klines(100.0)
    broken_only = [_lv(101.5, "resistance", "broken")]
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=kls, key_levels=broken_only, atr=1.0) \
        == 71 - 6
    far_intact = [_lv(103.0, "resistance")]  # 3×ATR > 2
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=kls, key_levels=far_intact, atr=1.0) \
        == 71 - 6


def test_short_side_symmetric():
    """空头对称：上方翻转压力位 +8（flipped_confirmed role=resistance），
    下方 1×ATR intact 支撑作前锚免疫惩罚"""
    kls = _klines(100.0)
    levels = [_lv(100.5, "resistance", "flipped_confirmed"), _lv(99.5, "support")]
    score = compute_quality_score(-1, _votes(-3), "trigger_pullback_confirm",
                                  klines=kls, key_levels=levels, atr=1.0)
    assert score == 71 + 8


def test_invalid_level_without_role_ignored():
    """invalid 位（role=None）完全不参选——不加分不构成前锚"""
    kls = _klines(100.0)
    levels = [{"price": 99.5, "state": "invalid", "role": None,
               "sources": [], "touches": 0, "rejections": 0, "kind": "low",
               "strength": 0, "flip_direction": None, "bars_since_event": 9,
               "distance_pct": 0.0}]
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=kls, key_levels=levels, atr=1.0) \
        == 71 - 6


def test_flipped_price_dropped_below_no_bonus():
    """翻转位价格已跌到位下方（role 仍 support、state 仍 flipped_confirmed）
    → 多头不拿回踩企稳加分；位在上方 1×ATR 内仍计前锚、免疫惩罚"""
    kls = _klines(100.0)
    levels = [_lv(100.5, "support", "flipped_confirmed")]
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kls, key_levels=levels, atr=1.0) \
        == 71
