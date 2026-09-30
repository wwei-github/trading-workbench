"""K 线形态识别模块

支持形态：
- 锤形线（Hammer）：长下影 + 小实体 + 短上影，出现在下跌末端
- 倒锤形线（Inverted Hammer）：长上影 + 小实体 + 短下影，出现在下跌末端
- 流星线（Shooting Star）：倒锤形（长上影冲高回落）的看跌判定，反抽压力位语境
- 看涨吞没（Bullish Engulfing）：阳线实体完全包住前根阴线实体
- 看跌吞没（Bearish Engulfing）：阴线实体完全包住前根阳线实体
- 启明星（Morning Star）：三根形态，阴线 + 小实体 + 阳线突破
- 刺透线（Piercing Line）：阴线后阳线开盘低于前低但收盘深入前根实体

每根 K 线格式：[open_time, open, high, low, close, volume, ...]
"""
from __future__ import annotations

import numpy as np
from typing import Optional

from app.services.strategy.talib_verify import boost_with_talib


def _body(open_: float, close: float) -> float:
    """实体大小"""
    return abs(close - open_)


def _upper_shadow(open_: float, high: float, close: float) -> float:
    """上影线"""
    return high - max(open_, close)


def _lower_shadow(open_: float, low: float, close: float) -> float:
    """下影线"""
    return min(open_, close) - low


def _is_bullish(open_: float, close: float) -> bool:
    return close > open_


def _is_bearish(open_: float, close: float) -> bool:
    return close < open_


def detect_hammer(klines: list[list], idx: int = -1) -> Optional[dict]:
    """锤形线

    条件：
    1. 下影线 >= 2 * 实体
    2. 上影线必须很短（<= 总振幅的 15%，可以有）
    3. 开收不同（body≈0 的十字由蜻蜓十字负责，避免重复命中）
    """
    k = klines[idx]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body = _body(o, c)
    lower = _lower_shadow(o, l, c)
    upper = _upper_shadow(o, h, c)
    total = h - l

    if total <= 0:
        return None

    if body < 1e-12:
        return None

    if lower >= body * 2 and upper <= total * 0.15:
        strength = 0.5
        if lower >= body * 3:
            strength = 0.8
        if _is_bullish(o, c):
            strength += 0.1
        return {"pattern": "hammer", "direction": "bullish", "strength": min(strength, 1.0)}

    return None


def detect_inverted_hammer(klines: list[list], idx: int = -1) -> Optional[dict]:
    """倒锤形线

    条件：
    1. 上影线 >= 2 * 实体
    2. 下影线 <= 实体 * 0.3
    3. 出现在下跌末端，预示可能反转
    """
    k = klines[idx]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body = _body(o, c)
    lower = _lower_shadow(o, l, c)
    upper = _upper_shadow(o, h, c)
    total = h - l

    if total <= 0 or body < 1e-12:
        return None

    if upper >= body * 2 and lower <= total * 0.15:
        strength = 0.5
        if upper >= body * 3:
            strength = 0.7
        return {"pattern": "inverted_hammer", "direction": "bullish", "strength": strength}

    return None


# 吞没实体下限：两根实体都须达到"近期典型实体"水平——实体太短（窄幅盘整里的
# 小实体互包）不构成有意义的吞没。基线取形态两根之前的 ENGULF_BODY_LOOKBACK 根
# 平均实体（排除形态自身，防大实体抬高自身门槛），样本不足 ENGULF_BODY_MIN_BARS
# 根视为历史不足、不判定
ENGULF_BODY_LOOKBACK = 20
ENGULF_BODY_MIN_RATIO = 1.0   # 两根实体均须 ≥ 基线均值 × 该系数
ENGULF_BODY_MIN_BARS = 10

# 决断K线影线质量门（docs/25 §4，2026-09-30 用户拍板，替代提示词降权路线）：
# 形态中承担「决断」角色的那根K线——总影线占全长比例过高 = 长影拉锯、犹豫非决断，
# 直接不判金K（不满足要求的不判）。只约束决断K线自身，形态其余K线（被吞没方/
# 趋势语境K/星）不参与该门；实体占比 ≥60% 即放行（门只拦 >40%）
# - 吞没：第二根（吞没K线）
# - 启明星/黄昏星：第三根（确认阳/阴线）
DECISION_SHADOW_MAX_PCT = 0.40   # 决断K线总影线 / K线全长 上限


def detect_engulfing(klines: list[list], idx: int = -1) -> Optional[dict]:
    """吞没形态（看涨/看跌）

    看涨吞没：当前阳线实体完全包住前一根阴线实体
    看跌吞没：当前阴线实体完全包住前一根阳线实体
    质量门（不满足即不判吞没）：
    1. 两根 K 线实体都必须比较长（≥ 近 20 根平均实体），
       实体太短的小实体互包不算吞没；
    2. 吞没K线（第二根）总影线 ≤ 全长 40%（长影吞没=犹豫非决断）。
    """
    # idx 与其他检测器同语义（负数=倒数，正数=绝对位置），统一换算成绝对位置
    pos = idx if idx >= 0 else len(klines) + idx
    if pos < 1 + ENGULF_BODY_MIN_BARS:
        return None

    k_cur = klines[idx]
    k_prev = klines[idx - 1]
    o1, c1 = float(k_prev[1]), float(k_prev[4])
    o2, c2 = float(k_cur[1]), float(k_cur[4])

    body1 = _body(o1, c1)
    body2 = _body(o2, c2)

    if body1 < 1e-12 or body2 < 1e-12:
        return None

    base = [
        _body(float(k[1]), float(k[4]))
        for k in klines[max(0, pos - 1 - ENGULF_BODY_LOOKBACK): pos - 1]
    ]
    if len(base) < ENGULF_BODY_MIN_BARS:
        return None
    avg_body = sum(base) / len(base)
    if avg_body <= 0 or body1 < avg_body * ENGULF_BODY_MIN_RATIO or body2 < avg_body * ENGULF_BODY_MIN_RATIO:
        return None

    # 影线质量门：吞没K线实体须占 K 线主体（总影线 ≤ 全长 40%）
    rng2 = float(k_cur[2]) - float(k_cur[3])
    if rng2 <= 0 or (rng2 - body2) / rng2 > DECISION_SHADOW_MAX_PCT:
        return None

    # 看涨吞没：前阴后阳，阳实体包住阴实体
    if _is_bearish(o1, c1) and _is_bullish(o2, c2):
        if o2 <= c1 and c2 >= o1:
            strength = 0.7
            if body2 > body1 * 2:
                strength = 0.9
            return {"pattern": "bullish_engulfing", "direction": "bullish", "strength": strength}

    # 看跌吞没：前阳后阴，阴实体包住阳实体
    if _is_bullish(o1, c1) and _is_bearish(o2, c2):
        if o2 >= c1 and c2 <= o1:
            strength = 0.7
            if body2 > body1 * 2:
                strength = 0.9
            return {"pattern": "bearish_engulfing", "direction": "bearish", "strength": strength}

    return None


def detect_morning_star(klines: list[list], idx: int = -1) -> Optional[dict]:
    """启明星（三根 K 线）

    条件：
    1. 第一根：阴线（下跌趋势中）
    2. 第二根：小实体（星），实体较小，可以是阴阳
    3. 第三根：阳线，收盘深入第一根实体
    4. 第三根影线不过长（总影线 ≤ 全长 40%，决断K线影线质量门）
    """
    if len(klines) < abs(idx) + 2:
        return None

    k1 = klines[idx - 2]
    k2 = klines[idx - 1]
    k3 = klines[idx]
    o1, c1 = float(k1[1]), float(k1[4])
    o2, c2 = float(k2[1]), float(k2[4])
    o3, c3 = float(k3[1]), float(k3[4])

    body1 = _body(o1, c1)
    body2 = _body(o2, c2)
    body3 = _body(o3, c3)

    if body1 < 1e-12 or body3 < 1e-12:
        return None

    # 第一根阴线
    if not _is_bearish(o1, c1):
        return None

    # 第二根小实体（星）
    avg_body = (body1 + body3) / 2
    if body2 >= avg_body * 0.5:
        return None

    # 第三根阳线，收盘深入第一根实体中部以上
    if not _is_bullish(o3, c3):
        return None

    # 影线质量门（docs/25 §4，2026-09-30）：第三根是决断K线，总影线 > 全长 40%
    # （冲高遭上方抛压打回/探底回拉）= 犹豫非决断，不判金K
    h3, l3 = float(k3[2]), float(k3[3])
    rng3 = h3 - l3
    if rng3 <= 0 or (rng3 - body3) / rng3 > DECISION_SHADOW_MAX_PCT:
        return None

    midpoint1 = (o1 + c1) / 2
    if c3 <= midpoint1:
        return None

    strength = 0.7
    if c3 >= o1:  # 收复第一根全部
        strength = 0.9

    return {"pattern": "morning_star", "direction": "bullish", "strength": strength}


def detect_piercing_line(klines: list[list], idx: int = -1) -> Optional[dict]:
    """刺透线（两根 K 线）

    条件：
    1. 第一根：阴线
    2. 第二根：阳线，开盘不高于前根收盘（加密货币 7x24 连续交易无跳空，
       经典定义的"低于前低"在此几乎不可能出现，放宽为平开或低开），
       收盘深入前根实体上半部
    """
    if len(klines) < abs(idx) + 1:
        return None

    k1 = klines[idx - 1]
    k2 = klines[idx]
    o1, h1, l1, c1 = float(k1[1]), float(k1[2]), float(k1[3]), float(k1[4])
    o2, h2, l2, c2 = float(k2[1]), float(k2[2]), float(k2[3]), float(k2[4])

    body1 = _body(o1, c1)

    if body1 < 1e-12:
        return None

    # 第一根阴线
    if not _is_bearish(o1, c1):
        return None

    # 第二根阳线
    if not _is_bullish(o2, c2):
        return None

    # 开盘不高于前根收盘（连续交易下平开即满足；高于前收盘则不成立）
    if o2 > c1:
        return None

    # 收盘深入前根实体上半部（超过中点）
    midpoint1 = (o1 + c1) / 2
    if c2 <= midpoint1:
        return None

    # 但不能完全吞没（否则是吞没）
    if c2 >= o1:
        return None

    strength = 0.6
    if c2 >= o1 * 0.7 + c1 * 0.3:  # 接近前根开盘价
        strength = 0.8

    return {"pattern": "piercing_line", "direction": "bullish", "strength": strength}


def detect_evening_star(klines: list[list], idx: int = -1) -> Optional[dict]:
    """黄昏星（三根 K 线，启明星的顶部镜像）

    条件：
    1. 第一根：阳线
    2. 第二根：小实体（星）
    3. 第三根：阴线，收盘深入第一根实体下半部
    4. 第三根影线不过长（总影线 ≤ 全长 40%，决断K线影线质量门）
    """
    if len(klines) < abs(idx) + 2:
        return None

    k1 = klines[idx - 2]
    k2 = klines[idx - 1]
    k3 = klines[idx]
    o1, c1 = float(k1[1]), float(k1[4])
    o2, c2 = float(k2[1]), float(k2[4])
    o3, c3 = float(k3[1]), float(k3[4])

    body1 = _body(o1, c1)
    body2 = _body(o2, c2)
    body3 = _body(o3, c3)

    if body1 < 1e-12 or body3 < 1e-12:
        return None

    if not _is_bullish(o1, c1):
        return None

    avg_body = (body1 + body3) / 2
    if body2 >= avg_body * 0.5:
        return None

    if not _is_bearish(o3, c3):
        return None

    # 影线质量门：第三根是决断K线（启明星第三根的顶部镜像，口径同 40%）
    h3, l3 = float(k3[2]), float(k3[3])
    rng3 = h3 - l3
    if rng3 <= 0 or (rng3 - body3) / rng3 > DECISION_SHADOW_MAX_PCT:
        return None

    midpoint1 = (o1 + c1) / 2
    if c3 >= midpoint1:
        return None

    strength = 0.7
    if c3 <= o1:
        strength = 0.9

    return {"pattern": "evening_star", "direction": "bearish", "strength": strength}


def detect_dark_cloud_cover(klines: list[list], idx: int = -1) -> Optional[dict]:
    """乌云盖顶（两根 K 线，刺透线的顶部镜像）

    条件：
    1. 第一根：阳线
    2. 第二根：阴线，开盘不低于前根收盘（连续交易放宽，同刺透线），
       收盘深入前根实体下半部
    """
    if len(klines) < abs(idx) + 1:
        return None

    k1 = klines[idx - 1]
    k2 = klines[idx]
    o1, h1, c1 = float(k1[1]), float(k1[2]), float(k1[4])
    o2, c2 = float(k2[1]), float(k2[4])

    body1 = _body(o1, c1)
    if body1 < 1e-12:
        return None

    if not _is_bullish(o1, c1):
        return None

    if not _is_bearish(o2, c2):
        return None

    # 开盘不低于前根收盘（连续交易下平开即满足；低于前收盘则不成立）
    if o2 < c1:
        return None

    # 收盘深入前根实体下半部（低于中点）
    midpoint1 = (o1 + c1) / 2
    if c2 >= midpoint1:
        return None

    # 但不能完全吞没（否则是看跌吞没）
    if c2 <= o1:
        return None

    strength = 0.6
    if c2 <= o1 * 0.7 + c1 * 0.3:
        strength = 0.8

    return {"pattern": "dark_cloud_cover", "direction": "bearish", "strength": strength}


def _detect_doji(klines: list[list], idx: int) -> Optional[dict]:
    """十字形态公共判定：蜻蜓（长下影） bullish / 墓碑（长上影） bearish"""
    k = klines[idx]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body = _body(o, c)
    lower = _lower_shadow(o, l, c)
    upper = _upper_shadow(o, h, c)
    total = h - l

    if total <= 0:
        return None

    # 十字：开收接近，实体占比极小
    if body > total * 0.1:
        return None

    # 蜻蜓十字：下影长、上影几乎无（探底回升）
    if lower >= total * 0.6 and upper <= total * 0.15:
        return {"pattern": "dragonfly_doji", "direction": "bullish", "strength": 0.6}

    # 墓碑十字：上影长、下影几乎无（冲高回落）
    if upper >= total * 0.6 and lower <= total * 0.15:
        return {"pattern": "gravestone_doji", "direction": "bearish", "strength": 0.6}

    return None


def detect_doji(klines: list[list], idx: int = -1) -> Optional[dict]:
    """蜻蜓/墓碑十字"""
    return _detect_doji(klines, idx)


def detect_hanging_man(klines: list[list], idx: int = -1) -> Optional[dict]:
    """上吊线：锤子形状但出现在上涨末端顶部（预示反转下跌）

    在锤形线形状基础上，要求当前收盘处于最近 10 根 K 线区间上部（前 30%），
    用于与锤形线（底部）区分。
    """
    shape = detect_hammer(klines, idx)
    if not shape:
        return None

    n = len(klines)
    window = klines[-min(11, n):-1] if idx == -1 else klines[max(0, idx - 10):idx]
    if len(window) < 5:
        return None
    hi = max(float(k[2]) for k in window)
    lo = min(float(k[3]) for k in window)
    if hi <= lo:
        return None

    c = float(klines[idx][4])
    pos = (c - lo) / (hi - lo)
    if pos >= 0.7:
        return {"pattern": "hanging_man", "direction": "bearish", "strength": shape["strength"]}

    return None


def detect_shooting_star(klines: list[list], idx: int = -1) -> Optional[dict]:
    """流星线：倒锤形（长上影冲高回落）的看空语境判定

    形状与倒锤形线完全相同（长上影 + 小实体 + 短下影），区别仅在语境：
    反抽压力位/EMA21 受阻回落时为看跌（2026-09-28 起替代上吊线进入看空
    金K清单——反抽受阻的语义锚是"冲高回落长上影"）。顶部不限于近期区间
    高位（顶部也可以是压力位），锚定语境由触发层保证（指标多数决定方向 +
    信号K线触碰 EMA21 收回防守侧 + 悬空金K拦截），检测器仅做形状
    判定——与锤形线无位置门控对称；同形双报（倒锤形线/流星线并列）与
    锤形线/上吊线对的设计一致。
    """
    shape = detect_inverted_hammer(klines, idx)
    if not shape:
        return None
    return {"pattern": "shooting_star", "direction": "bearish", "strength": shape["strength"]}


def detect_harami_breakout(klines: list[list], idx: int = -1) -> Optional[dict]:
    """孕线突破（三根 K 线）

    条件（以看涨为例）：
    1. 母线：大实体 K 线
    2. 内包线：实体完全在母线实体内（孕线，多空拉锯）
    3. 突破线：收盘突破母线实体高点（看涨）/低点（看跌）
    """
    if len(klines) < abs(idx) + 2:
        return None

    k1 = klines[idx - 2]  # 母线
    k2 = klines[idx - 1]  # 内包线
    k3 = klines[idx]      # 突破线
    o1, c1 = float(k1[1]), float(k1[4])
    o2, c2 = float(k2[1]), float(k2[4])
    o3, c3 = float(k3[1]), float(k3[4])

    body1 = _body(o1, c1)
    body2 = _body(o2, c2)
    body3 = _body(o3, c3)

    if body1 < 1e-12 or body3 < 1e-12:
        return None

    hi1, lo1 = max(o1, c1), min(o1, c1)

    # 母线要有足够实体（占该根 K 线振幅 30% 以上）
    total1 = float(k1[2]) - float(k1[3])
    if total1 <= 0 or body1 < total1 * 0.3:
        return None

    # 内包线：实体完全在母线实体内
    if not (max(o2, c2) < hi1 and min(o2, c2) > lo1):
        return None
    if body2 >= body1:
        return None

    mother_high = hi1
    mother_low = lo1

    # 看涨孕线突破：突破线收盘上破母线实体高点
    if c3 > mother_high and body3 >= body2:
        strength = 0.6
        if body3 >= body1:
            strength = 0.8
        return {"pattern": "bullish_harami", "direction": "bullish", "strength": strength}

    # 看跌孕线突破：突破线收盘下破母线实体低点
    if c3 < mother_low and body3 >= body2:
        strength = 0.6
        if body3 >= body1:
            strength = 0.8
        return {"pattern": "bearish_harami", "direction": "bearish", "strength": strength}

    return None


# ===== 12 金K 清单（回踩金K按方向筛选，docs/03-交易系统.md §6.2 基础上有偏离）=====
# 2026-09-12 精简：去掉刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑（信号质量弱或与保留形态高度重叠），
# 保留 6 种实体结构更明确的形态。检测函数保留（历史扫描结果展示用），仅不再参与信号判定
# 2026-09-28 看空侧调整：上吊线 → 流星线——回踩金K的语义是"回撤/反抽到结构位后的
# 反转确认"：看多回踩支撑配锤形线（长下影），看空反抽阻力配倒锤形线（长上影，即流星线），
# 上吊线（长下影出现在顶部）与反抽受阻语义不符。看多三种维持不变。
# 流星线仅形状判定（无区间高位门）：顶部=压力位/EMA21 反抽受阻的锚定语境
# 由触发层方向门控 + 触碰/悬空拦截保证，不要求近期区间高位
GOLDEN_12 = {
    # 看涨 3 种
    "hammer",              # 锤形线
    "bullish_engulfing",   # 看涨吞没
    "morning_star",        # 启明星
    # 看跌 3 种
    "shooting_star",       # 流星线（倒锤形出现在顶部）
    "bearish_engulfing",   # 看跌吞没
    "evening_star",        # 黄昏星
}

PATTERN_LABEL_MAP = {
    "hammer": "锤形线",
    "inverted_hammer": "倒锤形线",
    "bullish_engulfing": "看涨吞没",
    "bearish_engulfing": "看跌吞没",
    "morning_star": "启明星",
    "evening_star": "黄昏星",
    "shooting_star": "倒锤形线（流星）",
    "piercing_line": "刺透线",
    "dark_cloud_cover": "乌云盖顶",
    "bullish_harami": "看涨孕线突破",
    "bearish_harami": "看跌孕线突破",
    "dragonfly_doji": "蜻蜓十字",
    "gravestone_doji": "墓碑十字",
    "hanging_man": "上吊线",
    "close_above_prev_high": "收盘破前高",
}


# ===== 单影线形态的复合确认（2026-09-29 用户拍板）=====
# 锤形线（看多）/流星线即倒锤形（看空）是"单根单影线"形态，单独作为回踩
# 金K确认证据太弱：单根不再触发，须满足其一——
#   ① 双锤：连续两根同形态（前根+当根，探底回升连续两次确认）
#   ② 锤+趋势K：前根锤/流星 + 当根顺向趋势K（短影线长实体，实体 ≥70% 全长，
#      与突破K线同口径，2026-09-30-2 同步放宽）
# 复合形态（吞没/启明星/黄昏星）本身由多根K线构成、结构更强，维持单根确认。
SINGLE_SHADOW_PATTERNS = {"hammer", "shooting_star"}

# 顺向趋势K线实体/K线全长下限（与 trigger.BREAKOUT_BODY_RANGE_MIN 同口径，
# 2026-09-30-2 同步放宽 0.75→0.70；复制常量避免 strategy 内部反向 import trigger）
TREND_CANDLE_BODY_RANGE_MIN = 0.70


def detect_solid_trend_candle(klines: list[list], direction: int,
                              idx: int = -1) -> Optional[dict]:
    """顺向趋势K线：长实体（≥70% 全长）+ 顺向（做多=阳线、做空=阴线）。

    供"锤+趋势K"复合确认使用（docs 口径同突破K线长实体：影线合计 ≤30%
    即天然短影线，不做单侧影线额外限制）。
    """
    k = klines[idx]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body = abs(c - o)
    rng = h - l
    if rng <= 0 or body <= 0:
        return None
    if direction == 1 and c <= o:
        return None
    if direction == -1 and c >= o:
        return None
    if body / rng < TREND_CANDLE_BODY_RANGE_MIN:
        return None
    return {
        "pattern": "trend_candle",
        "direction": "bullish" if direction == 1 else "bearish",
        "strength": 0.6,
    }


def resolve_golden_evidence(klines: list[list], wanted: str,
                            patterns: list[dict]) -> tuple[Optional[dict], str]:
    """回踩金K证据统一裁决（2026-09-29 复合确认规则）。

    入参：
    - klines：全量K线（[-2]=当根已收盘、[-3]=前根已收盘）
    - wanted：期望方向 "bullish"/"bearish"（指标多数决方向）
    - patterns：detect_all_patterns(klines, idx=-2) 的当根形态检测结果
    返回 (golden, note)：
    - golden：确认证据形态 dict（pattern/direction），None=无有效证据
    - note：组合说明（""=单形态直接确认），trigger 层拼进触发 reason

    裁决顺序：
    1. 当根复合形态（吞没/启明星/黄昏星）方向匹配 → 直接确认
    2. 当根锤/流星方向匹配 → 前根同形态（双锤）才确认，否则不确认
    3. 当根无金K → 前根锤/流星（方向匹配）+ 当根顺向趋势K → 组合确认，
       pattern 落前根形态（前端展示/AI 提示词兼容）
    触点框架（当根触碰或 pending 次根确认）由 trigger 层负责，本函数只裁
    "形态证据是否成立"——无触碰语境的悬空组合不会绕过触点要求。
    """
    golden = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    golden = golden if (golden and golden["direction"] == wanted) else None

    # 规则1：复合形态直接确认
    if golden and golden["pattern"] not in SINGLE_SHADOW_PATTERNS:
        return golden, ""

    direction = 1 if wanted == "bullish" else -1

    # 规则2：当根单影线（锤/流星）→ 双锤组合确认
    if golden:
        try:
            prev = (detect_hammer(klines, -3) if golden["pattern"] == "hammer"
                    else detect_shooting_star(klines, -3))
        except Exception:
            prev = None
        if prev:
            return golden, "双锤组合"
        return None, ""  # 单锤/单流星不确认

    # 规则3：当根趋势K + 前根锤/流星 → 组合确认
    try:
        solid = detect_solid_trend_candle(klines, direction, -2)
    except Exception:
        solid = None
    if solid is None:
        return None, ""
    try:
        prev = (detect_hammer(klines, -3) if wanted == "bullish"
                else detect_shooting_star(klines, -3))
    except Exception:
        prev = None
    if prev and prev["direction"] == wanted:
        label = PATTERN_LABEL_MAP.get(prev["pattern"], prev["pattern"])
        return ({"pattern": prev["pattern"], "direction": wanted,
                 "strength": prev["strength"]},
                f"前根{label}+当根趋势K")
    return None, ""


def detect_all_patterns(klines: list[list], idx: int = -2) -> list[dict]:
    """检测指定位置的所有形态

    默认 idx=-2：最后一根已收盘 K 线（-1 是未收盘的）
    返回命中的形态列表，按 strength 降序
    """
    results = []
    for detector in [
        detect_hammer,
        detect_inverted_hammer,
        detect_engulfing,
        detect_morning_star,
        detect_evening_star,
        detect_piercing_line,
        detect_dark_cloud_cover,
        detect_doji,
        detect_hanging_man,
        detect_shooting_star,
        detect_harami_breakout,
    ]:
        try:
            r = detector(klines, idx)
            if r:
                results.append(r)
        except Exception:
            continue
    results.sort(key=lambda x: x["strength"], reverse=True)

    # TA-Lib 双源校验：方向一致时提升 strength（未安装则原样返回）
    results = boost_with_talib(klines, results, idx)

    return results


def detect_patterns_recent(klines: list[list], lookback: int = 3) -> list[dict]:
    """检测最近 lookback 根已收盘 K 线中的所有形态

    排除最后一根未收盘 K 线
    返回所有命中的形态（含位置信息）
    """
    n = len(klines)
    # 从倒数第 2 根（最后已收盘）往前找
    end = n - 1  # 排除 klines[-1]（未收盘）
    start = max(1, end - lookback)
    results = []
    for i in range(start, end):
        patterns = detect_all_patterns(klines, idx=i)
        for p in patterns:
            p["kline_idx"] = i
            results.append(p)
    results.sort(key=lambda x: (x["strength"], x["kline_idx"]), reverse=True)
    return results


def has_bullish_pattern(klines: list[list], lookback: int = 3) -> Optional[dict]:
    """最近 lookback 根内是否出现看涨形态

    返回最强的看涨形态，或 None
    """
    patterns = detect_patterns_recent(klines, lookback)
    for p in patterns:
        if p["direction"] == "bullish":
            return p
    return None


def close_above_prev_high(klines: list[list]) -> Optional[dict]:
    """最新已收盘 K 线的收盘价高于前一个高点

    用 klines[-2]（最后已收盘）的收盘价，
    不用 klines[-1]（当前未收盘）
    """
    if len(klines) < 3:
        return None

    n = len(klines)
    # 用倒数第 2 根（最后已收盘）
    close_last = float(klines[-2][4])

    # 向前找最近的高点（排除最后两根）
    for i in range(n - 3, max(n - 30, 1), -1):
        k = klines[i]
        h = float(k[2])
        if close_last > h:
            return {
                "pattern": "close_above_prev_high",
                "direction": "bullish",
                "strength": 0.6,
                "ref_high": h,
                "ref_idx": i,
                "breakout_pct": (close_last - h) / h * 100,
            }

    return None
