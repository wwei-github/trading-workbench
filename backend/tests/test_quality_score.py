"""质量分纯指标口径基线单测。

关键位修饰项已随关键位子系统整体移除（2026-09-28），compute_quality_score
回归纯指标口径：50 + net×10 + 修饰，不再接收 key_levels/atr/dual_touch
（2026-10-15 指标拆角色：net=方向票净差∈{1,2,3}，基础分改 net×10 保持量级）。
放量加分（2026-09-29）：三类触发统一为加分项（≥量比门槛 +5），突破不再卡死。
决断K线影线梯度加分（2026-10-16）：吞没/启明星/黄昏星三处影线硬门全部删除，
改按影线比梯度加分（≤10% +5、≤20% +3、≤30% +1、>30% 0，只加分不拒判形态）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy.indicators import volume_ratio  # noqa: E402
from app.services.strategy.technical import compute_quality_score  # noqa: E402


def _votes(net: int = 3) -> dict:
    """最小票数集：无 4h/RSI/OBV/布林修饰干扰（基础分 = 50 + net×10）"""
    return {
        "bull": ["MACD", "布林带", "RSI"][:max(net, 0)]
        if net > 0 else [],
        "bear": ["MACD"] if net < 0 else [],
        "bull_n": max(net, 0), "bear_n": max(-net, 0),
        "net": net, "voted": 3,
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
    """无关键位修饰的纯指标基线：net=3 → 50 + 30 = 80"""
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=_klines()) == 80


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
    assert compute_quality_score(1, _votes(), "trigger_breakout", klines=kl) == 85
    assert compute_quality_score(1, _votes(), "trigger_trend_breakout", klines=kl) == 85


def test_pullback_confirm_volume_bonus_kept():
    """回踩金K确认放量 +5 维持（2026-09-25 原有规则）"""
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_vol_klines(120.0)) == 85


def test_low_volume_no_bonus():
    """缩量（量比 < 1.2）不加不减：基线 80；突破类缩量仍产生信号由 trigger 层放行"""
    assert compute_quality_score(1, _votes(), "trigger_breakout",
                                 klines=_vol_klines(90.0)) == 80
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_vol_klines(90.0)) == 80


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
    """看涨吞没上影梯度加分（2026-10-16 三档口径）：上影 0.1/全长 2.0=5%
    ≤10% → +5=85；不传 pattern / 非映射形态名不加（仅决断类金K生效）"""
    kl = _engulf_klines(101.4, 99.4)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="bullish_engulfing") == 85
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl) == 80
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="hammer") == 80


def test_bullish_engulfing_upper_shadow_grades():
    """上影梯度中间档：0.3/2.0=15%（≤20%）→ +3=83；0.5/2.0=25%
    （旧二值加分边界，现落 ≤30% 档）→ +1=81"""
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_engulf_klines(101.6, 99.6),
                                 pattern="bullish_engulfing") == 83
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_engulf_klines(101.8, 99.8),
                                 pattern="bullish_engulfing") == 81


def test_bullish_engulfing_long_upper_shadow_no_bonus():
    """上影长（1.0/2.6≈38% >30%）只失加分：80 不加分也不拦截形态"""
    kl = _engulf_klines(102.3, 99.7)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="bullish_engulfing") == 80


def test_decision_shadow_bonus_thresholds_inclusive():
    """档位阈值取闭区间（≤10% +5、≤20% +3、≤30% +1）：几何用二进制可精确表示
    （全长 2.5、上影 0.25/0.5/0.75）直接验证 decision_shadow_bonus 边界不丢档"""
    from app.services.strategy.candlestick import decision_shadow_bonus

    def _k(high: float, low: float) -> list[list]:
        return [[0, 99.5, 99.5, 99.5, 99.5, 1.0, 1.0],
                [1, 99.5, high, low, 101.5, 1.0, 1.0],
                [2, 101.5, 101.5, 101.5, 101.5, 1.0, 1.0]]

    assert decision_shadow_bonus("bullish_engulfing", _k(101.75, 99.25)) == 5  # 恰 10%
    assert decision_shadow_bonus("bullish_engulfing", _k(102.0, 99.5)) == 3    # 恰 20%
    assert decision_shadow_bonus("bullish_engulfing", _k(102.25, 99.75)) == 1  # 恰 30%
    assert decision_shadow_bonus("bullish_engulfing", _k(102.3, 99.7)) == 0    # >30%


def _bear_engulf_klines(high: float, low: float) -> list[list]:
    """[-2] 为看跌吞没信号根（检测层几何：前根阳 100.0→101.2、当根阴 101.3→100.0
    完整包住），[-1] 未收盘根；high/low 控制下影比（下影 = min(o,c) − low）"""
    return [
        [0, 100.0, 101.2, 99.9, 101.2, 10.0, 10.0],
        [1, 101.3, high, low, 100.0, 10.0, 10.0],
        [2, 100.0, 100.0, 100.0, 100.0, 10.0, 10.0],
    ]


def test_bearish_engulfing_lower_shadow_grades():
    """看跌吞没下影梯度三档（2026-10-16 新增，沿用看涨镜像口径）：全长恒 2.0，
    下影 0.1=5% → +5=85；0.3=15% → +3=83；0.5=25% → +1=81；0.8=40% → 0=80"""
    sig = (-1, _votes(-3))
    for low, high, bonus in ((99.9, 101.9, 5), (99.7, 101.7, 3),
                             (99.5, 101.5, 1), (99.2, 101.2, 0)):
        assert compute_quality_score(*sig, "trigger_pullback_confirm",
                                     klines=_bear_engulf_klines(high, low),
                                     pattern="bearish_engulfing") == 80 + bonus


def _star_klines(high: float, low: float) -> list[list]:
    """[-2] 为启明星/黄昏星第三根（决断K线，几何 99.0→101.0 阳线、实体 2.0），
    前两根为占位；high/low 控制总影线比（总影线 = 全长 − 实体）"""
    return [
        [0, 101.0, 101.0, 99.0, 99.0, 10.0, 10.0],
        [1, 99.2, 99.4, 99.0, 99.2, 10.0, 10.0],
        [2, 99.0, high, low, 101.0, 10.0, 10.0],
        [3, 101.0, 101.0, 101.0, 101.0, 10.0, 10.0],
    ]


def test_star_third_candle_total_shadow_grades():
    """启明星/黄昏星第三根总影线梯度三档（2026-10-16 统一口径）：实体恒 2.0，
    全长 2.1=5% → +5=85；2.3=13% → +3=83；2.6=23% → +1=81；3.2=38% → 0=80"""
    for high, low, bonus in ((101.1, 99.0, 5), (101.3, 99.0, 3),
                             (101.6, 99.0, 1), (101.6, 98.4, 0)):
        for pattern, direction, net in (("morning_star", 1, 3),
                                        ("evening_star", -1, -3)):
            assert compute_quality_score(
                direction, _votes(net), "trigger_pullback_confirm",
                klines=_star_klines(high, low), pattern=pattern) == 80 + bonus


# ── 金K组合价格带结构点密度加分（2026-10-13-6；-7 改实体重叠口径）────────────


def _band_klines(sig_high: float, sig_low: float,
                 prev_high: float = 99.0, prev_low: float = 97.0,
                 sw_low_body: tuple = (95.0, 95.0),
                 sw_high_body: tuple = (103.0, 103.0)) -> list[list]:
    """20 根已收盘 + 1 未收盘；固定 2 个摆动点（idx7 低=95、idx12 高=103）。
    信号根 = closed[-1] = idx19（high/low 由入参给，决定单根价格带）；prev = idx18
    供组合金K带（golden_combo）。sw_low_body/sw_high_body = 两个摆动K线的 (open,
    close)，控制其实体（默认实体落在各自极值点，即实体重叠≈极值点落带内）；
    其余K线实体恒 [100,100]。成交恒等量 → 量比 1.0；已收盘 20 根 <22 →
    volume_ratio 返回 0（两类放量加分均不干扰，基线恒 80）"""
    highs = [101, 101, 101, 101, 101, 99, 99, 98, 99, 99,
             99, 99, 103, 99, 99, 99, 99, 99, prev_high, sig_high]
    lows = [100, 100, 100, 100, 100, 97, 96, 95, 96, 97,
            97, 97, 97, 97, 97, 97, 97, 97, prev_low, sig_low]
    kl = [[i, 100.0, float(highs[i]), float(lows[i]), 100.0, 10.0, 10.0]
          for i in range(20)]
    kl[7][1], kl[7][4] = float(sw_low_body[0]), float(sw_low_body[1])
    kl[12][1], kl[12][4] = float(sw_high_body[0]), float(sw_high_body[1])
    kl.append([20, 100.0, sig_high, sig_low, 100.0, 10.0, 10.0])  # 未收盘根
    return kl


def test_golden_zone_swing_count_band():
    """价格带内摆动K线实体重叠计数：窄带 [97,99] 与 idx7 实体[95,95]/idx12
    实体[103,103] 均不相交 → 0；宽带 [94,104] 两点实体均落带内 → 2"""
    from app.services.strategy.technical import golden_zone_swing_count
    assert golden_zone_swing_count(_band_klines(99.0, 97.0)) == 0
    assert golden_zone_swing_count(_band_klines(104.0, 94.0)) == 2


def test_golden_zone_swing_count_body_overlap_not_point():
    """口径 = 摆动K线实体与价格带重叠（2026-10-13-7），不再要求极值点本身落带内：
    ① 摆动高点极值 103 落带内、其实体 [98,99] 在带外 → 不计（旧「极值点落带内」
    口径会误计）；② 极值 103 在带外、实体 [100,101] 与带 [100.5,102] 重叠 → 计入
    （旧口径不计）"""
    from app.services.strategy.technical import golden_zone_swing_count
    # ① 带 [102,104]：含极值 103，但 idx12 实体 [98,99]、idx7 实体 [95,95] 均不相交 → 0
    assert golden_zone_swing_count(
        _band_klines(104.0, 102.0, sw_high_body=(98.0, 99.0))) == 0
    # ② 带 [100.5,102]：不含极值 103，但 idx12 实体 [100,101] 与带相交 → 1
    assert golden_zone_swing_count(
        _band_klines(102.0, 100.5, sw_high_body=(100.0, 101.0))) == 1


def test_golden_zone_swing_count_combo_widens_band():
    """组合金K（golden_combo=True）价格带并入前根 idx18 → 单根窄带 0 变组合 2"""
    from app.services.strategy.technical import golden_zone_swing_count
    kl = _band_klines(99.0, 97.0, prev_high=104.0, prev_low=94.0)
    assert golden_zone_swing_count(kl, golden_combo=False) == 0
    assert golden_zone_swing_count(kl, golden_combo=True) == 2


def test_quality_score_golden_zone_bonus_single():
    """回踩金K确认 + 单根价格带含 2 点 → 80 + 2×3 = 86；窄带不含点 → 80"""
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_band_klines(104.0, 94.0),
                                 pattern="hammer") == 86
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=_band_klines(99.0, 97.0),
                                 pattern="hammer") == 80


def test_quality_score_golden_zone_bonus_combo():
    """组合金K（golden_combo=True）用两根包络 → 2 点 +6=86；不带标志按单根 0 点=80"""
    kl = _band_klines(99.0, 97.0, prev_high=104.0, prev_low=94.0)
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm", klines=kl,
                                 pattern="hammer", golden_combo=True) == 86
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm", klines=kl,
                                 pattern="hammer", golden_combo=False) == 80


def test_quality_score_golden_zone_only_for_pullback_confirm():
    """仅回踩金K确认适用：突破触发 / 无 pattern（金K缺失的 upsert 快照）均不加分"""
    kl = _band_klines(104.0, 94.0)
    assert compute_quality_score(1, _votes(), "trigger_breakout",
                                 klines=kl, pattern="hammer") == 80
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl) == 80


def test_quality_score_golden_zone_cap_12():
    """带内点多时封顶 +12：正弦锯齿全峰谷落入宽带 → 80 + 12 = 92
    （2026-10-27 CARD-10 升格后：收盘价恒 100 → R²=0 < 0.4 触发弱趋势折价 -5，
    实际 80 + 12 − 5 = 87）"""
    import math
    n = 40
    kl = []
    for i in range(n):
        h = 100.0 + 3.0 * math.cos(2 * math.pi * i / 8)
        kl.append([i, 100.0, h, h - 1.0, 100.0, 10.0, 10.0])
    kl.append([n, 100.0, 103.0, 96.0, 100.0, 10.0, 10.0])  # 未收盘
    # 信号根取全窗包络 → 所有峰谷必落带内（含 ≥4 个点，触发封顶）
    kl[-2] = [n - 1, 100.0,
              max(k[2] for k in kl[:-1]), min(k[3] for k in kl[:-1]), 100.0, 10.0, 10.0]
    from app.services.strategy.technical import golden_zone_swing_count
    assert golden_zone_swing_count(kl) >= 4
    assert compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                 klines=kl, pattern="hammer") == 87


# ── 趋势质量折价（CARD-10 升格，2026-10-27 开庭）────────────────────────────


def _r2_klines(closes: list[float]) -> list[list]:
    """24 根已收盘 + 1 根未收盘；收盘价序列由入参给定（量恒定 → 量比 1.0）"""
    kl = [[i, c, c, c, c, 10.0, 10.0] for i, c in enumerate(closes)]
    kl.append([len(closes), closes[-1], closes[-1], closes[-1], closes[-1],
               10.0, 10.0])
    return kl


def test_r2_low_discount_applied_and_partial():
    """R²<0.4（锯齿震荡）→ 质量分 -5，且分项记 r2_low=-5；基线 80 → 75"""
    closes = [100.0, 110.0] * 12  # 24 根交替 → 回归斜率≈0、R²≈0
    parts: dict = {}
    score = compute_quality_score(1, _votes(), "trigger_candlestick",
                                  klines=_r2_klines(closes), parts_out=parts)
    assert score == 75
    assert parts["r2_low"] == -5
    assert parts["total"] == 75


def test_r2_high_no_bonus():
    """强趋势 R²≥0.7 不加分（r2_mid 读数最高，非单调；只做单向折价）→ 80"""
    closes = [100.0 + 0.5 * i for i in range(24)]  # 完美直线 → R²=1
    parts: dict = {}
    score = compute_quality_score(1, _votes(), "trigger_candlestick",
                                  klines=_r2_klines(closes), parts_out=parts)
    assert score == 80
    assert "r2_low" not in parts


def test_r2_discount_requires_enough_klines():
    """不足 25 根（23 已收盘）→ 不计算、不折价；防止短样本误罚"""
    closes = [100.0, 110.0] * 12
    kl = _r2_klines(closes)[:-2]  # 23 根已收盘
    parts: dict = {}
    assert compute_quality_score(1, _votes(), "trigger_candlestick",
                                 klines=kl, parts_out=parts) == 80
    assert "r2_low" not in parts


def test_r2_low_discount_clamped():
    """折价后总分仍在 0~100：无票基础分 50 + 震荡折价 → 45"""
    closes = [100.0, 110.0] * 12
    score = compute_quality_score(1, _votes(0), "trigger_candlestick",
                                  klines=_r2_klines(closes))
    assert score == 45
    assert 0 <= score <= 100


# ── 回踩箱体折价（2026-10-28）──────────────────────────────────────────────


def _box_klines(n: int = 20, base: float = 100.0,
                amp: float = 0.2, rng: float = 0.5) -> list[list]:
    """n 根已收盘 + 1 未收盘：实体在同一区间交替、箱宽 = 2×rng = ATR14。

    箱宽/ATR=1、实体重叠率=1；总根数 <25，不触发 R² 弱趋势折价，
    便于单独断言箱体折价。
    """
    kl = []
    for i in range(n):
        o = base + (amp if i % 2 == 0 else -amp)
        c = base - (amp if i % 2 == 0 else -amp)
        kl.append([i, o, base + rng, base - rng, c, 100.0, 100.0])
    kl.append([n, base, base + rng, base - rng, base, 100.0, 100.0])
    return kl


def _trend_klines(n: int = 20, start: float = 100.0,
                  step: float = 1.0) -> list[list]:
    """n 根已收盘 + 1 未收盘：稳步上行、相邻实体不重叠、箱宽远大于 3×ATR"""
    kl = []
    for i in range(n):
        o = start + i * step
        kl.append([i, o, o + step, o - 0.1, o + step * 0.8, 100.0, 100.0])
    nxt = start + n * step
    kl.append([n, nxt, nxt + step, nxt - 0.1, nxt + step * 0.8, 100.0, 100.0])
    return kl


def _wide_overlap_klines(n: int = 20, start: float = 100.0,
                         step: float = 0.4) -> list[list]:
    """实体高度重叠但整体漂移、箱宽 > 3×ATR——验证宽度门单独生效"""
    kl = []
    for i in range(n):
        b = start + i * step
        kl.append([i, b - 0.5, b + 0.6, b - 0.6, b + 0.5, 100.0, 100.0])
    b = start + n * step
    kl.append([n, b - 0.5, b + 0.6, b - 0.6, b + 0.5, 100.0, 100.0])
    return kl


def test_pullback_box_evidence_geometry():
    """窄箱 + 高重叠 → 命中；趋势（不重叠）/ 宽箱（宽度不达）→ None"""
    from app.services.strategy.technical import pullback_box_evidence
    ev = pullback_box_evidence(_box_klines())
    assert ev is not None
    assert ev["atr_mult"] == 1.0
    assert ev["overlap"] == 1.0
    assert pullback_box_evidence(_trend_klines()) is None
    assert pullback_box_evidence(_wide_overlap_klines()) is None


def test_pullback_box_discount_applied():
    """回踩确认命中箱体 → -15，分项记 box/box_atr/box_overlap：80 - 15 = 65"""
    parts: dict = {}
    score = compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                  klines=_box_klines(), parts_out=parts)
    assert score == 65
    assert parts["box"] == -15
    assert parts["box_atr"] == 1.0
    assert parts["box_overlap"] == 1.0
    assert parts["total"] == 65


def test_pullback_box_only_for_pullback_confirm():
    """突破类不消费箱体折价（已由 range_score 硬拒）：同序列仍 80"""
    parts: dict = {}
    score = compute_quality_score(1, _votes(), "trigger_breakout",
                                  klines=_box_klines(), parts_out=parts)
    assert score == 80
    assert "box" not in parts


def test_pullback_box_requires_enough_bars():
    """不足 20 根已收盘 → 不判箱体（避免短样本误罚）"""
    parts: dict = {}
    score = compute_quality_score(1, _votes(), "trigger_pullback_confirm",
                                  klines=_box_klines(10), parts_out=parts)
    assert score == 80
    assert "box" not in parts


def test_pullback_box_rejects_inverted_ohlc():
    """坏 OHLC（high<low）宽度为负、天然满足「<上限」——必须拒绝，不得判箱体"""
    from app.services.strategy.technical import pullback_box_evidence
    bad = [[i, 100.0, 99.0, 101.0, 100.2, 100.0, 100.0] for i in range(20)]
    bad.append([20, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    assert pullback_box_evidence(bad) is None

