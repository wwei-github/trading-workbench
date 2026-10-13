"""质量分纯指标口径基线单测。

关键位修饰项已随关键位子系统整体移除（2026-09-28），compute_quality_score
回归纯指标口径：50 + net×7 + 修饰，不再接收 key_levels/atr/dual_touch。
放量加分（2026-09-29）：三类触发统一为加分项（≥量比门槛 +5），突破不再卡死。
看涨吞没上影短加分（2026-10-13）：上影硬门改加分项（≤全长 25% +3），
不再因上影长拒判形态。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy.indicators import volume_ratio  # noqa: E402
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
    """最小 K 线：klines[-2][4] 为信号K线收盘价"""
    return [[0, cur, cur, cur, cur, 0, 0], [1, cur, cur, cur, cur, 0, 0]]


def _vol_klines(sig_vol: float, base: float = 100.0) -> list[list]:
    """22 根已收盘 + 1 根未收盘；最新已收盘根（closed[-1]）量 = sig_vol，
    比对窗口 closed[-21:-1] = 1 根 5 倍量堆 + 19 根 base（打歪均值口径）"""
    kl = [[i, 100.0, 101.0, 99.0, 100.0, base, base] for i in range(22)]
    kl[1] = [1, 100.0, 101.0, 99.0, 100.0, base * 5, base * 5]  # 量堆极端根
    kl.append([22, 100.0, 101.0, 99.0, 100.0, base, base])  # 未收盘根
    kl[-2] = [21, 100.0, 101.0, 99.0, 100.0, sig_vol, sig_vol]  # 信号根（最新已收盘）
    return kl


def test_baseline_score_unchanged():
    """无关键位修饰的纯指标基线：net=3 → 50 + 21 = 71"""
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=_klines()) == 71


def test_extra_params_rejected():
    """关键位时代的 key_levels/atr/dual_touch 参数已移除，传入应 TypeError"""
    import pytest
    with pytest.raises(TypeError):
        compute_quality_score(1, _votes(), "trigger_candlestick",
                              klines=_klines(), key_levels=[], atr=1.0)


def test_volume_ratio_median_robust_to_volume_spike():
    """中位数口径：20 根里 1 根 5 倍量堆不打歪基准——信号根 120/中位 100=1.2 达标
    （均值口径 = 120/120=1.0 会被量堆压成不达标）"""
    assert volume_ratio(_vol_klines(120.0)) == 1.2


def test_volume_ratio_insufficient_data():
    """已收盘根 < 22 → 0（数据不足不放量）"""
    assert volume_ratio(_klines()) == 0.0


def test_breakout_volume_bonus():
    """突破类放量 +5（2026-09-29 起放量降为加分项，突破类统一参与）"""
    kl = _vol_klines(120.0)
    assert compute_quality_score(1, _votes(), "trigger_breakout", klines=kl) == 76
    assert compute_quality_score(1, _votes(), "trigger_trend_breakout", klines=kl) == 76


def test_pullback_confirm_volume_bonus_kept():
    """回踩金K确认放量 +5 维持（2026-09-25 原有规则）"""
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_vol_klines(120.0)) == 76


def test_low_volume_no_bonus():
    """缩量（量比 < 1.2）不加不减：基线 71；突破类缩量仍产生信号由 trigger 层放行"""
    assert compute_quality_score(1, _votes(), "trigger_breakout",
                                 klines=_vol_klines(90.0)) == 71
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_vol_klines(90.0)) == 71


def _engulf_klines(high: float, low: float,
                   o: float = 99.9, c: float = 101.3) -> list[list]:
    """[-2] 为看涨吞没信号根（检测层几何：前根阴 101.2→100.0、当根阳 99.9→101.3
    完整包住），[-1] 未收盘根；high/low 控制上影比（上影 = high − c）"""
    return [
        [0, 101.2, 101.3, 99.9, 100.0, 10.0, 10.0],
        [1, o, high, low, c, 10.0, 10.0],
        [2, c, c, c, c, 10.0, 10.0],
    ]


def test_bullish_engulfing_short_upper_shadow_bonus():
    """看涨吞没上影短 +3（2026-10-13 硬门改加分项）：上影 0.2/全长 2.0=10%
    ≤25% → 71+3=74；不传 pattern / 其他形态名不加（仅看涨吞没专属）"""
    kl = _engulf_klines(101.5, 99.5)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="bullish_engulfing") == 74
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl) == 71
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="hammer") == 71


def test_bullish_engulfing_upper_shadow_boundary_25pct_bonus():
    """上影恰 25%（0.5/2.0）→ 边界计加分（加分线口径 ≤25%；检测层 2026-10-13
    起该值不再拦截、照判形态）"""
    kl = _engulf_klines(101.8, 99.8)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="bullish_engulfing") == 74


def test_bullish_engulfing_long_upper_shadow_no_bonus():
    """上影长（1.0/2.6≈38% >25%）只失加分：71 不加分也不拦截（旧 25% 硬门
    时代此形态连候选都不产生）"""
    kl = _engulf_klines(102.3, 99.7)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="bullish_engulfing") == 71
