"""金K形态清单与流星线检测测试

2026-09-28 看空金K：上吊线 → 流星线（倒锤形出现在顶部）。
回踩金K语义：看多回踩支撑配锤形线（长下影），看空反抽阻力配倒锤形线（长上影）。
"""
from app.services.strategy.candlestick import (
    GOLDEN_12,
    PATTERN_LABEL_MAP,
    detect_all_patterns,
    detect_engulfing,
    detect_evening_star,
    detect_morning_star,
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
    """金K清单按方向：完整 12 金K，看涨看跌各 6 种
    （2026-10-01-3：恢复 2026-09-12 精简掉的刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑；
    倒锤形线维持退出（-2 KSM 拍板）、上吊线维持被流星线替代（09-28 拍板））"""
    bullish = {"hammer", "bullish_engulfing", "morning_star",
               "piercing_line", "bullish_harami", "dragonfly_doji"}
    bearish = {"shooting_star", "bearish_engulfing", "evening_star",
               "dark_cloud_cover", "bearish_harami", "gravestone_doji"}
    assert GOLDEN_12 == bullish | bearish
    assert len(GOLDEN_12) == 12
    assert "inverted_hammer" not in GOLDEN_12
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


# ── 吞没影线质量门（2026-09-30 docs/25 §4：吞没K线总影线 > 全长 40% 不判金K）──

def _engulf_base(n: int = 24) -> list[list]:
    """横盘序列：每根实体 1.0（开=前收，阴阳交替），作为吞没实体基线
    （ENGULF_BODY_MIN_BARS=10、LOOKBACK=20 → n≥22 保证基线样本足够；
    末根收盘 100.0，与吞没对第一根价格衔接）"""
    out = []
    c = 100.0
    for i in range(n):
        o = c
        c = o + (1.0 if i % 2 == 0 else -1.0)
        out.append([1700000000000 + i * 3_600_000, o, max(o, c) + 0.1,
                    min(o, c) - 0.1, c, 10.0, 10.0])
    return out


def _append_engulf_pair(kl: list[list], *, cur_open: float, cur_close: float,
                        cur_high: float, cur_low: float,
                        prev_open: float = 101.2, prev_close: float = 100.0) -> list[list]:
    """追加吞没对（前根 + 当根）：前根默认阴线 o=101.2 c=100.0（实体 1.2 > 基线 1.0）"""
    n = len(kl)
    ts = 1700000000000 + n * 3_600_000
    kl.append([ts, prev_open, max(prev_open, prev_close) + 0.1,
               min(prev_open, prev_close) - 0.1, prev_close, 10.0, 10.0])
    kl.append([ts + 3_600_000, cur_open, cur_high, cur_low, cur_close, 10.0, 10.0])
    return kl


def test_engulfing_clean_shadow_detected():
    """正常吞没（总影线 0.3/1.8 ≈17% ≤ 40%）→ 照常判金K（门不误伤）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=99.9, cur_close=101.3,
                             cur_high=101.5, cur_low=99.7)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_engulfing"
    assert r["direction"] == "bullish"


def test_engulfing_long_shadow_rejected():
    """看涨吞没长上影（upper 1.0/rng 2.6 ≈38% > 15% 上影门）→ 不判金K
    （冲高遭抛压打回=犹豫非决断；2026-10-01 起看涨侧按上影门拦截）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=99.9, cur_close=101.3,
                             cur_high=102.3, cur_low=99.7)
    assert detect_engulfing(kl, idx=-1) is None


def test_engulfing_bullish_upper_shadow_boundary_15pct_allowed():
    """看涨吞没上影恰 15%（upper 0.3/rng 2.0）→ 边界放行（门只拦 >15%）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=99.9, cur_close=101.3,
                             cur_high=101.6, cur_low=99.6)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_engulfing"


def test_engulfing_bullish_long_lower_shadow_allowed():
    """看涨吞没长下影：下影 0.9、总影线 1.0/2.4≈42%（旧 40% 总影线门会拦）
    但上影 0.1/2.4≈4% ≤15% → 放行（2026-10-01 不对称化：探底回升=承接强）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=99.9, cur_close=101.3,
                             cur_high=101.4, cur_low=99.0)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_engulfing"


def test_engulfing_bearish_clean_shadow_detected():
    """空头正常吞没（总影线 0.4/1.9 ≈21%）→ 照常判金K"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.0, prev_close=101.2,
                             cur_open=101.3, cur_close=99.8,
                             cur_high=101.5, cur_low=99.6)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bearish_engulfing"
    assert r["direction"] == "bearish"


def test_engulfing_bearish_long_shadow_rejected():
    """空头对称：长影看跌吞没同样不判金K"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.0, prev_close=101.2,
                             cur_open=101.3, cur_close=99.8,
                             cur_high=102.3, cur_low=99.7)
    assert detect_engulfing(kl, idx=-1) is None


# ── 启明星/黄昏星决断K线影线门（2026-09-30：第三根总影线 > 全长 40% 不判金K）──

def _star_first_two(k1: list, k2: list) -> list[list]:
    """形态前两根K线 + 基线样本（星实体门需要 avg(body1, body3)，样本由第三根补足）"""
    return [k1, k2]


def test_morning_star_clean_third_candle_detected():
    """正常启明星：阳线收盘过中点、总影线 0.4/1.8 ≈22% ≤ 40% → 判金K"""
    kl = _star_first_two(
        [1700000100000, 102.0, 102.3, 99.7, 100.0, 10.0, 10.0],   # 阴线实体 2
        [1700000200000, 99.9, 100.1, 99.5, 99.7, 10.0, 10.0],     # 星实体 0.2
    )
    kl.append([1700000300000, 99.8, 101.4, 99.6, 101.2, 10.0, 10.0])  # 阳线 body1.4
    r = detect_morning_star(kl, idx=-1)
    assert r is not None and r["pattern"] == "morning_star"
    assert r["direction"] == "bullish"


def test_morning_star_long_shadow_third_candle_rejected():
    """第三根长上影（总影线 1.8/3.2 ≈56% > 40%）：收盘仍过中点但冲高遭抛压打回
    → 不判金K（犹豫非决断，仅影线门拦截）"""
    kl = _star_first_two(
        [1700000100000, 102.0, 102.3, 99.7, 100.0, 10.0, 10.0],
        [1700000200000, 99.9, 100.1, 99.5, 99.7, 10.0, 10.0],
    )
    kl.append([1700000300000, 99.8, 102.8, 99.6, 101.2, 10.0, 10.0])
    assert detect_morning_star(kl, idx=-1) is None


def test_morning_star_shadow_boundary_40pct_allowed():
    """第三根总影线恰 40%（body 1.2 / rng 2.0）→ 边界放行（门只拦 > 40%）"""
    kl = _star_first_two(
        [1700000100000, 102.0, 102.3, 99.7, 100.0, 10.0, 10.0],
        [1700000200000, 99.9, 100.1, 99.5, 99.7, 10.0, 10.0],
    )
    # o=100.2 c=101.4（body 1.2，过中点 101），l=99.6 h=101.6（rng 2.0，影线 0.8=40%）
    kl.append([1700000300000, 100.2, 101.6, 99.6, 101.4, 10.0, 10.0])
    r = detect_morning_star(kl, idx=-1)
    assert r is not None and r["pattern"] == "morning_star"


def test_morning_star_fat_mid_body_rejected():
    """中K实体 0.7 = 外侧均值×0.41：2026-10-01 收紧（>0.3）→ 不判
    （2-3 根组合呈看涨吞没观感；旧口径 0.5 会放行）"""
    kl = _star_first_two(
        [1700000100000, 102.0, 102.3, 99.7, 100.0, 10.0, 10.0],   # 阴线实体 2
        [1700000200000, 100.1, 100.4, 99.2, 99.4, 10.0, 10.0],    # 中K实体 0.7（阴）
    )
    kl.append([1700000300000, 99.8, 101.4, 99.6, 101.2, 10.0, 10.0])  # 阳线 body1.4
    assert detect_morning_star(kl, idx=-1) is None


def test_evening_star_clean_third_candle_detected():
    """正常黄昏星：阴线收盘破中点、总影线 0.4/1.8 ≈22% → 判金K"""
    kl = _star_first_two(
        [1700000100000, 100.0, 102.3, 99.7, 102.0, 10.0, 10.0],   # 阳线实体 2
        [1700000200000, 102.1, 102.5, 101.9, 102.3, 10.0, 10.0],  # 星实体 0.2
    )
    kl.append([1700000300000, 102.2, 102.4, 100.6, 100.8, 10.0, 10.0])  # 阴线 body1.4
    r = detect_evening_star(kl, idx=-1)
    assert r is not None and r["pattern"] == "evening_star"
    assert r["direction"] == "bearish"


def test_evening_star_long_shadow_third_candle_rejected():
    """空头对称：第三根长下影（总影线 1.6/3.0 ≈53% > 40%）→ 不判金K"""
    kl = _star_first_two(
        [1700000100000, 100.0, 102.3, 99.7, 102.0, 10.0, 10.0],
        [1700000200000, 102.1, 102.5, 101.9, 102.3, 10.0, 10.0],
    )
    kl.append([1700000300000, 102.2, 102.4, 99.4, 100.8, 10.0, 10.0])
    assert detect_evening_star(kl, idx=-1) is None


def test_evening_star_fat_mid_body_rejected():
    """中K实体 0.7 = 外侧均值×0.41：2026-10-01 与启明星对齐收紧（>0.3）→ 不判
    （2-3 根组合呈看跌吞没观感；旧口径 0.5 会放行）"""
    kl = _star_first_two(
        [1700000100000, 100.0, 102.3, 99.7, 102.0, 10.0, 10.0],   # 阳线实体 2
        [1700000200000, 102.3, 102.6, 101.6, 101.6, 10.0, 10.0],  # 中K阴线 body 0.7
    )
    kl.append([1700000300000, 102.2, 102.4, 100.6, 100.8, 10.0, 10.0])  # 阴线 body1.4
    assert detect_evening_star(kl, idx=-1) is None
