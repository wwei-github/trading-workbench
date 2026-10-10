"""金K形态清单与流星线检测测试

2026-09-28 看空金K：上吊线 → 流星线（倒锤形出现在顶部）。
回踩金K语义：看多回踩支撑配锤形线（长下影），看空反抽阻力配倒锤形线（长上影）。
2026-10-02 十字星口径：上下影线都很长、实体很短或几乎没有——都是十字星（中性）。
"""
from app.services.strategy.candlestick import (
    GOLDEN_PATTERNS,
    PATTERN_LABEL_MAP,
    detect_all_patterns,
    detect_dark_cloud_cover,
    detect_doji,
    detect_engulfing,
    detect_evening_star,
    detect_harami_breakout,
    detect_morning_star,
    detect_piercing_line,
    detect_shooting_star,
    resolve_golden_evidence,
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
    """金K清单按方向：2026-10-09 起看涨看跌各 4 种（孕线突破/蜻蜓/墓碑移出——
    信号质量弱，同 2026-09-12 精简口径；检测器保留仅历史展示。
    历史沿革：2026-10-01-3 曾恢复完整 12，倒锤形线维持退出（-2 KSM 拍板）、
    上吊线维持被流星线替代（09-28 拍板））"""
    bullish = {"hammer", "bullish_engulfing", "morning_star", "piercing_line"}
    bearish = {"shooting_star", "bearish_engulfing", "evening_star",
               "dark_cloud_cover"}
    assert GOLDEN_PATTERNS == bullish | bearish
    assert len(GOLDEN_PATTERNS) == 8
    assert "inverted_hammer" not in GOLDEN_PATTERNS
    assert "hanging_man" not in GOLDEN_PATTERNS
    # 2026-10-09 移出：孕线突破（两向）+ 蜻蜓/墓碑十字
    assert "bullish_harami" not in GOLDEN_PATTERNS
    assert "bearish_harami" not in GOLDEN_PATTERNS
    assert "dragonfly_doji" not in GOLDEN_PATTERNS
    assert "gravestone_doji" not in GOLDEN_PATTERNS
    for p in GOLDEN_PATTERNS:
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


# ── 十字星口径（2026-10-02 用户拍板）：双长影 + 实体很短/几乎没有 = 十字星 ──


def _flat_kl_then(candle):
    """9 根平盘 + 末根指定K线（单根形态判定，无多K复合语境）"""
    kl = _kl_list([100] * 9)
    kl.append([1700000090000, *candle, 10.0, 10.0])
    return kl


def test_long_legged_doji_tiny_body():
    """实体几乎没有 + 双长影 → 中性十字星，且不被误判进任何方向性金K"""
    # o=100 c=100.05 h=104.9 l=95.1：实体 0.5%，上影 49.5%/下影 50%
    kl = _flat_kl_then([100.0, 104.9, 95.1, 100.05])
    r = detect_doji(kl, idx=-1)
    assert r is not None
    assert r["pattern"] == "doji" and r["direction"] == "neutral"
    names = {p["pattern"] for p in detect_all_patterns(kl, idx=-1)}
    assert "hammer" not in names and "shooting_star" not in names
    assert "dragonfly_doji" not in names and "gravestone_doji" not in names


def test_small_body_both_shadows_doji():
    """实体很短（≤25% 全长）+ 双长影 → 同样归十字星（纺锤/高波浪一类拉锯K）"""
    # o=99.5 c=101 h=103 l=97：实体 25%，上影 33%/下影 42%
    kl = _flat_kl_then([99.5, 103.0, 97.0, 101.0])
    r = detect_doji(kl, idx=-1)
    assert r is not None
    assert r["pattern"] == "doji" and r["direction"] == "neutral"


def test_big_body_not_doji():
    """实体 >25% 总长不归十字星（锤形线家族/普通K线各归其位）"""
    # o=100 c=102 h=103 l=99：实体 50%，双侧影线各 25%
    kl = _flat_kl_then([100.0, 103.0, 99.0, 102.0])
    assert detect_doji(kl, idx=-1) is None


def test_doji_never_golden_evidence():
    """中性十字星不构成回踩金K证据（双向长影=方向未决，多空 wanted 均不确认）"""
    kl = _flat_kl_then([100.0, 104.9, 95.1, 100.05])
    patterns = detect_all_patterns(kl, idx=-1)
    for wanted in ("bullish", "bearish"):
        golden, _note = resolve_golden_evidence(kl, wanted, patterns)
        assert golden is None


def test_directional_doji_semantics_unchanged():
    """蜻蜓/墓碑判定不受影响：单侧长影 + 对侧影线几乎无仍各归其位"""
    dragonfly = _flat_kl_then([100.0, 100.5, 95.0, 100.05])  # 下影 91%/上影 8%
    r = detect_doji(dragonfly, idx=-1)
    assert r is not None and r["pattern"] == "dragonfly_doji" and r["direction"] == "bullish"
    gravestone = _flat_kl_then([100.0, 105.0, 99.5, 99.95])  # 上影 91%/下影 8%
    r = detect_doji(gravestone, idx=-1)
    assert r is not None and r["pattern"] == "gravestone_doji" and r["direction"] == "bearish"


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


def test_engulfing_tiny_engulfed_body_detected():
    """大实体吞小十字（WUSDT 2026-10-10 20:00 个案）：被吞没方实体 0.04
    （基线均值 1.0 的 4%）→ 2026-10-10 起被吞没方不设实体基线，判吞没
    （前根犹豫、当根决断=教科书最强吞没变体，body1 门曾整门误拒）"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.02, prev_close=99.98,
                             cur_open=99.9, cur_close=101.3,
                             cur_high=101.5, cur_low=99.7)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_engulfing"
    assert r["direction"] == "bullish"


def test_engulfing_tiny_mutual_bodies_still_rejected():
    """小实体互包（窄幅盘整噪声）：两根实体都远小于基线均值 →
    吞没K线（body2）实体基线门照常拦截"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.03, prev_close=99.97,
                             cur_open=99.96, cur_close=100.05,
                             cur_high=100.08, cur_low=99.93)
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


# ── 刺透线/乌云盖顶微跳空容差（2026-10-08：AKTUSDT 12:00→13:00 标准刺透
#    因开盘 +0.04% 打印差被零容差误拒，平开判定加 0.1% 容差）──

def _two_bar_piercing(cur_open: float) -> list[list]:
    """AKTUSDT 2026-10-08 12:00→13:00 线上实测复刻：
    前根阴线 o=0.7378 c=0.7274（mid=0.7326），当根阳线 c=0.7355（过中点、
    未吞没），cur_open 控制开盘位置"""
    return [
        [1700000100000, 0.7378, 0.7456, 0.7265, 0.7274, 10.0, 10.0],
        [1700000200000, cur_open, 0.7384, 0.7205, 0.7355, 10.0, 10.0],
    ]


def test_piercing_micro_gap_up_within_tolerance_detected():
    """开盘微高开 +0.04%（0.0003/0.7274，线上实测值）→ 视同平开，判刺透"""
    kl = _two_bar_piercing(cur_open=0.7277)
    r = detect_piercing_line(kl, idx=-1)
    assert r is not None
    assert r["pattern"] == "piercing_line" and r["direction"] == "bullish"


def test_piercing_flat_open_and_low_open_detected():
    """平开（=前收）与低开 → 照常判刺透（经典路径不回归）"""
    flat = _two_bar_piercing(cur_open=0.7274)
    assert detect_piercing_line(flat, idx=-1) is not None
    low = _two_bar_piercing(cur_open=0.7250)
    assert detect_piercing_line(low, idx=-1) is not None


def test_piercing_gap_at_tolerance_boundary_allowed():
    """开盘恰 +0.1%（容差边界，0.7274×1.001=0.7281274 → 取 0.7281）→ 放行"""
    kl = _two_bar_piercing(cur_open=0.7281)
    assert detect_piercing_line(kl, idx=-1) is not None


def test_piercing_real_gap_up_rejected():
    """真实高开 +0.5%（超出容差）→ 不成立（防用容差吞掉真跳空语义）"""
    kl = _two_bar_piercing(cur_open=0.7310)
    assert detect_piercing_line(kl, idx=-1) is None


def test_piercing_close_below_midpoint_still_rejected():
    """收盘未过前实体中点（c=0.7320 ≤ mid=0.7326）→ 照常拒（容差不放宽实体门）"""
    kl = [
        [1700000100000, 0.7378, 0.7456, 0.7265, 0.7274, 10.0, 10.0],
        [1700000200000, 0.7277, 0.7384, 0.7205, 0.7320, 10.0, 10.0],
    ]
    assert detect_piercing_line(kl, idx=-1) is None


def test_dark_cloud_micro_gap_down_within_tolerance_detected():
    """乌云盖顶镜像：开盘微低开 −0.04% → 视同平开，判乌云盖顶"""
    kl = [
        [1700000100000, 0.7274, 0.7384, 0.7265, 0.7378, 10.0, 10.0],  # 阳线 mid=0.7326
        [1700000200000, 0.7375, 0.7400, 0.7300, 0.7300, 10.0, 10.0],  # 阴线 o 低 0.0003，c 过中点下方
    ]
    r = detect_dark_cloud_cover(kl, idx=-1)
    assert r is not None
    assert r["pattern"] == "dark_cloud_cover" and r["direction"] == "bearish"


def test_dark_cloud_real_gap_down_rejected():
    """乌云盖顶镜像：真实低开 −0.5%（超出容差）→ 不成立"""
    kl = [
        [1700000100000, 0.7274, 0.7384, 0.7265, 0.7378, 10.0, 10.0],
        [1700000200000, 0.7341, 0.7400, 0.7300, 0.7300, 10.0, 10.0],
    ]
    assert detect_dark_cloud_cover(kl, idx=-1) is None


# ── 吞没微跳空容差（2026-10-08：o2 vs c1 同向门加 0.1% 容差，
#    c2 vs o1 收复门属真语义临界不放宽）──

def test_engulfing_bullish_micro_gap_up_detected():
    """看涨吞没：阳线开盘微高开 +0.04%（钉在前阴收盘=实体下沿的打印噪声）
    → 视同平开，判吞没（吞没体的 c2 ≥ o1 收复门独立满足）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=100.04, cur_close=101.3,
                             cur_high=101.5, cur_low=99.7)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_engulfing"


def test_engulfing_bullish_real_gap_up_rejected():
    """看涨吞没：真实高开 +0.5%（开盘在前阴实体中部，非包住）→ 不成立"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=100.5, cur_close=101.3,
                             cur_high=101.5, cur_low=100.2)
    assert detect_engulfing(kl, idx=-1) is None


def test_engulfing_bearish_micro_gap_down_detected():
    """看跌吞没镜像：阴线开盘微低开 −0.04% → 视同平开，判吞没"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.0, prev_close=101.2,
                             cur_open=101.16, cur_close=99.8,
                             cur_high=101.5, cur_low=99.6)
    r = detect_engulfing(kl, idx=-1)
    assert r is not None and r["pattern"] == "bearish_engulfing"


def test_engulfing_bearish_real_gap_down_rejected():
    """看跌吞没镜像：真实低开 +0.5%（开盘在前阳实体中部，非包住）→ 不成立"""
    kl = _append_engulf_pair(_engulf_base(), prev_open=100.0, prev_close=101.2,
                             cur_open=100.7, cur_close=99.8,
                             cur_high=101.5, cur_low=99.6)
    assert detect_engulfing(kl, idx=-1) is None


def test_engulfing_close_not_recovering_still_rejected():
    """看涨吞没：开盘噪声带内但收盘未收复前实体上沿（c2 < o1）→ 照常拒
    （容差不放宽收复门，避免吞没退化为刺透/孕线观感）"""
    kl = _append_engulf_pair(_engulf_base(), cur_open=100.04, cur_close=100.6,
                             cur_high=101.0, cur_low=99.7)
    assert detect_engulfing(kl, idx=-1) is None


# ── 孕线突破内包线微跳空容差（2026-10-08：内包线开盘必然钉在母线实体沿，
#    严格不等式约半数被噪声方向杀掉，母线边缘侧加 0.1% 容差）──

def _harami_with_inner(cur_inner_open: float):
    """母线阳线 o=100 c=102（实体沿 100/102）+ 内包线 o 可控 c=101.2
    （实体在母线内）+ 突破线收盘 102.4 上破母线实体高点"""
    kl = [
        [1700000100000, 100.0, 102.3, 99.7, 102.0, 10.0, 10.0],  # 母线
        [1700000200000, cur_inner_open, 101.5, 100.9, 101.2, 10.0, 10.0],  # 内包线
        [1700000300000, 101.3, 102.6, 101.0, 102.4, 10.0, 10.0],  # 突破线
    ]
    return kl


def test_harami_inner_open_pinned_to_mother_edge_detected():
    """内包线开盘钉在母线实体上沿（o2=101.9 < hi1=102 但仅差 0.05%）
    → 噪声带内判孕线突破"""
    kl = _harami_with_inner(cur_inner_open=101.9)
    r = detect_harami_breakout(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_harami"


def test_harami_inner_open_slightly_beyond_mother_edge_detected():
    """内包线开盘微出母线实体上沿 +0.04%（101.9→102.04，噪声方向随机出界）
    → 容差放行（修复前严格不等式必拒）"""
    kl = _harami_with_inner(cur_inner_open=102.04)
    r = detect_harami_breakout(kl, idx=-1)
    assert r is not None and r["pattern"] == "bullish_harami"


def test_harami_inner_body_truly_outside_still_rejected():
    """内包线实体真实出界（c2=102.3 > hi1×1.001）→ 照常拒（非孕线）"""
    kl = [
        [1700000100000, 100.0, 102.3, 99.7, 102.0, 10.0, 10.0],
        [1700000200000, 101.9, 102.5, 101.6, 102.3, 10.0, 10.0],  # 实体上沿出界 0.3
        [1700000300000, 102.4, 103.0, 102.0, 102.8, 10.0, 10.0],
    ]
    assert detect_harami_breakout(kl, idx=-1) is None
