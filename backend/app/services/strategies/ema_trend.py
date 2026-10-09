"""ema_trend 策略插件（docs/19 外部 EMA 方案 + 策略 A 融合，docs/20 S1）。

纯规则趋势策略：4H EMA144 趋势闸门（斜率 + 缠绕 + ADX 三重否决）→
1H 三线排列 + ATR 归一发散度 → 回踩（优先）/金叉（补充）两类入场。
S1 影子期只产信号落库，不进执行层（开单权限 S2 经影子数据评审后解锁）。

铁律：
- 一律已收盘 K 线判定（klines 最后一根为进行中时由调用方截断——本模块
  统一自行剔除最后一根未收盘 K 线）；
- 纯函数：不碰 DB、不下单、不读全局配置；参数由 registry config 注入；
- 双向镜像（多头逻辑 + 空头对称），无仅做多假设。
"""
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.services.strategies.base import StrategySignal
from app.services.strategy.technical import analyze_adx

# EMA144 斜率回看根数（4h）与走平阈值（20 根变化幅度/价格 < 0.5% → 走平）
SLOPE_BARS = 20
SLOPE_FLAT_PCT = 0.005
# 缠绕判定：近 10 根收盘价穿越 EMA144 次数 ≥3 → 震荡拒绝
WRAP_WINDOW = 10
WRAP_MAX = 2


def ema_series(closes: np.ndarray, period: int) -> Optional[np.ndarray]:
    """EMA 全序列（前 period-1 根 NaN）。样本不足返回 None。"""
    if len(closes) < period + 5:
        return None
    a = 2.0 / (period + 1)
    out = np.full(len(closes), np.nan)
    out[period - 1] = closes[:period].mean()
    for i in range(period, len(closes)):
        out[i] = a * closes[i] + (1 - a) * out[i - 1]
    return out


def _closed(klines: list[list]) -> list[list]:
    """剔除最后一根未收盘 K 线（已收盘判定纪律）。"""
    return klines[:-1] if len(klines) >= 2 else []


@dataclass
class EmaTrendConfig:
    """策略私有参数（registry 条目 config 注入；越界值回 clamp 默认）。"""

    div_fast_mid: float = 0.5       # 1h |EMA21-EMA55|/ATR 下限
    div_mid_slow: float = 1.0       # 1h |EMA55-EMA144|/ATR 下限
    adx_min: float = 20.0           # 4h ADX 趋势闸门下限
    touch_band_atr: float = 0.3     # 回踩触点容差（ATR 倍数）
    stop_atr: float = 0.5           # 止损离均线距离（ATR 倍数）
    vol_pullback_max: float = 1.5   # 回踩当根量上限（×MA20，超出=放量下跌不确认）
    vol_cross_min: float = 1.2      # 金叉当根量下限（×MA20）
    min_rr: float = 1.0             # TP1（前高）距入场最小 R 数，不足则 TP1=0
    tp_swing_bars: int = 20         # 前高/前低回看根数

    @classmethod
    def from_dict(cls, cfg: Optional[dict]) -> "EmaTrendConfig":
        src = cfg or {}
        out = cls()
        for f in ("div_fast_mid", "div_mid_slow", "adx_min", "touch_band_atr",
                  "stop_atr", "vol_pullback_max", "vol_cross_min", "min_rr"):
            v = src.get(f)
            if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
                setattr(out, f, float(v))
        n = src.get("tp_swing_bars")
        if isinstance(n, int) and 10 <= n <= 200:
            out.tp_swing_bars = n
        return out


class EmaTrendStrategy:
    """EMA 21/55/144 趋势跟踪插件（纯规则，影子产信号）。"""

    id = "ema_trend"
    display_name = "EMA 趋势跟踪"

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = EmaTrendConfig.from_dict(cfg)

    # ── 协议方法 ──────────────────────────────────────────────────────────

    def scan_candidates(self, universe: list[str]) -> list[str]:
        """第一版透传 universe（闸门在 build_signal 内判定，K 线只拉一次）。"""
        return list(universe)

    def build_signal(self, symbol: str, klines_1h: list[list],
                     klines_4h: list[list]) -> Optional[StrategySignal]:
        """对单 symbol 产出信号。klines 由调用方注入（纯函数可测）；
        外层协议签名 build_signal(symbol) 的行情注入在此类以显式参数承接。"""
        k1, k4 = _closed(klines_1h), _closed(klines_4h)
        if len(k1) < 200 or len(k4) < 200:
            return None

        gate = self._gate_4h(k4)
        if gate is None:
            return None
        direction, ev4 = gate

        structure = self._structure_1h(k1, direction)
        if structure is None:
            return None
        ind, ev1 = structure

        sig = self._pullback(symbol, direction, k1, ind) \
            or self._cross(symbol, direction, k1, ind)
        if sig is None:
            return None
        sig.raw["gate_4h"] = ev4
        sig.raw["structure_1h"] = ev1
        return sig

    def exit_spec(self) -> dict:
        """出场语义声明（S3 前不被引擎消费）：纯跟踪 + 结构失效出场，
        参数细节以 docs/19 §3.4 回测结论为准。"""
        return {
            "tp_ladder": None,           # 无固定止盈档（或按回测结论 tp_mode=swing_high）
            "be_arm": "atr_1x",
            "trail": "atr_2x",
            "sig_exit": "ema55_break",   # V2 菜单之一，回测定稿
        }

    # ── 4H 趋势闸门（三重否决：走平 / 缠绕 / ADX）────────────────────────

    def _gate_4h(self, k4: list[list]) -> Optional[tuple[str, dict]]:
        c = np.array([float(k[4]) for k in k4], dtype=float)
        e144 = ema_series(c, 144)
        if e144 is None or not np.isfinite(e144[-1]) or not np.isfinite(e144[-SLOPE_BARS]):
            return None
        price = float(c[-1])
        slope_pct = abs(float(e144[-1]) - float(e144[-SLOPE_BARS])) / price

        # 缠绕：近 WRAP_WINDOW 根收盘价在 EMA144 两侧的穿越次数
        wraps = 0
        for i in range(len(k4) - WRAP_WINDOW, len(k4)):
            side_now = c[i] - e144[i]
            side_prev = c[i - 1] - e144[i - 1]
            if side_now * side_prev < 0:
                wraps += 1

        adx_info = analyze_adx(k4, period=14)
        adx_val = float(adx_info["adx"]) if adx_info else None

        ev = {"slope_pct": round(slope_pct, 5), "wraps": wraps,
              "adx": round(adx_val, 1) if adx_val is not None else None,
              "close_vs_ema144": round(price / float(e144[-1]) - 1, 5)}

        if slope_pct < SLOPE_FLAT_PCT:
            return None  # 走平
        if wraps > WRAP_MAX:
            return None  # 缠绕
        if adx_val is None or adx_val < self.cfg.adx_min:
            return None  # 无趋势

        up = price > e144[-1] and e144[-1] >= e144[-SLOPE_BARS]
        down = price < e144[-1] and e144[-1] <= e144[-SLOPE_BARS]
        if up:
            return "long", {**ev, "direction": "up"}
        if down:
            return "short", {**ev, "direction": "down"}
        return None

    # ── 1H 结构（排列 + 发散度）──────────────────────────────────────────

    def _structure_1h(self, k1: list[list], direction: str):
        c = np.array([float(k[4]) for k in k1], dtype=float)
        e21 = ema_series(c, 21)
        e55 = ema_series(c, 55)
        e144 = ema_series(c, 144)
        if any(e is None or not np.isfinite(e[-1]) for e in (e21, e55, e144)):
            return None

        # ATR14（1h，Wilder 简化：TR 均值滚动）
        h = np.array([float(k[2]) for k in k1], dtype=float)
        low = np.array([float(k[3]) for k in k1], dtype=float)
        tr = np.maximum(h[1:] - low[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])))
        atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0
        if atr <= 0:
            return None

        d21, d55, d144 = float(e21[-1]), float(e55[-1]), float(e144[-1])
        if direction == "long":
            aligned = d21 > d55 > d144
            div_fm = (d21 - d55) / atr
            div_ms = (d55 - d144) / atr
        else:
            aligned = d21 < d55 < d144
            div_fm = (d55 - d21) / atr
            div_ms = (d144 - d55) / atr
        ev = {"aligned": aligned, "div_fast_mid": round(div_fm, 3),
              "div_mid_slow": round(div_ms, 3), "atr": round(atr, 8)}
        if not aligned or div_fm < self.cfg.div_fast_mid or div_ms < self.cfg.div_mid_slow:
            return None
        return (d21, d55, d144, atr, c, h, low), ev

    # ── 入场信号：回踩优先，金叉补充 ─────────────────────────────────────

    def _pullback(self, symbol: str, direction: str, k1: list, ind) -> Optional[StrategySignal]:
        d21, d55, _d144, atr, c, h, low = ind
        bar = k1[-1]
        o, cl, hi, lo = float(bar[1]), float(bar[4]), float(bar[2]), float(bar[3])
        v = float(bar[5])
        v_ma20 = float(np.mean([float(k[5]) for k in k1[-20:]]))
        tol = self.cfg.touch_band_atr * atr
        d = 1 if direction == "long" else -1

        # 触点：影线触及 EMA21/EMA55 容差带（多头看下影 lo，空头看上影 hi）
        probe = lo if d > 0 else hi
        touch_ref, touch_val = None, None
        for name, val in (("EMA21", d21), ("EMA55", d55)):
            if val - tol <= probe <= val + tol:
                touch_ref, touch_val = name, val
                break
        if touch_ref is None:
            return None

        # 企稳：方向 K 线（多头阳线/空头阴线）
        if d * (cl - o) <= 0:
            return None
        # 收盘站回均线之上（多头）/之下（空头）
        if d * (cl - touch_val) <= 0:
            return None
        # 回踩不放巨量（放量下跌非健康回踩）
        if v_ma20 > 0 and v > self.cfg.vol_pullback_max * v_ma20:
            return None

        stop = touch_val - d * self.cfg.stop_atr * atr
        tp1 = self._swing_tp(direction, k1, float(cl), stop, atr)
        ev = {"touched": touch_ref, "vol_vs_ma20": round(v / v_ma20, 2) if v_ma20 else None}
        return StrategySignal(
            strategy_id=self.id, symbol=symbol, direction=direction,
            entry=float(cl), stop=round(stop, 10), tp1=tp1, tp2=0.0,
            reason=f"1H回踩{touch_ref}企稳（4H趋势闸门通过）",
            confidence=0.6, signal_type="pullback", raw=ev,
        )

    def _cross(self, symbol: str, direction: str, k1: list, ind) -> Optional[StrategySignal]:
        d21, d55, d144, atr, c, h, low = ind
        bar = k1[-1]
        o, cl = float(bar[1]), float(bar[4])
        v = float(bar[5])
        v_ma20 = float(np.mean([float(k[5]) for k in k1[-20:]]))
        d = 1 if direction == "long" else -1

        # 金叉/死叉：EMA21 上（下）穿 EMA55（已收盘两根对比）
        if len(k1) < 3:
            return None
        e21_s = ema_series(c, 21)
        e55_s = ema_series(c, 55)
        if e21_s is None or e55_s is None:
            return None
        cross_now = d * (e21_s[-1] - e55_s[-1]) > 0 and d * (e21_s[-2] - e55_s[-2]) <= 0
        if not cross_now:
            return None
        # 金叉必须在 EMA144 有利侧 + 方向 K 线 + 放量确认
        if d * (cl - d144) <= 0:
            return None
        if d * (cl - o) <= 0:
            return None
        if v_ma20 <= 0 or v < self.cfg.vol_cross_min * v_ma20:
            return None

        stop = d55 - d * self.cfg.stop_atr * atr
        tp1 = self._swing_tp(direction, k1, float(cl), stop, atr)
        return StrategySignal(
            strategy_id=self.id, symbol=symbol, direction=direction,
            entry=float(cl), stop=round(stop, 10), tp1=tp1, tp2=0.0,
            reason="1H金叉+方向K线+放量（4H趋势闸门通过）",
            confidence=0.55, signal_type="cross", raw={"vol_vs_ma20": round(v / v_ma20, 2)},
        )

    def _swing_tp(self, direction: str, k1: list, entry: float,
                  stop: float, atr: float) -> float:
        """TP1 = 回看窗口前高（多头）/前低（空头）；距入场不足 min_rr 则 0（不设）。"""
        n = self.cfg.tp_swing_bars
        window = k1[-n - 1:-1] if len(k1) > n else k1[:-1]
        d = 1 if direction == "long" else -1
        swing = max(float(k[2]) for k in window) if d > 0 else min(float(k[3]) for k in window)
        r_unit = abs(entry - stop)
        if r_unit <= 0 or d * (swing - entry) < self.cfg.min_rr * r_unit:
            return 0.0
        return round(swing, 10)
