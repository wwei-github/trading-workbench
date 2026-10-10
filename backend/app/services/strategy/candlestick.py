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


# 两根/三根K线形态跨K线边界比较的微跳空容差（2026-10-08）：
# 加密 7×24 连续交易无真正跳空，但薄盘口品种前后根边界的第一笔成交
#（=各自 open/close 打印价）天然差几个 bp——零容差比较会把 0.04% 的
# 打印噪声当成「高开/低开」误拒（线上实例：AKTUSDT 2026-10-08 12:00→13:00
# 收盘过中点的标准刺透因开盘 +0.04% 被拒）。0.1% 为噪声带与真实高开/低开
# 的经验分界。适用四处「教科书必然成立却被钉死边界拒判」的比较：
# 刺透线（o2 vs c1 向上）与乌云盖顶（o2 vs c1 向下）的开盘门、
# 吞没（o2 vs c1 同向门，另一侧 c2 vs o1 属真语义临界不动）、
# 孕线内包线（o2/c2 vs 母线实体沿——内包线开盘必然钉在母线实体边缘，
# 严格不等式下约半数被噪声方向随机杀掉，危害比刺透翻倍）
MICRO_GAP_TOLERANCE = 0.001


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
    2. 下影线 <= 总振幅 * 0.15
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


# 吞没实体下限（2026-10-10 口径修订）：仅吞没K线（第二根）须达到"近期典型实体"
# 水平——决断K线必须有实体分量，窄幅盘整里的小实体互包（两根都小）不算吞没；
# 被吞没方（第一根）不再设实体基线——大实体吞小十字/小阴恰是教科书最强吞没
# 变体（前根犹豫、当根决断），线上 WUSDT 2026-10-10 20:00 大阳吞小阴因 body1
# 仅均实体 24% 被整门误拒（回踩 EMA21 企稳信号漏扫）。基线取形态两根之前的
# ENGULF_BODY_LOOKBACK 根实体（排除形态自身，防大实体抬高自身门槛），
# 样本不足 ENGULF_BODY_MIN_BARS 根视为历史不足、不判定。
# 基线统计量 2026-10-10-2 均值→中位数（用户拍板，预防性稳健化）：均值对离群
# 大实体与 regime 混窗敏感（KAIAUSDT 10-10 13:00 双 regime 混窗实测均值≈中位）。
# 比例系数 2026-10-10-3 1.0→0.6（用户拍板）：body2 须达中位实体的六成——
# 微实体互包（构造案 body2=基线 9%）仍拦，压缩段后实体占比尚可的决断K线
# 放行（KAIA 案 body2/中位=87.6% 由拒转过）；回看窗 20→10 等口径定标
# 等 blocked_by 死因分布数据
ENGULF_BODY_LOOKBACK = 20
ENGULF_BODY_MIN_RATIO = 0.6   # 吞没K线（body2）实体须 ≥ 基线中位数 × 该系数
ENGULF_BODY_MIN_BARS = 10

# 决断K线影线质量门（docs/25 §4，2026-09-30 用户拍板，替代提示词降权路线）：
# 形态中承担「决断」角色的那根K线——总影线占全长比例过高 = 长影拉锯、犹豫非决断，
# 直接不判金K（不满足要求的不判）。只约束决断K线自身，形态其余K线（被吞没方/
# 趋势语境K/星）不参与该门；实体占比 ≥60% 即放行（门只拦 >40%）
# - 吞没：第二根（吞没K线）
# - 启明星/黄昏星：第三根（确认阳/阴线）
DECISION_SHADOW_MAX_PCT = 0.40   # 决断K线总影线 / K线全长 上限

# 看涨吞没上影上限（2026-10-01 用户拍板，替代对称总影线门）：第二根上影/K线全长
# ≤15%（口径同锤形线上影"必须短"）——冲高遭上方抛压打回=犹豫非决断；下影不限
# （探底回升=下方承接强，长下影不扣分）。看跌吞没维持 DECISION_SHADOW_MAX_PCT。
ENGULF_UPPER_SHADOW_MAX_PCT = 0.15


def detect_engulfing(klines: list[list], idx: int = -1) -> Optional[dict]:
    """吞没形态（看涨/看跌）

    看涨吞没：当前阳线实体完全包住前一根阴线实体
    看跌吞没：当前阴线实体完全包住前一根阳线实体
    质量门（不满足即不判吞没）：
    1. 吞没K线（第二根）实体 ≥ 近 20 根中位实体 ×0.6——弱吞没下限：决断K线
       须有基本实体分量，微实体互包不算吞没；被吞没方不设基线（大实体吞
       小十字=最强变体，2026-10-10 WUSDT 个案修订）；统计量中位数（10-10-2
       均值→中位数稳健化）、比例 0.6（10-10-3 拍板，KAIA 87.6% 案由拒转过）；
    2. 看涨吞没第二根上影 ≤ 全长 15%（下影不限，2026-10-01 不对称化）；
       看跌吞没第二根总影线 ≤ 全长 40%（2026-09-30 口径维持）。
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
    # 基线统计量=中位数（10-10-2 稳健化）× 0.6 下限（10-10-3 拍板）：
    # body2 须达"近期典型实体"的六成，微实体互包仍拦、压缩段决断K线放行
    median_body = float(np.median(base))
    # 实体基线门（2026-10-10 修订）只约束吞没K线（body2）：决断K线须有实体分量；
    # 被吞没方（body1）不设基线——大实体吞小十字是最强变体，body1 门曾把
    # WUSDT 2026-10-10 20:00 大阳吞小阴整门误拒
    if median_body <= 0 or body2 < median_body * ENGULF_BODY_MIN_RATIO:
        return None

    # 影线质量门（2026-10-01 看涨侧不对称化）：先算几何，按方向套不同门
    rng2 = float(k_cur[2]) - float(k_cur[3])
    if rng2 <= 0:
        return None
    upper2 = _upper_shadow(o2, float(k_cur[2]), c2)

    # 看涨吞没：前阴后阳，阳实体包住阴实体
    if _is_bearish(o1, c1) and _is_bullish(o2, c2):
        # o2 ≤ c1×(1+0.1%)：连续交易下阳线开盘≈前阴收盘（前实体下沿），
        # 薄盘口打印噪声 ≤0.1% 视同平开（2026-10-08 微跳空容差，同刺透线）；
        # c2 ≥ o1（收盘收复前实体上沿）属真语义临界，不加容差
        if o2 <= c1 * (1 + MICRO_GAP_TOLERANCE) and c2 >= o1:
            # 上影必须短（≤ 全长 15%，口径同锤形线上影）：冲高遭抛压打回=
            # 犹豫非决断；下影不限（探底回升=下方承接强）
            if upper2 / rng2 > ENGULF_UPPER_SHADOW_MAX_PCT:
                return None
            strength = 0.7
            if body2 > body1 * 2:
                strength = 0.9
            return {"pattern": "bullish_engulfing", "direction": "bullish", "strength": strength}

    # 看跌吞没：前阳后阴，阴实体包住阳实体
    if _is_bullish(o1, c1) and _is_bearish(o2, c2):
        # o2 ≥ c1×(1−0.1%)：镜像同口径（阴线开盘≈前阳收盘=前实体上沿，
        # 打印噪声视同平开）；c2 ≤ o1 属真语义临界，不加容差
        if o2 >= c1 * (1 - MICRO_GAP_TOLERANCE) and c2 <= o1:
            # 决断K线影线质量门（2026-09-30 口径维持）：总影线 ≤ 全长 40%
            if (rng2 - body2) / rng2 > DECISION_SHADOW_MAX_PCT:
                return None
            strength = 0.7
            if body2 > body1 * 2:
                strength = 0.9
            return {"pattern": "bearish_engulfing", "direction": "bearish", "strength": strength}

    return None


# 启明星中K（星）实体上限（2026-10-01 用户拍板收紧 0.5→0.3）：中K实体超过
# 外侧两根均值×0.3 时，2-3 根组合已呈"看涨吞没"观感，不再算星
MORNING_STAR_MID_BODY_MAX_RATIO = 0.3


def detect_morning_star(klines: list[list], idx: int = -1) -> Optional[dict]:
    """启明星（三根 K 线）

    条件：
    1. 第一根：阴线（下跌趋势中）
    2. 第二根：小实体（星），实体 < 外侧均值×0.3（2026-10-01 收紧，
       防退化为看涨吞没观感），可以是阴阳
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

    # 第二根小实体（星）：2026-10-01 收紧 0.5→0.3——中K实体偏大时第三根
    # 常已包住中K，形态退化为看涨吞没观感
    avg_body = (body1 + body3) / 2
    if body2 >= avg_body * MORNING_STAR_MID_BODY_MAX_RATIO:
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
    2. 第二根：阳线，开盘不高于前根收盘的 1.1 倍（加密货币 7x24 连续交易无跳空，
       经典定义的"低于前低"在此几乎不可能出现，放宽为平开或低开；薄盘口边界
       成交打印噪声 ≤0.1% 视同平开，2026-10-08 起加微跳空容差），
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

    # 开盘不高于前根收盘×(1+0.1%)（连续交易下平开即满足；薄盘口边界打印
    # 噪声 ≤0.1% 视同平开，真实高开超过容差才不成立——2026-10-08 AKTUSDT
    # 标准刺透因 +0.04% 打印差被零容差误拒，加微跳空容差）
    if o2 > c1 * (1 + MICRO_GAP_TOLERANCE):
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
    2. 第二根：小实体（星），实体 < 外侧均值×0.3（2026-10-01 与启明星对齐收紧，
       防退化为看跌吞没观感），可以是阴阳
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

    # 第二根小实体（星）：2026-10-01 与启明星对齐 0.5→0.3——中K实体偏大时
    # 第三根常已包住中K，形态退化为看跌吞没观感
    avg_body = (body1 + body3) / 2
    if body2 >= avg_body * MORNING_STAR_MID_BODY_MAX_RATIO:
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
    2. 第二根：阴线，开盘不低于前根收盘的 0.99 倍（连续交易放宽，同刺透线；
       薄盘口边界打印噪声 ≤0.1% 视同平开，2026-10-08 加微跳空容差），
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

    # 开盘不低于前根收盘×(1−0.1%)（连续交易下平开即满足；薄盘口边界打印
    # 噪声 ≤0.1% 视同平开，真实低开超过容差才不成立——与刺透线镜像同口径）
    if o2 < c1 * (1 - MICRO_GAP_TOLERANCE):
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
    """十字形态判定：蜻蜓（长下影）bullish / 墓碑（长上影）bearish / 十字星 neutral

    2026-10-02 用户口径：上下影线都很长、实体很短或几乎没有——都是十字星。
    此前双长影K线两分支均不命中、完全不识别（也不会被误判进锤/流星/蜻蜓/墓碑
    ——各有对侧影线 ≤15% 门拦截），现显式识别为中性 doji。不进 GOLDEN_PATTERNS
    （双长影=多空拉锯、方向未决，不构成回踩企稳证据），
    resolve_golden_evidence 按方向过滤天然排除，仅用于形态识别与展示。
    """
    k = klines[idx]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body = _body(o, c)
    lower = _lower_shadow(o, l, c)
    upper = _upper_shadow(o, h, c)
    total = h - l

    if total <= 0:
        return None

    # 蜻蜓十字：开收接近（实体占比极小）、下影长、上影几乎无（探底回升）
    if body <= total * 0.1:
        if lower >= total * 0.6 and upper <= total * 0.15:
            return {"pattern": "dragonfly_doji", "direction": "bullish", "strength": 0.6}
        # 墓碑十字：上影长、下影几乎无（冲高回落）
        if upper >= total * 0.6 and lower <= total * 0.15:
            return {"pattern": "gravestone_doji", "direction": "bearish", "strength": 0.6}
        # 十字（实体几乎没有）且两侧影线都长（任一侧都不满足"几乎无"）
        if lower > total * 0.15 and upper > total * 0.15:
            return {"pattern": "doji", "direction": "neutral", "strength": 0.5}
        return None

    # 实体很短（≤25% 总长）+ 双侧影线都长：纺锤/高波浪一类拉锯K，按用户口径
    # 同归十字星（中性）。对侧影线 ≤15% 的归锤形线家族（detect_hammer /
    # detect_inverted_hammer 上报，本分支条件与之互斥、不抢报）
    if body <= total * 0.25 and lower > total * 0.15 and upper > total * 0.15:
        return {"pattern": "doji", "direction": "neutral", "strength": 0.5}

    return None


def detect_doji(klines: list[list], idx: int = -1) -> Optional[dict]:
    """蜻蜓/墓碑/长腿十字"""
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

    # 内包线：实体完全在母线实体内（±0.1% 微跳空容差）——内包线开盘必然
    # 钉在母线实体边缘（o2≈母线收盘=hi1 或 lo1），严格不等式下约半数被
    # 薄盘口打印噪声方向随机杀掉（2026-10-08，同刺透/吞没口径）
    if not (max(o2, c2) < hi1 * (1 + MICRO_GAP_TOLERANCE)
            and min(o2, c2) > lo1 * (1 - MICRO_GAP_TOLERANCE)):
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


# ===== 金K 清单（回踩金K按方向筛选，docs/03-交易系统.md §6.2 基础上有偏离）=====
# 2026-09-12 精简：去掉刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑（信号质量弱或与保留形态高度重叠），
# 保留 6 种实体结构更明确的形态。检测函数保留（历史扫描结果展示用），仅不再参与信号判定
# 2026-09-28 看空侧调整：上吊线 → 流星线——回踩金K的语义是"回撤/反抽到结构位后的
# 反转确认"：看多回踩支撑配锤形线（长下影），看空反抽阻力配倒锤形线（长上影，即流星线），
# 上吊线（长下影出现在顶部）与反抽受阻语义不符。看多三种维持不变。
# 流星线仅形状判定（无区间高位门）：顶部=压力位/EMA21 反抽受阻的锚定语境
# 由触发层方向门控 + 触碰/悬空拦截保证，不要求近期区间高位
# 2026-10-01 用户拍板：倒锤形线加入白名单（看涨 4 种），单锤/单倒锤恢复单根确认
# 2026-10-01-2 用户拍板（线上 KSMUSDT 回踩 EMA21 匹配倒锤个案）：倒锤形线移出看涨
# 白名单——看涨回踩语境里倒锤的长上影 = 反抽遭上方抛压打回，不是支撑企稳证据；
# 支撑企稳只认长下影探底回升（锤形线）。看涨白名单回到 3 种
# 2026-10-01-3 用户拍板：恢复完整 12 金K（看涨 6 + 看跌 6 对称）——2026-09-12
# 精简掉的刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑全部回归白名单参与信号判定
# （此前仅历史展示）；倒锤形线维持退出（-2 拍板）、上吊线维持被流星线替代
# （2026-09-28 拍板）。恢复形态均为多K复合结构（刺透/乌云/孕线）或与锤/流星
# 互补的 body≈0 十字（蜻蜓/墓碑，与锤形线不重复命中），单根直接确认
# 2026-10-09 用户拍板：孕线突破（两向）与蜻蜓/墓碑十字移出白名单（信号质量弱，
# 同 2026-09-12 精简口径）——金K清单 12→8（看涨/看跌各 4）。检测器与标签保留
# （历史扫描结果/待确认池诊断展示用），仅不再参与信号判定。常量更名
# GOLDEN_PATTERNS：数量随拍板浮动，不再入名
# B4 单一来源（2026-10-10，docs/20）：白名单拆为有序二元组（渲染顺序确定），
# GOLDEN_PATTERNS 为并集；ai_analyzer.SIGNAL_TYPE_LABELS 等展示文本一律经
# golden_whitelist_labels() 渲染，改白名单不再需要手工同步提示词标签
GOLDEN_BULLISH = (
    "hammer",              # 锤形线
    "bullish_engulfing",   # 看涨吞没
    "morning_star",        # 启明星
    "piercing_line",       # 刺透线
)
GOLDEN_BEARISH = (
    "shooting_star",       # 流星线（倒锤形出现在顶部）
    "bearish_engulfing",   # 看跌吞没
    "evening_star",        # 黄昏星
    "dark_cloud_cover",    # 乌云盖顶
)
GOLDEN_PATTERNS = set(GOLDEN_BULLISH) | set(GOLDEN_BEARISH)


def golden_whitelist_labels() -> str:
    """金K白名单中文清单（看涨/看跌各一组，顺序随常量元组），供提示词/标签渲染"""
    bull = "/".join(PATTERN_LABEL_MAP[p] for p in GOLDEN_BULLISH)
    bear = "/".join(PATTERN_LABEL_MAP[p] for p in GOLDEN_BEARISH)
    return f"看涨 {bull}，看跌 {bear}"


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
    "doji": "十字星",
    "hanging_man": "上吊线",
    "close_above_prev_high": "收盘破前高",
}


# ===== 单影线形态的复合确认（2026-09-29 用户拍板；2026-10-01 收窄至仅流星线）=====
# 流星线（倒锤形出现在顶部，看空）是"单根单影线"形态，单独作为回踩
# 金K确认证据太弱：单根不再触发，须满足其一——
#   ① 双流星：连续两根同形态（前根+当根，反抽受阻连续两次确认）
#   ② 流星+趋势K：前根流星 + 当根顺向趋势K（短影线长实体，实体 ≥60% 全长，
#      2026-10-10-4 拍板 0.70→0.60：回踩反转根常带探底下影——KAIAUSDT 10-10
#      13:00 复合路径实体占比 61.8% 被 70% 门拒，下影 28% 恰是回踩探底回升
#      的正常形态；突破K线长实体门维持 0.70 不动）
# 2026-10-01 用户拍板：锤形线移出本集合、倒锤形线加入白名单——单锤/单倒锤
# 恢复单根直接确认（探底回升语义足够强）；复合形态（吞没/启明星/黄昏星）
# 本身由多根K线构成、结构更强，维持单根确认。
# 2026-10-01-2 用户拍板：倒锤形线整体退出看涨白名单（GOLDEN_PATTERNS），单影线
# 复合确认维持仅流星线。
SINGLE_SHADOW_PATTERNS = {"shooting_star"}

# 顺向趋势K线实体/K线全长下限（2026-10-10-4 拍板 0.70→0.60）：复合确认证据
# 层放宽——回踩反转根常带探底下影，实体占比 60% 已有决断分量；与
# trigger.BREAKOUT_BODY_RANGE_MIN（突破K线，维持 0.70）不再同口径，
# 2026-09-30-2 的"两常量同步"约定就此解除
TREND_CANDLE_BODY_RANGE_MIN = 0.60


def detect_solid_trend_candle(klines: list[list], direction: int,
                              idx: int = -1) -> Optional[dict]:
    """顺向趋势K线：长实体（≥60% 全长，2026-10-10-4）+ 顺向（做多=阳线、做空=阴线）。

    供"锤+趋势K"复合确认使用（证据层口径，独立于突破K线的 0.70 触发层门；
    影线不做单侧额外限制——60% 实体天然影线合计 ≤40%）。
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
    """回踩金K证据统一裁决（2026-09-29 复合确认；2026-10-01 收窄+方向优先）。

    入参：
    - klines：全量K线（[-2]=当根已收盘、[-3]=前根已收盘）
    - wanted：期望方向 "bullish"/"bearish"（指标多数决方向）
    - patterns：detect_all_patterns(klines, idx=-2) 的当根形态检测结果
    返回 (golden, note)：
    - golden：确认证据形态 dict（pattern/direction），None=无有效证据
    - note：组合说明（""=单形态直接确认），trigger 层拼进触发 reason

    裁决顺序：
    0. 金K候选先按 wanted 方向过滤再取首个——同形双报倒锤/流星 strength
       并列，方向优先防 TA-Lib 单侧提升后反侧误拦（旧口径"取首个再验方向"
       会把方向不符的首个形态当一票否决，已废弃）。
       同向双报（流星 0.7 + 乌云盖顶/看跌吞没等直通形态并存）先取非单影线
       直通候选，取不到才回落 strength 序首个走复合路径（2026-10-01-3 补丁）
    1. 当根金K方向匹配且非单影线（白名单 8 种里仅流星线是单影线；倒锤形线
       2026-10-01-2 退出、孕线突破/蜻蜓/墓碑 2026-10-09 移出，方向过滤天然
       不入选）→ 直接确认
    2. 当根流星方向匹配 → 前根同形态（双流星）才确认，否则不确认
    3. 当根无金K → 前根锤（看多）/流星（看空）+ 当根顺向趋势K → 组合确认，
       pattern 落前根形态（前端展示/AI 提示词兼容）
    触点框架（当根触碰或 pending 次根确认）由 trigger 层负责，本函数只裁
    "形态证据是否成立"——无触碰语境的悬空组合不会绕过触点要求。
    """
    cand = [p for p in patterns
            if p["pattern"] in GOLDEN_PATTERNS and p["direction"] == wanted]
    # 直通优先（2026-10-01-3 补丁）：同向双报时先取非单影线候选——复合确认
    # 是"无直通证据时"的降级路径，不该反过来把同根K上并存的直通形态挡在
    # 门外（流星 0.7 恒高于乌云盖顶/看跌吞没 0.6~0.8 重叠区，纯 strength 序
    # 会让直通证据被单影线复合门误拦）。取不到非单影线候选才回落 strength
    # 序首个走复合路径
    golden = next((p for p in cand if p["pattern"] not in SINGLE_SHADOW_PATTERNS), None)
    if golden is None and cand:
        golden = cand[0]

    # 规则1：非单影线金K直接确认
    if golden and golden["pattern"] not in SINGLE_SHADOW_PATTERNS:
        return golden, ""

    direction = 1 if wanted == "bullish" else -1

    # 规则2：当根流星 → 双流星组合确认（2026-10-01 起单影线复合仅剩流星线）
    if golden:
        try:
            prev = detect_shooting_star(klines, -3)
        except Exception:
            prev = None
        if prev:
            return golden, "双流星组合"
        return None, ""  # 单流星不确认

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
