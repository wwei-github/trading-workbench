"""质量分纯指标口径基线单测。

关键位修饰项已随关键位子系统整体移除（2026-09-28），compute_quality_score
回归纯指标口径：50 + net×7 + 修饰，不再接收 key_levels/atr/dual_touch。
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
    """最小 K 线：klines[-2][4] 为信号K线收盘价"""
    return [[0, cur, cur, cur, cur, 0, 0], [1, cur, cur, cur, cur, 0, 0]]


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
