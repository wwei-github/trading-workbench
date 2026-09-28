"""金K形态清单与流星线检测测试

2026-09-28 看空金K：上吊线 → 流星线（倒锤形出现在顶部）。
回踩金K语义：看多回踩支撑配锤形线（长下影），看空反抽阻力配倒锤形线（长上影）。
"""
from app.services.strategy.candlestick import (
    GOLDEN_12,
    PATTERN_LABEL_MAP,
    detect_all_patterns,
    detect_shooting_star,
)


def _kl_list(closes, o=None, h=None, l=None):
    """构造最小 K 线序列 [[open_time, open, high, low, close, vol, qv], ...]"""
    n = len(closes)
    out = []
    for i in range(n):
        op = o[i] if o else closes[i]
        hi = h[i] if h else max(op, closes[i])
        lo = l[i] if l else min(op, closes[i])
        out.append(
            [1700000000000 + i * 3600_000, op, hi, lo, closes[i], 10.0, 10.0]
        )
    return out


def test_golden_sets_by_direction():
    """金K清单按方向各三种：看多锤/吞没/启明星，看空流星/吞没/黄昏星"""
    bullish = {"hammer", "bullish_engulfing", "morning_star"}
    bearish = {"shooting_star", "bearish_engulfing", "evening_star"}
    assert GOLDEN_12 == bullish | bearish
    assert "hanging_man" not in GOLDEN_12
    for p in GOLDEN_12:
        assert p in PATTERN_LABEL_MAP


def _uptrend_kl_with_signal_candle():
    """上涨趋势后收一根流星线信号K：长上影冲高回落，收盘处于区间下部"""
    # 9 根稳步上涨的 K 线（idx 0..8），第 10 根（idx 9，末根已收盘）为流星线
    kl = _kl_list([100, 101, 102, 103, 104, 105, 106, 107, 108])
    # 流星线：小实体，长上影（上影 >= 2*实体），下影极短，收盘回到区间下部
    kl.append([1700000090000, 108.9, 112.0, 108.7, 108.8, 10.0, 10.0])
    return kl


def test_shooting_star_detected_bearish():
    kl = _uptrend_kl_with_signal_candle()
    r = detect_shooting_star(kl, idx=-1)
    assert r is not None
    assert r["pattern"] == "shooting_star"
    assert r["direction"] == "bearish"
    # 双源校验路径：detect_all_patterns 应含流星线且标 bearish
    names = [(p["pattern"], p["direction"]) for p in detect_all_patterns(kl, idx=-1)]
    assert ("shooting_star", "bearish") in names


def test_shooting_star_shape_pair_semantics():
    """倒锤形同形双报（与锤形线/上吊线对一致）：形状检测器无位置概念

    顶部=压力位（2026-09-28 用户口径）的锚定语境由触发层保证（方向门控 +
    信号K线触碰 EMA21 收回防守侧 + 悬空拦截）：下跌后反抽到压力位的
    倒锤形（收盘往往不在区间高位）正是要捕捉的看空形态，位置门会漏报。
    """
    # 下跌后反抽倒锤形：同形双报——倒锤形线（bullish 展示）+ 流星线（bearish 信号）
    kl_down = _kl_list([108, 107, 106, 105, 104, 103, 102, 101, 100])
    kl_down.append([1700000090000, 99.1, 102.2, 98.9, 99.2, 10.0, 10.0])
    dirs = {p["pattern"]: p["direction"] for p in detect_all_patterns(kl_down, idx=-1)}
    assert dirs.get("inverted_hammer") == "bullish"
    assert dirs.get("shooting_star") == "bearish"

    # 上涨后同形：同样双报，看空触发按方向取流星线
    kl_up = _uptrend_kl_with_signal_candle()
    dirs_up = {p["pattern"]: p["direction"] for p in detect_all_patterns(kl_up, idx=-1)}
    assert dirs_up.get("shooting_star") == "bearish"
