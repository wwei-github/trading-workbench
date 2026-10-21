"""技术指标包：MACD / 布林带 / RSI / SuperTrend / ADX / OBV / 4h 背景

AI 分析两条管线共用注入（P0 ai_analyzer._build_messages 与 P1 ai_agent._build_user_msg）：
方向按「方向票」多数决（2026-09-17 起；2026-10-15 指标拆角色，docs/21 O9）：
仅 MACD/布林带/RSI 三张方向票计方向，同向 ≥2 张即定向，不再强制"以 K 线为准"；
票数统计由程序完成，注入文本中的"方向票"行直接给结论。

- 全部基于已收盘 K 线（剔除最后一根未收盘），与 ema.py 同口径
- 每个指标一个 analyze_* 函数：数据不足返回 None，返回 dict 含 state/state_label/detail，
  方向性指标额外带 dir（1 多 / -1 空 / 0 中性）供共振统计
- render_indicator_block 供两条管线一行接入；单指标异常只降级该行，不影响分析
- calc_ema 复用 ema.py（其不依赖 strategy 包内其他模块，无循环导入）
"""
import logging
from statistics import median
from typing import Optional

import numpy as np

from app.services.strategy.candlestick import decision_shadow_bonus
from app.services.strategy.ema import calc_ema
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.swing import find_swing_points

logger = logging.getLogger(__name__)


def _closed_closes(klines: list[list]) -> np.ndarray:
    """已收盘 K 线的收盘价数组（剔除最后一根未收盘）"""
    closed = klines[:-1] if len(klines) >= 2 else klines
    return np.array([float(k[4]) for k in closed], dtype=float)


def _macd_series(
    klines: list[list], fast: int = 12, slow: int = 26, signal: int = 9,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """MACD 序列内部计算（analyze_macd / analyze_macd_div 共用）。

    返回 (dif, dea, hist) 数组——尾部与已收盘K线逐根对齐（截断只影响序列头部），
    数据不足（已收盘 < slow+signal+10）返回 None。
    """
    closes = _closed_closes(klines)
    if len(closes) < slow + signal + 10:
        return None

    f = calc_ema(closes, fast)
    s = calc_ema(closes, slow)
    if f is None or s is None:
        return None
    dif_valid = (f - s)[slow - 1:]  # slow-1 之前 EMA 慢线未成形（NaN）
    dea_full = calc_ema(dif_valid, signal)
    if dea_full is None:
        return None
    dif = dif_valid[signal - 1:]
    dea = dea_full[signal - 1:]
    return dif, dea, dif - dea


def analyze_macd(
    klines: list[list], fast: int = 12, slow: int = 26, signal: int = 9,
) -> Optional[dict]:
    """MACD(fast,slow,signal)：DIF=EMA快-EMA慢，DEA=DIF 的 EMA，柱=DIF-DEA（国际口径）。

    状态优先级：3 根内金叉/死叉 > 零轴同侧动能（多头/空头）> 多空胶着。
    数据不足（已收盘 < slow+signal+10）返回 None。
    """
    series = _macd_series(klines, fast, slow, signal)
    if series is None:
        return None
    dif, dea, hist = series

    dn, dea_n, hn = float(dif[-1]), float(dea[-1]), float(hist[-1])

    cross_up = cross_down = False
    for i in range(len(dif) - 3, len(dif)):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            cross_up = True
        elif dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
            cross_down = True

    if cross_up:
        state, cross_desc = "golden_cross", "3根内金叉"
    elif cross_down:
        state, cross_desc = "death_cross", "3根内死叉"
    elif dn > 0 and hn > 0:
        state, cross_desc = "bullish", ""
    elif dn < 0 and hn < 0:
        state, cross_desc = "bearish", ""
    else:
        state, cross_desc = "mixed", ""

    # 柱体动能：最近两根柱绝对值放大/收敛
    hist_trend = "柱体放大" if abs(hn) > abs(float(hist[-2])) else "柱体收敛"

    parts = ([cross_desc] if cross_desc else []) + [
        f"DIF={dn:.6g}", f"DEA={dea_n:.6g}", f"柱={hn:.6g}",
        f"零轴{'上' if dn > 0 else '下'}方", hist_trend,
    ]
    labels = {
        "golden_cross": "金叉", "death_cross": "死叉",
        "bullish": "多头动能", "bearish": "空头动能", "mixed": "多空胶着",
    }
    return {
        "dif": dn, "dea": dea_n, "hist": hn,
        "state": state, "state_label": labels[state],
        "dir": 1 if state in ("golden_cross", "bullish")
        else -1 if state in ("death_cross", "bearish") else 0,
        "detail": ", ".join(parts),
    }


def analyze_macd_div(
    klines: list[list], fast: int = 12, slow: int = 26, signal: int = 9,
    half: int = 12,
) -> Optional[dict]:
    """MACD 背离（docs/14 CARD-04，缠论盘整背驰可取子集）：近 2*half 根已收盘
    K线前后半段比较。

    - diverge_bear：后半段价格新高（max high 抬升）而 MACD 柱峰值衰减——
      价新高动能不跟随，多头信号质量差
    - diverge_bull：后半段价格新低（min low 下移）而柱低谷抬升——空头信号质量差
    - confirm：其余（与 cvd_div 桶语义对齐）
    数据不足（MACD 序列未成形或柱序列不足 2*half 根）返回 None。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < 2 * half:
        return None
    series = _macd_series(klines, fast, slow, signal)
    if series is None:
        return None
    _dif, _dea, hist = series
    if len(hist) < 2 * half:
        return None
    # 柱序列尾部与已收盘K线尾部逐根对齐，取末 2*half 根比较前后半段
    closed = closed[-2 * half:]
    hist = hist[-2 * half:]
    high = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)

    hi1, hi2 = float(high[:half].max()), float(high[half:].max())
    lo1, lo2 = float(low[:half].min()), float(low[half:].min())
    p1, p2 = float(hist[:half].max()), float(hist[half:].max())
    v1, v2 = float(hist[:half].min()), float(hist[half:].min())

    if hi2 > hi1 and p2 < p1:
        bucket = "diverge_bear"
    elif lo2 < lo1 and v2 > v1:
        bucket = "diverge_bull"
    else:
        bucket = "confirm"
    return {
        "price_high_chg_pct": round((hi2 / hi1 - 1) * 100, 3) if hi1 > 0 else None,
        "price_low_chg_pct": round((lo2 / lo1 - 1) * 100, 3) if lo1 > 0 else None,
        "hist_peak_delta": round(p2 - p1, 8),
        "hist_trough_delta": round(v2 - v1, 8),
        "bucket": bucket,
    }


def analyze_bollinger(
    klines: list[list], period: int = 20, ndev: float = 2.0,
) -> Optional[dict]:
    """布林带(period,ndev)：SMA 中轨 ± ndev 倍总体标准差（TA-Lib 同口径 ddof=0）。

    状态=最新收盘价相对轨道的位置；带宽取近 60 根滚动分位，低位=收窄（酝酿变盘）。
    """
    closes = _closed_closes(klines)
    if len(closes) < period + 10:
        return None

    window = closes[-period:]
    mid = float(window.mean())
    if mid <= 0:
        return None
    std = float(window.std(ddof=0))
    upper, lower = mid + ndev * std, mid - ndev * std
    price = float(closes[-1])
    bw = (upper - lower) / mid * 100

    if price > upper:
        state, label = "above_upper", "上轨上方"
    elif price < lower:
        state, label = "below_lower", "下轨下方"
    elif price >= mid:
        state, label = "above_mid", "中轨上方"
    else:
        state, label = "below_mid", "中轨下方"

    # 带宽近 60 根滚动分位（数据足够才算）；bw_rank 数值供质量分使用
    rank_desc = ""
    bw_rank: Optional[float] = None
    if len(closes) >= period + 60:
        bws = []
        for i in range(len(closes) - 60, len(closes)):
            w = closes[i - period + 1: i + 1]
            m = float(w.mean())
            if m > 0:
                bws.append(float(w.std(ddof=0)) * 2 * ndev / m * 100)
        if bws:
            rank = sum(1 for b in bws if b < bw) / len(bws) * 100
            bw_rank = rank
            zone = "收窄" if rank <= 25 else "张开" if rank >= 80 else "常态"
            rank_desc = f", 带宽近60根分位={rank:.0f}%, {zone}"

    return {
        "upper": upper, "mid": mid, "lower": lower, "bandwidth": bw,
        "bw_rank": bw_rank,
        "state": state, "state_label": label,
        "dir": 1 if price >= mid else -1,
        "detail": (
            f"上轨={upper:.6g}, 中轨={mid:.6g}, 下轨={lower:.6g}, "
            f"带宽={bw:.2f}%{rank_desc}"
        ),
    }


def _wilder_rsi(closes: np.ndarray, period: int) -> Optional[np.ndarray]:
    """Wilder 平滑 RSI 序列（种子=首 period 段涨跌幅均值，其后 (prev*(p-1)+cur)/p）"""
    deltas = np.diff(closes)
    if len(deltas) < period + 5:
        return None
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    ag = float(gains[:period].mean())
    al = float(losses[:period].mean())
    out = []
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + float(gains[i])) / period
        al = (al * (period - 1) + float(losses[i])) / period
        out.append(100.0 if al <= 0 and ag > 0 else 50.0 if al <= 0
                   else 100.0 - 100.0 / (1.0 + ag / al))
    return np.array(out)


def analyze_rsi(klines: list[list], period: int = 14) -> Optional[dict]:
    """RSI(period)：Wilder 平滑。状态=超买/偏强/中性/偏弱/超卖 + 近 3 根趋势。"""
    closes = _closed_closes(klines)
    if len(closes) < period + 15:
        return None
    vals = _wilder_rsi(closes, period)
    if vals is None or len(vals) < 4:
        return None

    v = float(vals[-1])
    if v >= 70:
        state, label = "overbought", "超买"
    elif v >= 55:
        state, label = "strong", "偏强"
    elif v >= 45:
        state, label = "neutral", "中性"
    elif v >= 30:
        state, label = "weak", "偏弱"
    else:
        state, label = "oversold", "超卖"

    diff3 = v - float(vals[-4])
    trend = "上行" if diff3 > 1 else "下行" if diff3 < -1 else "走平"
    return {
        "rsi": v, "state": state, "state_label": label,
        "dir": 1 if v >= 55 else -1 if v <= 45 else 0,
        "detail": f"RSI={v:.1f}, {trend}",
    }


def macd_zero_state(klines: list[list], fast: int = 12, slow: int = 26,
                    signal: int = 9) -> Optional[dict]:
    """MACD 零轴状态（docs/19 §3.3 策略 A 第三道企稳确认的证据函数）。

    判定（多头口径，空头镜像）：
    - above: DIF 与 DEA 均在零轴上方（动量确认最强）；
    - pullback_hold: 回踩零轴不破——最近 3 根内 DIF 或 DEA 曾触及零轴下侧
      但当前双双回到零轴上方（健康回踩，未真正破位）；
    - below / mixed: 零轴下方 / 胶着（多头动量不确认）。
    数据不足返回 None。纯证据函数，不投权重——供 ema_trend raw 证据与
    未来 golden_k 企稳确认（P1 观测后）复用。
    """
    series = _macd_series(klines, fast, slow, signal)
    if series is None or len(series[0]) < 5:
        return None
    dif, dea, _hist = series
    d_now, e_now = float(dif[-1]), float(dea[-1])
    d_min3 = float(min(dif[-3:]))
    e_min3 = float(min(dea[-3:]))

    if d_now > 0 and e_now > 0:
        if d_min3 <= 0 or e_min3 <= 0:
            state, label = "pullback_hold", "回踩零轴不破"
        else:
            state, label = "above", "零轴上方"
        dir_ = 1
    elif d_now < 0 and e_now < 0:
        if d_min3 >= 0 or e_min3 >= 0:
            state, label = "pullback_hold", "反抽零轴不破"
        else:
            state, label = "below", "零轴下方"
        dir_ = -1
    else:
        state, label, dir_ = "mixed", "零轴胶着", 0
    return {"state": state, "state_label": label, "dir": dir_,
            "dif": d_now, "dea": e_now}


def analyze_stochastic(
    klines: list[list], k_period: int = 14, k_smooth: int = 3, d_smooth: int = 3,
) -> Optional[dict]:
    """Stochastic（KDJ 口径，docs/19 L1 观测候选：股票流派「KDJ 20~50 金叉」）。

    RSV = (收 − N 最低) / (N 最高 − N 最低) × 100；K = RSV 的 k_smooth SMA；
    D = K 的 d_smooth SMA；J = 3K − 2D（国内 KDJ 口径）。
    状态：超买（K≥80）/偏强/中性/偏弱/超卖（K≤20）+ 近 3 根内金叉/死叉标记。
    数据不足（已收盘 < k_period + k_smooth + d_smooth + 5）返回 None。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    need = k_period + k_smooth + d_smooth + 5
    if n < need:
        return None
    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    c = np.array([float(k[4]) for k in closed], dtype=float)

    hh = np.array([h[i - k_period + 1:i + 1].max() for i in range(k_period - 1, n)])
    ll = np.array([low[i - k_period + 1:i + 1].min() for i in range(k_period - 1, n)])
    rng = hh - ll
    rsv = np.where(rng > 0, (c[k_period - 1:] - ll) / np.where(rng > 0, rng, 1.0) * 100.0, 50.0)

    def _sma(x: np.ndarray, w: int) -> np.ndarray:
        return np.convolve(x, np.ones(w) / w, mode="valid")

    k_full = _sma(rsv, k_smooth)
    if len(k_full) < d_smooth:
        return None
    d_full = _sma(k_full, d_smooth)
    k_line = k_full[d_smooth - 1:]   # 截头与 D 线对齐（K 多经一次平滑）
    d_line = d_full
    k, d = float(k_line[-1]), float(d_line[-1])
    j_val = 3 * k - 2 * d

    cross = 0
    for i in range(max(1, len(d_line) - 3), len(d_line)):
        if k_line[i] > d_line[i] and k_line[i - 1] <= d_line[i - 1]:
            cross = 1
        elif k_line[i] < d_line[i] and k_line[i - 1] >= d_line[i - 1]:
            cross = -1

    if k >= 80:
        state, label = "overbought", "超买"
    elif k >= 55:
        state, label = "strong", "偏强"
    elif k >= 45:
        state, label = "neutral", "中性"
    elif k >= 20:
        state, label = "weak", "偏弱"
    else:
        state, label = "oversold", "超卖"
    return {
        "k": k, "d": d, "j": j_val, "state": state, "state_label": label,
        "cross": cross,  # 1=近3根金叉 -1=近3根死叉 0=无
        "dir": 1 if (cross == 1 and k <= 50) else -1 if (cross == -1 and k >= 50) else 0,
        "detail": f"K={k:.1f} D={d:.1f} J={j_val:.1f} {label}",
    }


def atr_series(klines: list[list], period: int = 14) -> Optional[np.ndarray]:
    """Wilder 平滑 ATR 序列（D1，docs/24）：与 SuperTrend 内部 ATR 同口径抽出复用。

    TR[i] = max(高-低, |高-前收|, |低-前收|)（首根取高-低），
    种子 = 前 period 根 TR 均值，其后 atr[i] = (atr[i-1]*(p-1)+tr[i])/p。
    输入含未收盘根（内部剔除最后一根，与文件内其他函数同口径）；
    序列前 period-1 项为 NaN（种子未成形）。已收盘不足 period+5 根返回 None。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    if n < period + 5:
        return None
    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    c = np.array([float(k[4]) for k in closed], dtype=float)

    tr = np.empty(n)
    tr[0] = h[0] - low[0]
    tr[1:] = np.maximum(
        h[1:] - low[1:], np.maximum(abs(h[1:] - c[:-1]), abs(low[1:] - c[:-1])),
    )
    atr = np.full(n, np.nan)
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def analyze_supertrend(
    klines: list[list], period: int = 10, mult: float = 3.0,
) -> Optional[dict]:
    """SuperTrend(period,mult)：Wilder ATR 通道跟踪，收盘价越线翻转。

    状态=多头/空头趋势；detail 含趋势线值、距现价距离、最近翻转距今根数。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    if n < period + 15:
        return None
    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    c = np.array([float(k[4]) for k in closed], dtype=float)

    # Wilder ATR（D1：抽出 atr_series 供指标块 ATR 行复用，口径与原实现一致）
    atr = atr_series(klines, period)

    start = period  # 首个同时具备完整 ATR 与前收盘的索引
    hl2 = (h + low) / 2
    basic_ub = hl2 + mult * atr
    basic_lb = hl2 - mult * atr

    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)  # 1=多头 -1=空头
    final_ub[start - 1] = basic_ub[start - 1]
    final_lb[start - 1] = basic_lb[start - 1]
    trend[start - 1] = 1 if c[start - 1] >= hl2[start - 1] else -1

    for i in range(start, n):
        # 通道只朝趋势方向收紧（防回撤扩带）
        final_ub[i] = (basic_ub[i] if basic_ub[i] < final_ub[i - 1] or c[i - 1] > final_ub[i - 1]
                       else final_ub[i - 1])
        final_lb[i] = (basic_lb[i] if basic_lb[i] > final_lb[i - 1] or c[i - 1] < final_lb[i - 1]
                       else final_lb[i - 1])
        if trend[i - 1] == 1:
            trend[i] = -1 if c[i] < final_lb[i] else 1
        else:
            trend[i] = 1 if c[i] > final_ub[i] else -1

    line_arr = np.where(trend == 1, final_lb, final_ub)
    line = float(line_arr[-1])
    if not np.isfinite(line) or line <= 0:
        return None
    price = float(c[-1])
    dist = (price - line) / line * 100

    state = "bullish" if trend[-1] == 1 else "bearish"
    bars_ago = None
    for i in range(n - 1, start, -1):
        if trend[i] != trend[i - 1]:
            bars_ago = n - 1 - i
            break

    parts = [f"线={line:.6g}", f"距现价{dist:+.2f}%"]
    if bars_ago is not None:
        parts.append(f"{bars_ago}根前翻转")
    return {
        "line": line, "direction": state,
        "state": state,
        "state_label": "多头趋势" if state == "bullish" else "空头趋势",
        "dir": 1 if state == "bullish" else -1,
        "detail": ", ".join(parts),
    }


def analyze_adx(klines: list[list], period: int = 14) -> Optional[dict]:
    """ADX(period)：Wilder 平滑的趋势强度，+DI/-DI 给出趋势方向。

    状态：ADX≥25 强趋势 / 20~25 趋势酝酿 / <20 震荡（无趋势）；
    方向由 +DI/-DI 的大小判定（仅 ADX≥20 时计入共振，弱震荡不给方向）。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    if n < period * 2 + 16:
        return None
    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    c = np.array([float(k[4]) for k in closed], dtype=float)

    # +DM/-DM/TR（长度 n-1，对齐 i=1..n-1）
    up_move = h[1:] - h[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = np.empty(n)
    tr[0] = h[0] - low[0]
    tr[1:] = np.maximum(
        h[1:] - low[1:], np.maximum(abs(h[1:] - c[:-1]), abs(low[1:] - c[:-1])),
    )
    tr1 = tr[1:]

    # Wilder 累计平滑：sm[i] = sm[i-1] - sm[i-1]/p + cur（种子=前 p 项求和）
    def _wilder_sum(arr: np.ndarray) -> Optional[np.ndarray]:
        m = len(arr)
        if m < period + 5:
            return None
        sm = np.full(m, np.nan)
        sm[period - 1] = arr[:period].sum()
        for i in range(period, m):
            sm[i] = sm[i - 1] - sm[i - 1] / period + arr[i]
        return sm

    sm_p, sm_m, sm_tr = _wilder_sum(plus_dm), _wilder_sum(minus_dm), _wilder_sum(tr1)
    if sm_p is None or sm_m is None or sm_tr is None:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = 100.0 * sm_p / sm_tr
        minus_di = 100.0 * sm_m / sm_tr
        dx = 100.0 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-12)
    dx_valid = dx[~np.isnan(dx)]
    if len(dx_valid) < period + 5:
        return None
    # ADX：DX 的 Wilder 平滑（种子=首 period 个 DX 均值）
    adx_arr = np.full(len(dx_valid), np.nan)
    adx_arr[period - 1] = dx_valid[:period].mean()
    for i in range(period, len(dx_valid)):
        adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx_valid[i]) / period
    adx = float(adx_arr[-1])
    pdi, mdi = float(plus_di[-1]), float(minus_di[-1])
    if not np.isfinite(adx):
        return None

    bull = pdi > mdi
    trend_dir = "多头" if bull else "空头"
    di_dir = (1 if bull else -1) if adx >= 20 else 0
    if adx >= 25:
        state, label = "strong", f"强趋势（{trend_dir}）"
    elif adx >= 20:
        state, label = "developing", f"趋势酝酿（{trend_dir}）"
    else:
        state, label = "ranging", "震荡（无趋势）"

    return {
        "adx": adx, "plus_di": pdi, "minus_di": mdi,
        "state": state, "state_label": label, "dir": di_dir,
        "detail": f"ADX={adx:.1f}, +DI={pdi:.1f}, -DI={mdi:.1f}",
    }


def analyze_obv(klines: list[list], lookback: int = 20) -> Optional[dict]:
    """OBV 量价关系：近 lookback 根 OBV 净变化 ÷ 同期总量，与价格方向对比判背离。

    状态：量价齐升（资金流入）/ 量价齐跌（资金流出）/ 顶背离（价涨量出）/
    底背离（价跌量入）/ 中性。背离是共振中的重要反向信号。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    if n < lookback + 15:
        return None
    c = np.array([float(k[4]) for k in closed], dtype=float)
    v = np.array([float(k[5]) for k in closed], dtype=float)
    total_vol = float(v[-lookback:].sum())
    if total_vol <= 0:
        return None

    obv_delta = np.sign(np.diff(c)) * v[1:]
    obv = np.concatenate([[0.0], np.cumsum(obv_delta)])
    ratio = float(obv[-1] - obv[-1 - lookback]) / total_vol
    price_chg = (float(c[-1]) / float(c[-1 - lookback]) - 1) * 100

    if ratio >= 0.05 and price_chg > 0:
        state, label, d = "inflow", "量价齐升（资金流入）", 1
    elif ratio <= -0.05 and price_chg > 0:
        state, label, d = "bear_div", "顶背离（价涨量出）", -1
    elif ratio <= -0.05 and price_chg < 0:
        state, label, d = "outflow", "量价齐跌（资金流出）", -1
    elif ratio >= 0.05 and price_chg < 0:
        state, label, d = "bull_div", "底背离（价跌量入）", 1
    else:
        state, label, d = "neutral", "中性", 0

    return {
        "state": state, "state_label": label, "dir": d,
        "detail": (
            f"近{lookback}根价格{price_chg:+.2f}%, "
            f"OBV净量占总量{ratio * 100:+.0f}%"
        ),
    }


# ── 4h EMA 状态机（2026-10-11 再重构，docs/17）─────────────────────────────
# 4h 背景从「div 排列强度分档（+早前的过热/冷却期）」重构为 EMA 状态机 11 态：
# 三线（EMA21/55/144）的金叉/死叉/拐头/排列直接定名状态，状态→行为纯函数
# （锁单矩阵查表）。退役说明：
# - div 强度档 weak/medium/strong/extreme：角度阈值分档语义模糊（稳态下
#   strong 与「不过热」近乎互斥），div 原始值保留作 detail 注记证据
# - 过热期/冷却期（2026-10-11 早前引入）：过热是预警（抛物线可能继续走很久，
#   禁开误杀面大），拐头是事实发生——警戒职责统一收敛到 turn 态
# - 宏观守卫（EMA55 vs EMA144）被状态机自然吸收：排列/交叉态恒宏观同侧、
#   过渡态恒逆侧，不再是独立分支
# 判定优先级：tangle → macro_cross → turn → recover → aligned → transition
CROSS_FRESH_BARS = 10       # 交叉「初期」窗口（55×144 与 21×55 共用）
TURN_CONFIRM_BARS = 2       # 拐头确认：EMA21 斜率连续反向根数（幅度确认，
                            # 吸收同日衰竭期退役的单根翻向噪声教训）
TANGLE_CROSS_LOOKBACK = 10  # 缠绕第三道：近 10 根 21×55 交叉 ≥2 次（锯齿）

# ── 4h 闸门常量（2026-10-20 由 ema_gate 迁入）────────────────────────────────
# 4h 趋势闸门与 4h 状态机合并为 htf_context 单入口（用户拍板）：一份 core、一套
# 「缠绕」判据（detect_tangle，两处共用）、一个方向权威。原 gate_4h 的三重否决
# （走平/缠绕/ADX）拆解为：走平=独立拒因 flat；缠绕=并入统一判据；ADX=缠绕第一道。
MIN_CLOSED_4H = 200         # 4h 数据门（EMA144 斜率回看 -SLOPE_BARS 需更长历史，
                            # 比 htf_core 的 155 更严；两处统一采用本门）
SLOPE_BARS = 20             # EMA144 斜率回看根数（4h 闸门与 1h 结构门 144 方向共用）
SLOPE_FLAT_PCT = 0.005      # 20 根变化幅度/价格 < 0.5% → 走平（独立拒因）
TANGLE_ADX_MIN = 20.0       # 缠绕第一道：ADX<20 无趋势
TANGLE_GAP_PCT = 0.001      # 缠绕第二道：|EMA21-EMA55|/price < 0.1% 纠缠
TANGLE_WRAP_N = 10          # 缠绕第四道：价格×EMA144 穿越回看根数（原 gate WRAP_WINDOW）
TANGLE_WRAP_MAX = 2         # 缠绕第四道：穿越 >2 次（≥3）→ 震荡（原 gate WRAP_MAX）

STATE_LABELS = {
    "tangle": "缠绕",
    "turn_bull": "多头拐头", "turn_bear": "空头拐头",
    "macro_cross_bull": "宏观金叉初期", "macro_cross_bear": "宏观死叉初期",
    "bull_recover": "多头恢复初期", "bear_recover": "空头恢复初期",
    "aligned_bull": "多头排列", "aligned_bear": "空头排列",
    "bull_dip": "牛内回调", "bear_rally": "熊内反弹",
}
STATE_BLOCK_BOTH = ("tangle", "turn_bull", "turn_bear")   # 双向禁开
STATE_LOCK_CONTRA = (                                     # 锁逆势（宏观同侧）
    "macro_cross_bull", "macro_cross_bear",
    "bull_recover", "bear_recover",
    "aligned_bull", "aligned_bear",
)
STATE_TRANSITION = ("bull_dip", "bear_rally")             # 宏观逆侧过渡态（回调/反弹区）
# 前置门放行白名单（2026-10-20 用户拍板，硬改无开关）：htf_context 的 block_both
# 收紧为「非本集即双向禁开」——4h 前置门仅放行多头/空头排列（aligned_*），其余
# 状态（turn_* 拐头 / macro_cross_* 交叉初期 / *_recover 恢复初期 / bull_dip·
# bear_rally 过渡态）一律拦下，映射漏斗关卡 htf_state_lock（tangle 仍由
# reject_reason 先行否决，映射 gate_4h）。STATE_BLOCK_BOTH 保留原义（tangle/turn_*），
# 仅供 AI 层 enforce_htf_constraint 使用，不参与前置门。
STATE_ALIGNED = ("aligned_bull", "aligned_bear")

# 质量分（同向加分, 逆向减分）：tangle/turn 双向禁开后仅扫描侧排序兜底；
# macro_cross 趋势级事件独享最高档（原 extreme 语义被吸收）
STATE_QUALITY = {
    "tangle": (0, 0),
    "turn_bull": (-8, -4), "turn_bear": (-8, -4),
    "macro_cross_bull": (12, -14), "macro_cross_bear": (12, -14),
    "bull_recover": (8, -10), "bear_recover": (8, -10),
    "aligned_bull": (10, -12), "aligned_bear": (10, -12),
    "bull_dip": (5, -6), "bear_rally": (5, -6),
}
_STATE_LEGACY_NEUTRAL = (5, -8)  # 退役旧档（weak/medium/strong/extreme/
                                 # exhausted/overheat/cooling）存量行兜底
_STATE_DIR = {   # 状态方向（多数决第 7 票 & 质量分同逆判定；tangle 不投票）
    "tangle": 0,
    "turn_bull": 1, "turn_bear": -1,
    "macro_cross_bull": 1, "macro_cross_bear": -1,
    "bull_recover": 1, "bear_recover": -1,
    "aligned_bull": 1, "aligned_bear": -1,
    "bull_dip": -1, "bear_rally": 1,
}
_STATE_MACRO = {  # 宏观对齐：True 同侧 / False 逆侧 / None 未评估（禁开态）
    "tangle": None,
    "turn_bull": None, "turn_bear": None,
    "macro_cross_bull": True, "macro_cross_bear": True,
    "bull_recover": True, "bear_recover": True,
    "aligned_bull": True, "aligned_bear": True,
    "bull_dip": False, "bear_rally": False,
}


def _sign_changes(f: np.ndarray, m: np.ndarray, n: int) -> int:
    """近 n 根内 f−m 的符号变号次数（交叉计数；NaN 种子区自动剔除）"""
    d = (f - m)[-n:]
    d = d[np.isfinite(d)]
    if len(d) < 2:
        return 0
    return int(np.sum(d[1:] * d[:-1] < 0))


def _wrap_count(closes: np.ndarray, e144: np.ndarray, n: int = TANGLE_WRAP_N) -> int:
    """近 n 根收盘价在 EMA144 两侧的穿越次数（原 gate_4h 口径，逐根比对）"""
    if len(closes) <= n:
        return 0
    cnt = 0
    for i in range(len(closes) - n, len(closes)):
        if (closes[i] - e144[i]) * (closes[i - 1] - e144[i - 1]) < 0:
            cnt += 1
    return cnt


def detect_tangle(
    closes: np.ndarray, e21: np.ndarray, e55: np.ndarray, e144: np.ndarray,
    adx_v: Optional[float],
) -> tuple[bool, str]:
    """4h「缠绕」统一判据（2026-10-20 合并，原两套口径取并集，两处共用）：

    1. ADX<20 无趋势（原状态机第一道；原 gate 的独立 ADX 否决并入此道）
    2. |EMA21-EMA55|/price < 0.1% 纠缠（原状态机第二道）
    3. 近 10 根 21×55 交叉 ≥2 次（锯齿，原状态机第三道）
    4. 近 10 根收盘价穿越 EMA144 >2 次（震荡，原 gate_4h 缠绕口径）

    返回 (是否缠绕, 命中证据文本)。ADX 不可用（None）不计入本判据——原状态机
    对 ADX=None fail-open，gate 的 fail-closed 由调用方（htf_context）另行处理。
    """
    price = float(closes[-1])
    if adx_v is not None and adx_v < TANGLE_ADX_MIN:
        return True, f"ADX{adx_v:.0f}<20 无趋势"
    if price > 0 and abs(float(e21[-1]) - float(e55[-1])) / price < TANGLE_GAP_PCT:
        return True, "21/55间距<0.1% 纠缠"
    sc = _sign_changes(e21, e55, TANGLE_CROSS_LOOKBACK)
    if sc >= 2:
        return True, f"近{TANGLE_CROSS_LOOKBACK}根21×55交叉{sc}次（锯齿）"
    wraps = _wrap_count(closes, e144)
    if wraps > TANGLE_WRAP_MAX:
        return True, f"近{TANGLE_WRAP_N}根价格穿越EMA144 {wraps}次（震荡）"
    return False, ""


def _ema_state(
    closes: np.ndarray, s21: np.ndarray, s55: np.ndarray, s144: np.ndarray,
    adx_v: Optional[float], atr4h: float,
) -> tuple[str, list[str], dict]:
    """4h EMA 状态机（11 态，判定优先级即代码顺序，末根已收盘序列）：

    1. tangle 缠绕（detect_tangle 四道合一，2026-10-20 起与 4h 闸门共用同一判据）：
       ADX<20 ∨ 21/55 间距<0.1% ∨ 近10根21×55交叉≥2次（锯齿）∨ 近10根价格穿越
       EMA144>2次（震荡）
    2. macro_cross_*：55×144 交叉后 <CROSS_FRESH_BARS 根（严格小于，age=10
       已出初期）——趋势级事件优先定名（深 V 双叉同步时 21 回踩不误判拐头/恢复）
    3. turn_*：仅全排列内 EMA21 斜率连续 TURN_CONFIRM_BARS 根反向（还没
       全排列过就无「头」可拐；交叉初期回踩天然落 recover/过渡态）
    4. *_recover：宏观同侧的 21×55 新交叉 <CROSS_FRESH_BARS 根（金叉+宏观
       多=多头恢复 / 死叉+宏观空=空头恢复；宏观逆侧的新交叉落过渡态）
    5. aligned_*：全排列稳态（无近期交叉）
    6. bull_dip/bear_rally：宏观逆侧过渡态（21×55 关系定名，任意龄）

    返回 (state, notes, meta)；notes=状态判据证据（含年龄），meta 携带两个
    交叉对的 (_htf_cross_age 口径) 年龄与方向供上层补注记。"""
    price = float(closes[-1])
    age_m, dir_m = _htf_cross_age(s55, s144)  # 宏观 55×144
    age_c, dir_c = _htf_cross_age(s21, s55)   # 中周期 21×55
    meta = {"age_c": age_c, "dir_c": dir_c, "age_m": age_m, "dir_m": dir_m}

    # 1. 缠绕（四道合一，2026-10-20 与 4h 闸门合并为 detect_tangle 单判据）
    tangled, t_note = detect_tangle(closes, s21, s55, s144, adx_v)
    if tangled:
        return "tangle", [t_note], meta

    full_bull = s21[-1] > s55[-1] > s144[-1]
    full_bear = s21[-1] < s55[-1] < s144[-1]
    slope_now = float(s21[-1]) - float(s21[-2])
    slope_prev = float(s21[-2]) - float(s21[-3])

    # 2. 宏观交叉初期（趋势级事件，金叉/死叉位置定方向）
    if age_m is not None and age_m < CROSS_FRESH_BARS:
        pos = (
            f"，价距144约{abs(price - float(s144[-1])) / atr4h:.1f}×ATR"
            if atr4h > 0 else ""
        )
        if dir_m == 1:
            return "macro_cross_bull", [f"55×144金叉后{age_m}根{pos}"], meta
        return "macro_cross_bear", [f"55×144死叉后{age_m}根{pos}"], meta

    # 3. 拐头（仅全排列内：EMA21 斜率连续 TURN_CONFIRM_BARS 根反向）
    if full_bull and slope_now < 0 and slope_prev < 0:
        return "turn_bull", [f"EMA21斜率连续{TURN_CONFIRM_BARS}根下行（排列内拐头）"], meta
    if full_bear and slope_now > 0 and slope_prev > 0:
        return "turn_bear", [f"EMA21斜率连续{TURN_CONFIRM_BARS}根上行（排列内拐头）"], meta

    # 4. 恢复初期（宏观同侧的 21×55 新交叉；价格位置注记同宏观交叉）
    if age_c is not None and age_c < CROSS_FRESH_BARS:
        pos = (
            f"，价距144约{abs(price - float(s144[-1])) / atr4h:.1f}×ATR"
            if atr4h > 0 else ""
        )
        if dir_c == 1 and s55[-1] > s144[-1]:
            return "bull_recover", [f"21×55金叉后{age_c}根（宏观多头侧恢复）{pos}"], meta
        if dir_c == -1 and s55[-1] < s144[-1]:
            return "bear_recover", [f"21×55死叉后{age_c}根（宏观空头侧恢复）{pos}"], meta

    # 5/6. 排列稳态 / 过渡态（全排列与过渡完备分割，无第五种落点）
    if full_bull:
        return "aligned_bull", [], meta
    if full_bear:
        return "aligned_bear", [], meta
    if s21[-1] > s55[-1]:  # 55<144：宏观空头内的反弹
        return "bear_rally", [], meta
    return "bull_dip", [], meta  # 21<55 且 55>144：宏观多头内的回调


def _htf_cross_age(f: np.ndarray, m: np.ndarray) -> tuple[Optional[int], int]:
    """EMA21/55 最后一次交叉：(距最新已收盘根的根数, 交叉后方向 ±1)。

    序列内无交叉的极端情况返回 (None, 当前排列方向)。
    慢线 EMA 以 SMA 种子成形，种子位之前 diff 为 NaN（NaN>0 恒 False）——
    从首个有效位的下一位起扫，避免 NaN→有效值边界被误判为一次交叉。
    """
    diff = f - m
    n = len(diff)
    side_now = 1 if diff[-1] > 0 else -1
    valid = np.where(~np.isnan(diff))[0]
    if len(valid) == 0:
        return None, side_now
    for i in range(n - 1, int(valid[0]), -1):
        if (diff[i] > 0) != (diff[i - 1] > 0):
            return n - 1 - i, (1 if diff[i] > 0 else -1)
    return None, side_now


def htf_core(htf_klines: list[list], fast: int = 21, slow: int = 55) -> Optional[dict]:
    """4h 共享计算核心（2026-10-13 合并）：一次算好 EMA21/55/144 + ADX(14) +
    ATR(14,4h)，供 gate_4h 与 analyze_htf_background 共用——同一份 4h 序列不再
    双算。EMA 用 ema.calc_ema（SMA 种子 + 2/(N+1) 递推，与 gate_4h 旧用的
    ema_gate.ema_series 同源，合并后数值不变）。

    输入原始 4h K 线（尾根未收盘，内部统一剔除）。返回
    {closed, closes, e21, e55, e144, adx, atr, price}；样本不足（已收盘 <155，
    EMA144 需 SMA 种子 + 收敛余量）返回 None。adx 计算失败降级 None（调用方
    按各自口径处理：gate_4h 视作无趋势否决、状态机跳过该道判据）。
    """
    if not htf_klines or len(htf_klines) < 2:
        return None
    closed = htf_klines[:-1]
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    if len(closes) < 155:
        return None
    e21 = calc_ema(closes, fast)
    e55 = calc_ema(closes, slow)
    e144 = calc_ema(closes, 144)
    if e21 is None or e55 is None or e144 is None:
        return None
    if not np.isfinite(e21[-1]) or float(e144[-1]) <= 0:
        return None
    # ADX（状态机缠绕第一道判据；analyze_adx 内部同样剔除未收盘根，口径一致；
    # 计算失败静默降级 None）
    adx_res = analyze_adx(htf_klines)
    adx_v = float(adx_res["adx"]) if adx_res else None
    # ATR(14,4h)：TR14 均值（K 线格式 [ts,o,h,l,c,vol]）
    tr_list = []
    for i in range(1, len(closed)):
        h_i, l_i = float(closed[i][2]), float(closed[i][3])
        c_prev = float(closed[i - 1][4])
        tr_list.append(max(h_i - l_i, abs(h_i - c_prev), abs(l_i - c_prev)))
    atr = float(np.mean(tr_list[-14:])) if len(tr_list) >= 14 else 0.0
    return {
        "closed": closed, "closes": closes,
        "e21": e21, "e55": e55, "e144": e144,
        "adx": adx_v, "atr": atr, "price": float(closes[-1]),
    }


def analyze_htf_background(
    htf_klines: list[list], fast: int = 21, slow: int = 55,
    core: Optional[dict] = None,
) -> Optional[dict]:
    """4h 高周期背景：EMA 状态机 11 态（1h 分析的大方向参照）。

    htf_klines 为 4h 周期 K 线（最后一根未收盘，内部剔除）；core 为可选的 4h
    共享计算核心（htf_core）——传入则复用其 EMA/ADX/ATR，免去本函数再算一遍，
    缺省时内部自算（向后兼容）。
    状态判定见 _ema_state（缠绕 → 宏观交叉 → 拐头 → 恢复 → 排列 → 过渡，
    优先级即其代码顺序）；行为矩阵（锁单/质量分查表）：
    - tangle / turn_*：双向禁开（缠绕无趋势 / 排列内拐头警戒）
    - macro_cross_* / *_recover / aligned_*：宏观同侧锁逆势
    - bull_dip / bear_rally：宏观逆侧过渡态恒放行（质量分兜底）
    dir=状态方向（tangle=0 不投票；过渡态按 21×55 关系定）；
    macro_aligned 被状态机自然吸收（锁逆势态恒 True / 过渡态恒 False /
    tangle·turn None 未评估）；div（排列角度 ATR）退役为纯注记证据；
    age_bars=21×55 交叉年龄注记，macro_cross_age=55×144 交叉年龄。

    返回 {state(11态key), state_label, dir, macro_aligned,
          age_bars, macro_cross_age, div, detail}
    """
    core = core or htf_core(htf_klines, fast, slow)
    if core is None:
        return None
    return _htf_state_view(core, fast, slow)


def _htf_state_view(core: dict, fast: int = 21, slow: int = 55) -> Optional[dict]:
    """状态机视图（analyze_htf_background 与 htf_context 共用，单一实现）。

    2026-10-20 抽出：4h 闸门与状态机合并后，两入口共用同一状态装配逻辑，
    避免「状态判定」出现第二份实现。返回结构与 analyze_htf_background 一致。
    """
    closes = core["closes"]
    f, m, s144 = core["e21"], core["e55"], core["e144"]
    fn, mn, sn = float(f[-1]), float(m[-1]), float(s144[-1])
    if sn <= 0 or not np.isfinite(fn):
        return None
    adx_v = core["adx"]
    atr4h = core["atr"]
    div_fm = abs(fn - mn) / atr4h if atr4h > 0 else None
    div_ms = abs(mn - sn) / atr4h if atr4h > 0 else None
    div_min = min(div_fm, div_ms) if div_fm is not None and div_ms is not None else None

    # 状态机判定（tangle → macro_cross → turn → recover → aligned → transition）
    state, st_notes, meta = _ema_state(closes, f, m, s144, adx_v, atr4h)
    dir_ = _STATE_DIR[state]
    macro_aligned = _STATE_MACRO[state]

    # 注记组装：状态判据证据优先；未作为判据的交叉年龄补注
    # （macro_cross/recover 的对应年龄已在 st_notes 内，不重复）
    notes = list(st_notes)
    if state not in ("macro_cross_bull", "macro_cross_bear") \
            and meta["age_m"] is not None:
        w = "金叉" if meta["dir_m"] == 1 else "死叉"
        notes.append(f"55×144{w}后{meta['age_m']}根")
    if state not in ("bull_recover", "bear_recover"):
        if meta["age_c"] is not None:
            w = "金叉" if meta["dir_c"] == 1 else "死叉"
            notes.append(f"21×55{w}后{meta['age_c']}根")
        else:
            notes.append("21×55窗口内无交叉（极老趋势）")
    if div_min is not None:
        notes.append(f"排列角度{div_min:.2f}ATR（仅注记）")

    detail = (
        f"EMA{fast}={fn:.6g}, EMA{slow}={mn:.6g}, EMA144={sn:.6g} · "
        + " · ".join(notes)
    )
    if adx_v is not None:
        detail += f" · ADX={adx_v:.1f}"
    return {
        "state": state, "state_label": STATE_LABELS[state], "dir": dir_,
        "macro_aligned": macro_aligned,
        "age_bars": meta["age_c"], "macro_cross_age": meta["age_m"],
        "div": div_min, "detail": detail,
        # 4h 强度三要素（2026-10-21 T2-6 影子评分用）：ADX、EMA144 斜率分位、
        # 价格距 EMA144 的 ATR 倍数。只进质量分「影子分项」，不改变现有返回分与排序
        "adx": round(float(adx_v), 1) if adx_v is not None else None,
        "price_vs_ema144_atr": (
            round(abs(float(closes[-1]) - sn) / atr4h, 3) if atr4h > 0 else None
        ),
        "slope_pctile": _slope_pctile(s144, float(closes[-1])),
    }


def _slope_pctile(e144: np.ndarray, price: float, lookback: int = 200) -> Optional[float]:
    """EMA144 斜率（相对价格）在近 lookback 根内的分位（0~1，越大越陡）。

    斜率口径与 htf_context 的 slope_pct 同源：|EMA144[t]-EMA144[t-SLOPE_BARS]|/价格。
    数据不足 20 个有效样本返回 None（不做强度打分，避免短历史噪声）。
    """
    n = len(e144)
    if n < SLOPE_BARS + 2 or price <= 0:
        return None
    vals: list[float] = []
    for i in range(max(SLOPE_BARS, n - lookback), n):
        a, b = e144[i - SLOPE_BARS], e144[i]
        if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
            continue
        vals.append(abs(float(b) - float(a)) / price)
    if len(vals) < 20:
        return None
    cur = vals[-1]
    return round(sum(1 for v in vals if v <= cur) / len(vals), 4)


def htf_context(htf_klines: list[list], core: Optional[dict] = None) -> Optional[dict]:
    """4h 合并入口（2026-10-20 用户拍板）：趋势闸门决策 + 背景状态 + 行为矩阵。

    合并前 4h 有两道独立前置门（ema_gate.gate_4h 三重否决 + 状态机双向禁开/锁
    逆势），各自定义「缠绕」与数据门，且方向双源（价格×EMA144 位置+斜率 vs
    21/55/144 排列）。合并后：一份 core、一套 detect_tangle 判据、一个入口。
    原 gate_4h 的价格×EMA144 位置与 EMA144 斜率降为本入口的趋势健康证据；
    走平(flat) 保留为独立拒因，ADX 并入缠绕判据。

    返回 None = 数据/实现异常（调用方按旧 gate_4h 异常路径降级放行）；
    否则返回 dict（含 analyze_htf_background 全字段 + 合并字段）：
    - direction "long"/"short" 或 None（None ⇒ reject_reason 非空）
    - reject_reason: None|"data"|"flat"|"tangle"|"adx"|"undetermined"
      （映射漏斗关卡 gate_4h；"adx" 仅表示 ADX 计算不可用的 fail-closed）
    - block_both: 非多头/空头排列（aligned_*）即双向禁开——映射漏斗关卡
      htf_state_lock（含 turn_* 拐头、macro_cross_*/*_recover 未完全排列、
      bull_dip/bear_rally 过渡态；tangle 已由 reject_reason 先行否决）。2026-10-20
      用户拍板收紧：4h 前置门仅放行 aligned_bull/aligned_bear（硬改无开关）。
    - evidence: {slope_pct, wraps, adx, close_vs_ema144, direction(up/down)}
    逆势锁方向不单列：调用方以 state∈STATE_LOCK_CONTRA 判定，方向读 dir。
    """
    core = core or htf_core(htf_klines)
    if core is None:
        return None
    closed, closes = core["closed"], core["closes"]
    e21, e55, e144 = core["e21"], core["e55"], core["e144"]
    price = core["price"]
    adx_v = core["adx"]

    direction: Optional[str] = None
    reject: Optional[str] = None
    ev: dict = {}
    if len(closed) < MIN_CLOSED_4H:
        reject = "data"
    elif not np.isfinite(float(e144[-1])) \
            or not np.isfinite(float(e144[-SLOPE_BARS])):
        reject = "data"
    else:
        slope_pct = abs(float(e144[-1]) - float(e144[-SLOPE_BARS])) / price
        wraps = _wrap_count(closes, e144)
        ev = {"slope_pct": round(slope_pct, 5), "wraps": wraps,
              "adx": round(adx_v, 1) if adx_v is not None else None,
              "close_vs_ema144": round(price / float(e144[-1]) - 1, 5)}
        if slope_pct < SLOPE_FLAT_PCT:
            reject = "flat"                      # 走平（独立拒因）
        else:
            tangled, _t_note = detect_tangle(closes, e21, e55, e144, adx_v)
            if tangled:
                reject = "tangle"                # 缠绕（含 ADX<20 道）
            elif adx_v is None:
                reject = "adx"                   # ADX 不可用 → fail-closed
            else:
                up = price > e144[-1] and e144[-1] >= e144[-SLOPE_BARS]
                down = price < e144[-1] and e144[-1] <= e144[-SLOPE_BARS]
                if up:
                    direction = "long"
                    ev["direction"] = "up"
                elif down:
                    direction = "short"
                    ev["direction"] = "down"
                else:
                    reject = "undetermined"      # 位置与斜率不同号（原隐性 None）

    state_view = _htf_state_view(core)
    out = dict(state_view) if state_view else {}
    st = out.get("state")
    out.update({
        "direction": direction,
        "reject_reason": reject,
        "block_both": st not in STATE_ALIGNED,
        "evidence": ev,
    })
    return out


def enforce_htf_constraint(
    ai_result: dict, htf_klines: Optional[list], signal_type: Optional[str] = None,
) -> dict:
    """4h 状态机硬约束的程序化兜底（原地修改并返回 ai_result）。

    锁单矩阵（2026-10-11 再重构为 EMA 状态机 11 态，docs/17）：
    - tangle 缠绕 / turn_* 拐头 → 双向禁开（缠绕无趋势 / 排列内 EMA21 连续
      反向=趋势衰竭警戒；只管开仓，持仓出场不受影响——出场引擎独立运行，
      「闸门管进、持仓管出」）；
    - macro_cross_* / *_recover / aligned_*（宏观同侧态）→ 锁逆势
      （宏观对齐由状态机自然吸收，无需另查 EMA55 vs 144）；
    - bull_dip / bear_rally（宏观逆侧过渡态）→ 放行，质量分兜底；但回踩
      确认单（signal_type=trigger_pullback_confirm）例外，程序 veto 转
      影子 A/B（见下「影子 A/B」段）。
    LLM 可能无视 prompt 注入的约束行，开单前程序把关。无 4h 数据或非
    suggest 时原样返回；suggest 且有 4h 数据时顺带 stash htf_phase（=状态
    key），供 AIAnalysis / 复盘「4h趋势期」维度落库。

    影子 A/B（2026-10-20，用户拍板）：过渡态 × 回踩确认 → 程序 veto、不进真单。
    决策假设（2026-10-20 正式网复盘，仅正式网、已剔除测试网异常单）：逆宏观回踩
    缺乏趋势跟随赖以盈利的肥尾、期望为负，而同侧背景同信号盈利。
    入库快照口径复核（trade_records.ai_snapshot->>'htf_phase'，该列 2026-10-09 起
    写入）：该组合 n=3（均值 −0.334R、最佳 +1.07R）、同侧对照 n=186（long 81 均
    −0.165R / short 105 均 +0.128R，方向差异 t≈1.4、p≈0.16 不显著）——样本远不足
    以单独判读，仅作影子观察。原注释所引「74 笔均值 −0.29R / 最佳 +2.4R，同侧
    +0.16R / 最佳 +6.3R」无留存脚本、无法按上述快照列复现（疑为对历史 K 线回溯
    重算 4h 状态），判读前应补可复现口径。
    本分支只否决入场，veto_original 三价快照仍保留，由复盘任务
    （run_trade_review_task）回放假想表现，与同侧通过单逐日对比，构成样本外
    影子 A/B（ab_bucket 标记供统计隔离）。
    """
    try:
        if not htf_klines or ai_result.get("trade_decision") != "suggest":
            return ai_result
        htf = analyze_htf_background(htf_klines)
        if not htf:
            return ai_result
        st = htf["state"]
        ai_result["htf_phase"] = st
        direction = (ai_result.get("direction") or "").lower()

        def _veto(reason: str) -> dict:
            # veto 闭环（docs/24 A5a）：程序否决的 suggest 同样快照原始建议，
            # 复盘回放假想表现，验证 4h 硬约束的贡献（键名与 dual_judge 对齐）
            return {
                "source": "htf_constraint",
                "direction": ai_result.get("direction"),
                "entry_price": ai_result.get("entry_price"),
                "stop_loss": ai_result.get("stop_loss"),
                "take_profit_1": ai_result.get("take_profit_1"),
                "take_profit_2": ai_result.get("take_profit_2"),
                "risk_reward_ratio": ai_result.get("risk_reward_ratio"),
                "recommendation": ai_result.get("recommendation"),
                "reason": reason,
            }

        _label = STATE_LABELS[st]

        # 4h 单一权威（2026-10-21 T3-2）：筛选层只放行 STATE_ALIGNED，兜底约束
        # 必须用同一常量，否则非 aligned 状态在提示词/兜底里出现第二套口径。
        # 过渡态 × 回踩确认仍先走影子 A/B 快照（保留 ab_bucket 统计隔离）。
        if st in STATE_TRANSITION and signal_type == "trigger_pullback_confirm":
            _v = _veto(f"影子AB：过渡态×回踩确认（4h{_label}）")
            _v["ab_bucket"] = "transition_pbc"   # 统计隔离标记
            ai_result["trade_decision"] = "skip"
            ai_result["skip_reason"] = (
                f"状态约束：4h{_label}（非多头/空头排列）影子 A/B——回踩确认单程序 veto"
                "（不入真单，原始建议留档复盘）"
            )
            ai_result["veto_original"] = _v
            return ai_result
        if st not in STATE_ALIGNED:
            if st == "tangle":
                _why = "缠绕无趋势"
            elif st.startswith("turn_"):
                _why = "排列内拐头，趋势衰竭警戒"
            else:
                _why = "非多头/空头排列，4h 前置门不放行"
            ai_result["trade_decision"] = "skip"
            ai_result["skip_reason"] = (
                f"状态约束：4h{_label}（{_why}）双向禁开新仓（程序强制 skip）"
            )
            ai_result["veto_original"] = _veto(f"4h{_label}双向禁开闸（{_why}）")
            return ai_result

        # 宏观同侧态 → 锁逆势（排列/交叉方向即 4h 方向）
        if st in STATE_LOCK_CONTRA:
            htf_dir = int(htf["dir"])
            if direction == "short" and htf_dir == 1:
                ai_result["trade_decision"] = "skip"
                ai_result["skip_reason"] = (
                    f"方向约束：4h{_label}背景禁止开空（程序强制 skip）"
                )
                ai_result["veto_original"] = _veto("4h 状态机方向硬约束（宏观同侧）")
            elif direction == "long" and htf_dir == -1:
                ai_result["trade_decision"] = "skip"
                ai_result["skip_reason"] = (
                    f"方向约束：4h{_label}背景禁止开多（程序强制 skip）"
                )
                ai_result["veto_original"] = _veto("4h 状态机方向硬约束（宏观同侧）")
            # 同向 → 放行
        # 过渡态非回踩确认（bull_dip/bear_rally 其他信号）已在上方统一拦截：
        # 非 aligned 一律双向禁开（2026-10-21 T3-2 单一权威）
    except Exception as e:
        logger.warning("4h方向约束兜底检查失败（放行）: %s", e)
    return ai_result


# 注入顺序与参数标注（顺序即渲染顺序）；短名用于共振行展示
_INDICATORS = (
    ("MACD(12,26,9)", "MACD", analyze_macd),
    ("布林带(20,2)", "布林带", analyze_bollinger),
    ("RSI(14)", "RSI", analyze_rsi),
    ("SuperTrend(10,3)", "SuperTrend", analyze_supertrend),
    ("ADX(14)", "ADX", analyze_adx),
    ("OBV(20)", "OBV", analyze_obv),
)
HTF_DISPLAY, HTF_SHORT = "4h背景(EMA状态)", "4h背景"

# ── 指标角色分工（2026-10-15 拍板，docs/21 O9）────────────────────────────
# 方向票：直接回答多空、对 1h 入场有时效性的三张——MACD / 布林带 / RSI。
# 门槛 = 方向票同向 ≥DIR_VOTE_MIN 张（scanner），net = 方向票净差（符号=方向）。
# 其余指标退出方向票、避免滞后指标主导 1h 方向：
# - ADX(14) 表趋势强度非方向，强度交由 range_score 项5（1h ADX<18 预警）承担；
# - OBV(20) 量价指标，改由质量分「顶/底背离」扣分（原有逻辑，不重复投方向票）；
# - SuperTrend(10,3) 趋势跟踪天然滞后，逆向时仅作风险扣分（RISK_VOTE_PENALTY）；
# - 4h背景已被前置硬门 gate_4h 一票否决覆盖，仍写入 votes_json 供 AI/质量分，
#   但不计方向票（避免同一 4h 事实在筛选链里被二次投票）。
DIR_VOTE_SHORTS = ("MACD", "布林带", "RSI")
DIR_VOTE_MIN = 2                      # 方向票同向门槛（同向张数下限）
RISK_VOTE_PENALTY = {"SuperTrend": -6}   # 辅助票逆向扣分（质量分，仅逆向时生效）
# 1H 结构门未过重罚（2026-10-15 拍板，docs/21 O9）：结构门由硬门降级为质量分
# 重罚（-15，量级≈两个 4h 逆向档）——不过门照常出信号，但排序显著落后，
# 让 AI/配额优先取结构合规信号
STRUCTURE_GATE_PENALTY = -15


def _compute_all(
    klines: list[list], htf_klines: Optional[list] = None,
    htf_bg: Optional[dict] = None,
) -> list[tuple]:
    """计算全部指标，返回 [(显示名, 短名, result|None)]；单指标异常降级为 None。

    htf_bg（2026-10-10）：调用方已算好的 4h 背景结果（如 scanner ranging 过滤
    步骤的产物），传入则直接复用不再重算；htf_klines 仅在 htf_bg 缺席时使用
    （旧路径兼容：AI 管线仍传原始 4h K 线）。"""
    results = []
    for display, short, fn in _INDICATORS:
        try:
            r = fn(klines)
        except Exception as e:
            logger.warning("指标 %s 计算失败（跳过）: %s", display, e)
            r = None
        results.append((display, short, r))
    if htf_bg is not None:
        results.append((HTF_DISPLAY, HTF_SHORT, htf_bg))
    elif htf_klines:
        try:
            htf = analyze_htf_background(htf_klines)
        except Exception as e:
            logger.warning("4h背景计算失败（跳过）: %s", e)
            htf = None
        results.append((HTF_DISPLAY, HTF_SHORT, htf))
    return results


def build_indicator_lines(klines: list[list]) -> list[str]:
    """逐指标渲染 "- 名称: 状态（detail）" 行（兼容旧调用，不含共振行）"""
    return [
        f"- {display}: {r['state_label']}（{r['detail']}）"
        for display, _, r in _compute_all(klines) if r
    ]


def _render_atr_line(
    klines: list[list], period: int = 14, rank_window: int = 100,
) -> str:
    """渲染 ATR 信息行（D1，docs/24）：- ATR(14)=值（占现价% · N根分位 p%）

    提示词纪律多处按 ATR 定规则（≤1×ATR 的位是争夺位、近锚资格止损宽度
    ≥0.3×ATR），注入数值与分位让 AI 有据可依；非方向票，仅波动率参考。
    分位=当前 ATR 在最近 rank_window 根有效 ATR 序列中按"小于当前值"计的
    百分位（与布林带宽分位同口径），序列不足 rank_window 根按实际根数算。
    数据不足/现价或 ATR 非正时返回空串（整行省略）。
    """
    try:
        atr = atr_series(klines, period)
        if atr is None:
            return ""
        valid = atr[~np.isnan(atr)]
        if len(valid) == 0:
            return ""
        cur = float(valid[-1])
        closed = klines[:-1] if len(klines) >= 2 else klines
        price = float(closed[-1][4])
        if not np.isfinite(cur) or cur <= 0 or price <= 0:
            return ""
        seq = valid[-rank_window:]
        rank = sum(1 for v in seq if v < cur) / len(seq) * 100
        pct = cur / price * 100
        return (
            f"- ATR({period})={cur:.4f}（{pct:.1f}% · "
            f"{len(seq)}根分位 {rank:.0f}%）"
        )
    except Exception as e:
        logger.warning("ATR 行渲染失败（跳过）: %s", e)
        return ""


def render_indicator_block(
    klines: list[list], htf_klines: Optional[list] = None,
    trigger_type: Optional[str] = None,
) -> str:
    """整块注入文本（含标题、4h 背景与方向票结论行，尾带换行）；无可用指标返回空串。

    P0/P1 两条管线共用：插在均线形态之后、摆动结构之前。
    结论行由程序统计（方向票多空张数），提示词按此行执行方向票规则。

    trigger_type（2026-10-21）：回踩确认单的方向由 4h 定向、1h 只决定入场时点，
    标题改为「方向=4h；1h 方向票=确认度」，避免 LLM 把 1h 方向票当否决项。
    """
    results = _compute_all(klines, htf_klines)
    lines = [
        f"- {display}: {r['state_label']}（{r['detail']}）"
        for display, _, r in results if r
    ]
    if not lines:
        return ""

    # ATR 行（D1，docs/24）：紧跟指标行追加，在"指标方向"结论行之前；
    # 数据不足整行省略，不影响其余指标与多数决统计
    atr_line = _render_atr_line(klines)
    if atr_line:
        lines.append(atr_line)

    # 方向票统计（2026-10-15 拍板，docs/21 O9）：仅 DIR_VOTE_SHORTS（MACD/
    # 布林带/RSI）计方向，其余指标（SuperTrend/ADX/OBV/4h背景）不再投方向票——
    # 结论行只承载数据（票数+来源指标），行为规则由系统提示词 _RULE_VOTE 承载
    bull = [s for _, s, r in results
            if s in DIR_VOTE_SHORTS and r and int(r.get("dir") or 0) > 0]
    bear = [s for _, s, r in results
            if s in DIR_VOTE_SHORTS and r and int(r.get("dir") or 0) < 0]
    nb, ns = len(bull), len(bear)
    if nb > ns:
        _verdict = "看多为主"
    elif ns > nb:
        _verdict = "看空为主"
    else:
        _verdict = "多空分歧"
    lines.append(
        f"- 方向票（{'/'.join(DIR_VOTE_SHORTS)}）: 看多{nb}项"
        + (f"（{'/'.join(bull)}）" if bull else "")
        + f" vs 看空{ns}项"
        + (f"（{'/'.join(bear)}）" if bear else "")
        + f" → {_verdict}；SuperTrend/ADX/OBV/4h背景不计方向票（仅强度/量价/风险提示）"
    )

    # 4h 状态机硬约束行（2026-10-11 再重构 EMA 状态机 11 态）：缠绕/拐头
    # 双向禁开行在前（凌驾锁逆势），宏观同侧态（宏观交叉/恢复/排列）锁逆势，
    # 过渡态（牛内回调/熊内反弹）不锁；程序侧另有 enforce_htf_constraint 兜底
    htf_r = next((r for _, s, r in results if s == HTF_SHORT and r), None)
    # 4h 单一权威（T3-2）：筛选与兜底都只放行 STATE_ALIGNED，注入行同口径
    if htf_r and htf_r.get("state") not in STATE_ALIGNED:
        _st = htf_r["state"]
        if _st == "tangle":
            _why = "缠绕无趋势"
        elif str(_st).startswith("turn_"):
            _why = "排列内拐头，趋势衰竭警戒"
        else:
            _why = "非多头/空头排列，4h 前置门不放行"
        lines.append(
            f"- 状态约束（硬性）: 4h{htf_r['state_label']}（{_why}）"
            " —— 双向禁开新仓，long/short 一律 skip（优先于一切信号）"
        )
    elif htf_r and htf_r.get("state") in STATE_LOCK_CONTRA:
        if htf_r.get("dir") == 1:
            lines.append(
                f"- 方向约束（硬性）: 4h{htf_r['state_label']}（宏观同侧）"
                " —— 仅可 long，开空一律 skip（优先于方向票，细则见系统提示词硬约束）"
            )
        else:
            lines.append(
                f"- 方向约束（硬性）: 4h{htf_r['state_label']}（宏观同侧）"
                " —— 仅可 short，开多一律 skip（优先于方向票，细则见系统提示词硬约束）"
            )

    # 4h 背景结构化行（2026-10-10 C7，docs/20）：state/macro 以机器
    # 可读键值对注入——detail 字符串会被 LLM 误读，关键字段不再只依赖散文
    # 渲染。tangle/turn 宏观对齐未评估（None），不输出 macro 键（None 渲染成
    # 「macro=逆侧」会向 LLM 注入错误事实，同 2026-10-11 ranging 修复口径）
    if htf_r:
        ma = htf_r.get("macro_aligned")
        if ma is None:
            lines.append(
                f"- 4h背景: state={htf_r['state']}（双向禁开，宏观对齐未评估）"
            )
        else:
            lines.append(
                f"- 4h背景: state={htf_r['state']}"
                f" macro={'同侧' if ma else '逆侧'}"
            )

    # 盈亏比取舍提示行已随 trail RR 豁免一并退役（2026-10-11 策略一 ladder-only）；
    # 2026-10-12 策略二/trail 整体删除后：盈亏比不足一律如实 skip，无第三条路

    # 标题按触发类型区分（2026-10-21）：回踩确认单是 4h 定向、1h 定时点，
    # 1h 方向票只表确认度
    if trigger_type == "trigger_pullback_confirm":
        _title = "技术指标（方向=4h；1h 方向票=确认度）:\n"
    else:
        _title = "技术指标（方向=方向票多数决）:\n"
    return _title + "\n".join(lines) + "\n"


def compute_indicator_votes(
    klines: list[list], htf_klines: Optional[list] = None,
    htf_bg: Optional[dict] = None,
) -> Optional[dict]:
    """方向票数统计——扫描筛选与 AI 注入同一把尺子（指标驱动管线核心）。

    htf_bg：预计算的 4h 背景结果（scanner ranging 过滤产物），复用免重算；
    与 htf_klines 二选一（htf_bg 优先），两者皆缺 → 4h背景行缺失。

    2026-10-15 拍板（docs/21 O9）：仅 DIR_VOTE_SHORTS（MACD/布林带/RSI）投方向票，
    其余指标（SuperTrend/ADX/OBV/4h背景）退出方向统计——net 恒为方向票净差，
    符号=候选方向（scanner 与 _hit_direction 消费）。

    返回 None = 无任何可用指标；否则：
    {bull: [多票短名], bear: [空票短名], bull_n, bear_n, net, voted,
     results: [(短名, 指标结果dict)]}
    voted = 有数据的「方向票」张数（≤len(DIR_VOTE_SHORTS)）；results 仍含全部指标
    供 AI 注入/质量分使用。
    """
    results = [
        (short, r) for _, short, r in _compute_all(klines, htf_klines, htf_bg) if r
    ]
    if not results:
        return None
    dir_votes = [(s, r) for s, r in results if s in DIR_VOTE_SHORTS]
    bull = [s for s, r in dir_votes if int(r.get("dir") or 0) > 0]
    bear = [s for s, r in dir_votes if int(r.get("dir") or 0) < 0]
    return {
        "bull": bull, "bear": bear,
        "bull_n": len(bull), "bear_n": len(bear),
        "net": len(bull) - len(bear),
        "voted": len(dir_votes),
        "results": results,
    }


def pullback_stabilize_bonus(
    klines: list[list], direction: int, touch_idx: int = -3,
    volume_semantics: bool = True,
) -> Optional[dict]:
    """回踩企稳加分项（docs/19 §3.3 条件2/3，2026-10-10 拍板：加分不拦截）。

    条件2 量能语义——触碰根量比 = 触碰根量 ÷ 其前 20 根中位量（volume_ratio
    同口径：中位数抗量堆根极值，比外部设计的 MA20 更稳）：
    - 量比 >1.5 → -5：放量回踩 = 抛压重，是放量下跌而非健康回踩；
    - 量比 <0.8 → +3：缩量回踩（策略 A「量能配合：回调缩量」，阈值我们自定）；
    - 其间 → 0。当根确认时触碰根即金K根，其量能已由放量加分（≥1.2 +5）评估，
      语义冲突，调用方传 volume_semantics=False 跳过本项。

    条件3 MACD 零轴状态（macd_zero_state，动量维度独立于均线排列；state 与
    dir 绑定——above 仅多头 / below 仅空头 / pullback_hold 双向）：
    - dir 与信号同向：pullback_hold 回踩/反抽零轴不破 +5（健康回踩、动量保持，
      最强企稳证据）；above/below 零轴同侧 +4（动量确认）；
    - 反向或 mixed 零轴胶着 → -4（动量不确认）。

    touch_idx：触碰根下标（次根确认 -3 / 触碰当根 pending 快照 -2 / 当根确认
    -2）。数据不足的项静默跳过；klines 为空返回 None。
    返回 {"mod": 合计, "touch_vol_ratio", "macd_zero", "items": [名目]}。
    """
    if not klines:
        return None
    mod, items = 0, []
    ev: dict = {"mod": 0, "touch_vol_ratio": None, "macd_zero": None, "items": items}

    # 条件2：触碰根量比（触碰根前 20 根中位量作分母）；数据异常只降级本项
    if volume_semantics:
        try:
            closed = klines[:-1] if len(klines) >= 2 else klines
            ti = len(closed) + touch_idx
            if 20 <= ti < len(closed):
                med = median(float(k[5]) for k in closed[ti - 20:ti])
                if med > 0:
                    vr = float(closed[ti][5]) / med
                    ev["touch_vol_ratio"] = round(vr, 2)
                    if vr > 1.5:
                        mod -= 5
                        items.append("放量回踩-5")
                    elif vr < 0.8:
                        mod += 3
                        items.append("缩量回踩+3")
        except (ValueError, TypeError, IndexError, ZeroDivisionError):
            pass  # 量能数据异常只降级该修饰项，不影响其余证据

    # 条件3：MACD 零轴状态（原始 K 线传入，函数内部自剔尾）。
    # state 与 dir 绑定（macd_zero_state）：above→dir=1 / below→dir=-1 /
    # pullback_hold 双向皆有（回踩不破=1 / 反抽不破=-1）→ dir==direction 即同向
    try:
        macd = macd_zero_state(klines)
    except Exception:
        macd = None
    if macd and macd.get("state"):
        ev["macd_zero"] = macd["state"]
        if int(macd.get("dir") or 0) == direction:
            if macd["state"] == "pullback_hold":
                mod += 5
                items.append("回踩零轴不破+5" if direction == 1 else "反抽零轴不破+5")
            else:  # above（多头）/ below（空头）：零轴同侧 = 动量确认
                mod += 4
                items.append("MACD零轴上方+4" if direction == 1 else "MACD零轴下方+4")
        else:
            mod -= 4
            items.append("MACD动量不确认-4")

    ev["mod"] = mod
    return ev


def golden_zone_swing_count(
    klines: list, golden_combo: bool = False,
    swing_order: int = 3, lookback: int = 200,
) -> int:
    """实体与金K价格带重叠的摆动高低点数（2026-10-13-6 拍板，-7 改实体重叠口径）。

    价格带 = 金K组合自身的 [min低, max高]——单个金K取信号根 klines[-2]，
    组合金K（golden_combo=True）再并入前根 klines[-3] 的包络。回看近 lookback
    根 1h 已收盘，统计「实体（open~close 区间）与该带重叠」的结构摆动高点 + 低点
    （find_swing_points）个数——不再要求极值点本身落带内，而是该摆动K线的实体
    与带相交即计（长影线探带、极值在外但实体压带内均算）。带内点越多 = 金K一口
    吞掉越多结构转折点、决断力越强。样本不足返回 0。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < 2 * swing_order + 1:
        return 0
    band = closed[-1]
    hi, lo = float(band[2]), float(band[3])
    if golden_combo and len(closed) >= 2:
        prev = closed[-2]
        hi = max(hi, float(prev[2]))
        lo = min(lo, float(prev[3]))
    window = closed[-lookback:]
    highs = np.array([float(k[2]) for k in window], dtype=float)
    lows = np.array([float(k[3]) for k in window], dtype=float)
    body_lo = np.array([min(float(k[1]), float(k[4])) for k in window], dtype=float)
    body_hi = np.array([max(float(k[1]), float(k[4])) for k in window], dtype=float)
    hi_idx, lo_idx = find_swing_points(highs, lows, swing_order)
    cnt = sum(1 for i in hi_idx if body_lo[i] <= hi and body_hi[i] >= lo)
    cnt += sum(1 for i in lo_idx if body_lo[i] <= hi and body_hi[i] >= lo)
    return cnt


def compute_quality_score(
    direction: int, votes: dict, trigger_type: str,
    klines: Optional[list] = None,
    stabilize: Optional[dict] = None,
    pattern: Optional[str] = None,
    golden_combo: bool = False,
    swing_order: int = 3,
    structure_gate: bool = True,
    range_penalty: bool = False,
    parts_out: Optional[dict] = None,
) -> int:
    """候选质量分（0-100）：方向一致性为主，修饰项只影响排序不改变方向。

    direction: 候选方向（1 多 / -1 空，= net 的符号）
    trigger_type: trigger_breakout / trigger_trend_breakout / trigger_pullback_confirm
    klines: 可选，放量加分与看涨吞没上影加分需要
    stabilize: 可选，回踩企稳加分项（pullback_stabilize_bonus 产物，docs/19
    §3.3 条件2/3：缩量回踩 +3 / 放量回踩 -5 / MACD 零轴同向 +4~+5 / 动量
    不确认 -4；仅回踩类触发传入，加分不拦截）
    pattern: 可选，金K形态名（trigger 输出的 pattern 字段，回踩金K确认路径）；
    看涨吞没上影短加分项据此生效（上影长已不拦形态）
    structure_gate: 1H 结构门结果（trigger 的 structure_gate 证据；仅 ema21 触点
    可能为 False，其余触点豁免恒 True）。2026-10-15 拍板：结构门由硬门降级为
    质量分重罚（STRUCTURE_GATE_PENALTY）——不过门照常出信号，仅排序落后
    range_penalty: range_score≥1 但触发类型为回踩确认（scanner 降权不拒），-8
    parts_out（2026-10-21，T0-3）：传入 dict 时填充分项贡献（base/htf/adx/volume/
    shadow/rsi/obv/risk_vote/boll/zone/stabilize/structure_gate/range），供提示词
    与复盘展示「质量分 = 基础 + 各项」；不改变返回值与既有调用方

    修饰规则（2026-09-17 设计评审定稿；2026-09-24 4h 背景提权；2026-10-11
    再重构 EMA 状态机 11 态：div 强度档与过热/冷却分值矩阵退役，改
    STATE_QUALITY 状态分矩阵；衰竭期分值同日早前已删；2026-10-15 指标拆角色：
    基础分改按「方向票净差」net×10（net∈{1,2,3}→基础 60/70/80），SuperTrend
    逆向改风险扣分、ADX/OBV 退出方向票）：
    - 方向票基础：50 + direction × net × 10（net = MACD/布林带/RSI 净差）
    - 辅助票逆向扣分：SuperTrend 方向与候选相反 −6（趋势跟踪滞后，仅风险提示，
      同向/中性不加分）
    - 4h背景按状态机状态：同向 排列+10 / 宏观交叉+12（趋势级事件独享最高档，
      原 extreme 语义被吸收）/ 恢复初期+8 / 过渡态+5；逆向 同态 -12/-14/-10/-6
    - 拐头 turn_* 同向 -8 / 逆向 -4（双向禁开后仅扫描侧排序兜底）；
      tangle dir=0 不进此分支
    - 逆宏观（bull_dip/bear_rally 过渡态，macro_aligned=False）额外 -4
      （硬约束不锁，分数兜底）；排列/交叉态恒同侧、tangle/turn 未评估，
      本罚天然只作用于过渡态
    - 突破触发且 ADX≥25 +5（强趋势中的突破可信）
    - 放量加分：量比 ≥1.2 +5（2026-09-25 金K类；2026-09-29 起突破放量不再
      是触发硬门，三类触发统一为加分项；缩量不加不减，仅排序自然落后）
    - 决断K线影线梯度加分 +5/+3/+1（2026-10-16 拍板：三处硬门全删、统一梯度，
      candlestick.DECISION_SHADOW_BONUS_GRADES）：吞没第二根（看涨看上影/看跌看
      下影）、启明星·黄昏星第三根（总影线），影线比 ≤10% +5、≤20% +3、≤30% +1、
      >30% 0——长影=拉锯犹豫的语义降为排序信号，只失加分、不再拒判形态
    - 金K组合价格带结构点密度 +3×n、封顶 +12（2026-10-13-6 拍板，-7 改实体重叠
      口径）：金K确认（单根/组合）的 [min低, max高] 价格带与近 200 根 1h 摆动
      高低点K线的实体重叠个数越多加分越高——实体（open~close）与带相交即计，
      不再要求极值点本身落带内（点越多=金K一口吞掉越多结构转折点、决断力越强）；
      仅回踩金K确认候选（pattern 存在）适用，突破/无金K upsert 不加分
    - RSI>75 做多 / RSI<25 做空 -8（追高杀低）
    - OBV 顶背离做多 / 底背离做空 -6（量价背离）
    - 突破触发且布林带宽分位 ≤10 -5（带宽收窄的突破易假）
    - 1H 结构门未过（structure_gate=False）−STRUCTURE_GATE_PENALTY
    """
    # votes=None（2026-10-18 方向票关关闭后，指标全不可用且 4h 闸门兜底定向的
    # 罕见路径可达）：按无票处理——空票集、net=0、基础分 50，各修饰项自动跳过
    by_short = dict(votes["results"]) if votes else {}
    parts: Optional[dict] = {} if parts_out is not None else None

    def _add(key: str, delta: int) -> None:
        """记分并同步分项（parts 为 None 时零开销，保持既有调用方行为）"""
        nonlocal score
        score += delta
        if parts is not None:
            parts[key] = parts.get(key, 0) + int(delta)

    if trigger_type == "trigger_pullback_confirm":
        # 回踩（2026-10-21 方向职责拆分）：1h 动量逆势是回撤形态本身（越深越反向），
        # 用「同向票数」作基础分、反向票不扣分——否则深回撤候选恒被压到 20 分，
        # 排序垫底后被 AI 配额挤掉，筛选层的放行等于白做。
        same_n = (votes["bull_n"] if direction == 1 else votes["bear_n"]) if votes else 0
        score = 50 + same_n * 10  # 0/1/2/3 → 50/60/70/80，与原刻度一致
    else:
        score = 50 + direction * (votes["net"] if votes else 0) * 10  # net∈{1,2,3} → 60/70/80
    if parts is not None:
        parts["base"] = int(score)

    htf = by_short.get(HTF_SHORT)
    if htf and int(htf.get("dir") or 0):
        same = int(htf["dir"]) == direction
        # 状态分矩阵（2026-10-11 再重构）：查 STATE_QUALITY（同向加分, 逆向减分）；
        # 退役旧档（weak/medium/strong/extreme/exhausted/overheat/cooling 的
        # 存量行）与未知状态走中性兜底 ±5/−8；tangle dir=0 不进此分支
        st = htf.get("state") or htf.get("phase")
        same_mod, counter_mod = STATE_QUALITY.get(st, _STATE_LEGACY_NEUTRAL)
        _add("htf", same_mod if same else counter_mod)
        if htf.get("macro_aligned") is False:
            _add("htf_macro", -4)  # 逆宏观：牛内回调做空/熊内反弹做多一律压分

    adx = by_short.get("ADX")
    if (trigger_type in ("trigger_breakout", "trigger_trend_breakout")
            and adx and float(adx.get("adx") or 0) >= 25):
        _add("adx", 5)

    # 放量加分（三类触发统一，2026-09-29）：量比 ≥1.2 +5；缩量/平量不加不减。
    # 阈值与 BREAKOUT_VOL_RATIO 同源（1.2）
    if trigger_type in ("trigger_breakout", "trigger_trend_breakout",
                        "trigger_pullback_confirm") and klines:
        try:
            if volume_ratio(klines) >= 1.2:
                _add("volume", 5)
        except (ValueError, TypeError, IndexError, ZeroDivisionError):
            pass  # 量能数据异常只降级该修饰项，不影响基础分

    # 决断K线影线梯度加分（2026-10-16 拍板：三处硬门全删 → 多档加分、不否定形态）：
    # 口径与档位单一事实源 = candlestick.DECISION_SHADOW_BONUS_GRADES /
    # decision_shadow_bonus（看涨吞没上影 / 看跌吞没下影 / 星第三根总影线，
    # ≤10% +5、≤20% +3、≤30% +1、>30% 0；pattern 为 None 时返回 0）
    if klines:
        _add("shadow", decision_shadow_bonus(pattern, klines))

    rsi = by_short.get("RSI")
    if rsi:
        v = float(rsi.get("rsi") or 50)
        if (direction == 1 and v > 75) or (direction == -1 and v < 25):
            _add("rsi", -8)

    obv = by_short.get("OBV")
    if obv:
        st = obv.get("state")
        if (direction == 1 and st == "bear_div") or (direction == -1 and st == "bull_div"):
            _add("obv", -6)

    # 辅助票逆向扣分（2026-10-15 拍板，docs/21 O9）：SuperTrend 等滞后指标退出
    # 方向票，仅当方向与候选相反时作风险扣分（同向/中性不加分）
    for _short, _pen in RISK_VOTE_PENALTY.items():
        _r = by_short.get(_short)
        if _r and int(_r.get("dir") or 0) and int(_r["dir"]) != direction:
            _add("risk_vote", _pen)

    boll = by_short.get("布林带")
    if trigger_type == "trigger_breakout" and boll:
        rank = boll.get("bw_rank")
        if rank is not None and float(rank) <= 10:
            _add("boll", -5)

    # 金K组合价格带结构点密度加分（2026-10-13-6 拍板，-7 改实体重叠口径）：金K确认
    # （单根/组合）的 [min低, max高] 价格带与近 200 根已收盘 1h 摆动高低点K线的
    # 实体（open~close）重叠的个数越多 → 金K一口吞掉越多结构转折点、决断力越强。
    # 每点 +3、封顶 +12（只加分不拦截）。仅回踩金K确认候选（pattern 存在）；
    # 突破触发 / 无金K 的 upsert 不适用。
    if trigger_type == "trigger_pullback_confirm" and pattern and klines:
        try:
            n_pts = golden_zone_swing_count(klines, golden_combo,
                                            swing_order=swing_order)
            if n_pts > 0:
                _add("zone", min(n_pts * 3, 12))
        except (ValueError, TypeError, IndexError, ZeroDivisionError):
            pass  # 摆动点计算异常只降级该修饰项，不影响基础分

    # 回踩企稳加分项（docs/19 §3.3 条件2/3，2026-10-10 拍板）：缩量/放量回踩
    # 与 MACD 零轴动量证据，加分不拦截，仅回踩类触发由调用方传入
    if stabilize:
        _add("stabilize", int(stabilize.get("mod") or 0))

    # 1H 结构门重罚（2026-10-15 拍板，docs/21 O9）：门由硬门降级为质量分重罚，
    # 不过门照常出信号、仅排序落后（仅 ema21 触点可能为 False）
    if not structure_gate:
        _add("structure_gate", STRUCTURE_GATE_PENALTY)

    # 震荡闸门降权（2026-10-21）：回踩命中 range_score 不再否决，改质量分 -8；
    # 突破类仍由 scanner 硬拒，不会带 range_penalty 走到这里
    if range_penalty:
        _add("range", -8)

    # 4h 强度分（2026-10-21 T2-6，影子）：aligned 只说明排列，不说明趋势强弱与
    # 剩余空间。按 spec F3 三要素给分，但**只记录不参与排序**——先影子观察
    # 「强弱 aligned 分差」与配额命中率，样本 ≥30 笔后再决定是否并入正式分。
    strength = 0
    if htf and int(htf.get("dir") or 0) == direction:
        _adx4 = htf.get("adx")
        if _adx4 is not None:
            strength += 6 if float(_adx4) >= 30 else (3 if float(_adx4) >= 20 else 0)
        _pct = htf.get("slope_pctile")
        if _pct is not None:
            strength += 5 if float(_pct) >= 0.70 else (3 if float(_pct) >= 0.30 else 0)
        _dist = htf.get("price_vs_ema144_atr")
        if _dist is not None:
            strength += 3 if float(_dist) <= 1.5 else (-4 if float(_dist) > 3 else 0)
    if parts is not None:
        parts["htf_strength_shadow"] = int(strength)

    final = max(0, min(100, int(round(score))))
    if parts is not None:
        parts["total"] = int(final)
        parts_out.clear()
        parts_out.update(parts)
    return final
