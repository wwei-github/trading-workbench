"""震荡评分卡（docs/19 §3.2 L2，8 项扣分制，纯函数）。

与 7 票 votes 正交：votes 回答「值不值得做」（加分制），评分卡回答
「现在是不是震荡」（扣分制）。**起步只算不拦**（观测口径）——升软闸门
的启用条件预注册在 docs/19 §3.2（L1 ≥4 周且 ≥200 条建议单、分位数
重估阈值、区分度验证、软闸门 2 周无申诉），此处默认阈值 = 外部方案
出厂值仅供观测期统计，**不得直接用于拦截**。

所有判定一律已收盘 K 线（传入序列含未收盘根时由内部统一剔除，与
technical.py 同口径）。
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.services.strategy.technical import analyze_adx, analyze_bollinger


@dataclass
class RangeScoreConfig:
    """评分卡阈值（观测期出厂值；P1 定标后由 scan_filters 配置覆盖）。"""

    slope_flat_pct: float = 0.005    # 项1：4h EMA144 斜率走平阈值
    wrap_max: int = 2                # 项2：缠绕 ≥3 次扣分（wrap_count > wrap_max）
    div_fast_mid_min: float = 0.5    # 项4：1h 发散度下限
    adx_min_1h: float = 18.0         # 项5：1h ADX 下限
    bbw_low_pct: float = 30.0        # 项6：BBW 处近 120 根低分位阈值（%）
    bbw_lookback: int = 120          # 项6：BBW 分位回看根数
    cross_ema21_max: int = 3         # 项7：近 20 根穿 EMA21 ≥4 次扣分
    vol_cv_max: float = 0.3          # 项8：近 20 根量能变异系数下限


@dataclass
class RangeScoreResult:
    score: int                       # 0~8，≥3（定标后）视为震荡
    items: list[dict] = field(default_factory=list)  # [{no, name, hit, value}]

    def as_dict(self) -> dict:
        return {"score": self.score, "items": self.items}


def _wrap_count(c: np.ndarray, e: np.ndarray, window: int) -> int:
    """收盘价在均线两侧的穿越次数（近 window 根）。"""
    n = 0
    for i in range(len(c) - window, len(c)):
        if (c[i] - e[i]) * (c[i - 1] - e[i - 1]) < 0:
            n += 1
    return n


def _cross_count(c: np.ndarray, e: np.ndarray, window: int) -> int:
    """收盘价上/下穿均线次数（近 window 根，含穿越瞬间与横穿）。"""
    n = 0
    for i in range(len(c) - window, len(c)):
        if (c[i] - e[i]) * (c[i - 1] - e[i - 1]) < 0:
            n += 1
    return n


def _ema(closes: np.ndarray, period: int) -> Optional[np.ndarray]:
    if len(closes) < period + 5:
        return None
    a = 2.0 / (period + 1)
    out = np.full(len(closes), np.nan)
    out[period - 1] = closes[:period].mean()
    for i in range(period, len(closes)):
        out[i] = a * closes[i] + (1 - a) * out[i - 1]
    return out


def compute_range_score(
    klines_1h: list[list],
    klines_4h: list[list],
    cfg: Optional[RangeScoreConfig] = None,
) -> RangeScoreResult:
    """8 项扣分制震荡评分（docs/19 §3.2 表格逐项实现）。

    返回 RangeScoreResult；K 线不足的项目记 hit=None（不算分不误伤）。
    """
    cfg = cfg or RangeScoreConfig()
    items: list[dict] = []

    closed4 = klines_4h[:-1] if len(klines_4h) >= 2 else []
    closed1 = klines_1h[:-1] if len(klines_1h) >= 2 else []
    c4 = np.array([float(k[4]) for k in closed4], dtype=float) if closed4 else np.array([])
    c1 = np.array([float(k[4]) for k in closed1], dtype=float) if closed1 else np.array([])

    # 项1：4h EMA144 斜率走平
    e144_4 = _ema(c4, 144) if len(c4) else None
    v1 = None
    if e144_4 is not None and len(c4) > 20 and np.isfinite(e144_4[-20]) and np.isfinite(e144_4[-1]):
        v1 = abs(float(e144_4[-1]) - float(e144_4[-20])) / float(c4[-1])
    hit1 = v1 < cfg.slope_flat_pct if v1 is not None else None
    items.append({"no": 1, "name": "4h_EMA144斜率走平", "hit": hit1,
                  "value": round(v1, 5) if v1 is not None else None})

    # 项2：4h 缠绕
    w2 = _wrap_count(c4, e144_4, 10) if e144_4 is not None and len(c4) > 11 else None
    hit2 = w2 > cfg.wrap_max if w2 is not None else None
    items.append({"no": 2, "name": "4h缠绕EMA144", "hit": hit2, "value": w2})

    # 项3：1h 三线不排列（多空都不满足）
    tri = None
    if len(c1) >= 149:
        e21 = _ema(c1, 21)
        e55 = _ema(c1, 55)
        e144 = _ema(c1, 144)
        if all(e is not None and np.isfinite(e[-1]) for e in (e21, e55, e144)):
            a, b, cv = float(e21[-1]), float(e55[-1]), float(e144[-1])
            tri = a > b > cv or a < b < cv
    hit3 = tri is False if tri is not None else None
    items.append({"no": 3, "name": "1h三线不排列", "hit": hit3, "value": tri})

    # 项4：1h 发散度不足（|EMA21-EMA55|/ATR14）
    div_fm = None
    if len(c1) >= 70:
        e21 = _ema(c1, 21)
        e55 = _ema(c1, 55)
        h1 = np.array([float(k[2]) for k in closed1], dtype=float)
        low1 = np.array([float(k[3]) for k in closed1], dtype=float)
        tr = np.maximum(h1[1:] - low1[1:],
                        np.maximum(np.abs(h1[1:] - c1[:-1]), np.abs(low1[1:] - c1[:-1])))
        atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0
        if e21 is not None and e55 is not None and np.isfinite(e21[-1]) and np.isfinite(e55[-1]) and atr > 0:
            div_fm = abs(float(e21[-1]) - float(e55[-1])) / atr
    hit4 = div_fm < cfg.div_fast_mid_min if div_fm is not None else None
    items.append({"no": 4, "name": "1h发散度不足", "hit": hit4,
                  "value": round(div_fm, 3) if div_fm is not None else None})

    # 项5：1h ADX 弱
    adx1 = analyze_adx(klines_1h, period=14)
    v5 = float(adx1["adx"]) if adx1 else None
    hit5 = v5 < cfg.adx_min_1h if v5 is not None else None
    items.append({"no": 5, "name": "1h_ADX弱", "hit": hit5,
                  "value": round(v5, 1) if v5 is not None else None})

    # 项6：BBW 低分位（收口）
    v6 = None
    bb = analyze_bollinger(klines_1h)
    if bb and len(closed1) >= cfg.bbw_lookback + 25:
        bw_series = []
        look = closed1[-(cfg.bbw_lookback + 20):]
        cc = np.array([float(k[4]) for k in look], dtype=float)
        for i in range(20, len(cc)):
            win = cc[i - 20:i]
            mid = win.mean()
            sd = win.std()
            if sd > 0:
                bw_series.append(4 * sd / mid)
        if len(bw_series) >= 30 and bb.get("bandwidth") is not None:
            arr = np.array(bw_series, dtype=float)
            v6 = float((arr < float(bb["bandwidth"])).mean() * 100.0)
    hit6 = v6 < cfg.bbw_low_pct if v6 is not None else None
    items.append({"no": 6, "name": "BBW低分位收口", "hit": hit6,
                  "value": round(v6, 1) if v6 is not None else None})

    # 项7：价格反复穿越 EMA21（近 20 根 ≥4 次）
    v7 = None
    if len(c1) >= 90:
        e21_1 = _ema(c1, 21)
        if e21_1 is not None:
            v7 = _cross_count(c1, e21_1, 20)
    hit7 = v7 > cfg.cross_ema21_max if v7 is not None else None
    items.append({"no": 7, "name": "反复穿越EMA21", "hit": hit7, "value": v7})

    # 项8：量能低迷（近 20 根成交量变异系数 < 0.3）
    v8 = None
    if len(closed1) >= 20:
        vols = np.array([float(k[5]) for k in closed1[-20:]], dtype=float)
        m = float(vols.mean())
        if m > 0:
            v8 = float(vols.std() / m)
    hit8 = v8 < cfg.vol_cv_max if v8 is not None else None
    items.append({"no": 8, "name": "量能低迷", "hit": hit8,
                  "value": round(v8, 3) if v8 is not None else None})

    score = sum(1 for it in items if it["hit"] is True)
    return RangeScoreResult(score=score, items=items)
