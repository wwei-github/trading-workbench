"""技术指标包（辅助参考）：MACD / 布林带 / RSI / SuperTrend

AI 分析两条管线共用注入（P0 ai_analyzer._build_messages 与 P1 ai_agent._build_user_msg）：
K 线与量能为主、指标为辅——只给状态标签 + 关键数值，不替代结构判断。

- 全部基于已收盘 K 线（剔除最后一根未收盘），与 ema.py 同口径
- 每个指标一个 analyze_* 函数：数据不足返回 None，返回 dict 含 state/state_label/detail
- render_indicator_block 供两条管线一行接入；单指标异常只降级该行，不影响分析
- calc_ema 复用 ema.py（其不依赖 strategy 包内其他模块，无循环导入）
"""
import logging
from typing import Optional

import numpy as np

from app.services.strategy.ema import calc_ema

logger = logging.getLogger(__name__)


def _closed_closes(klines: list[list]) -> np.ndarray:
    """已收盘 K 线的收盘价数组（剔除最后一根未收盘）"""
    closed = klines[:-1] if len(klines) >= 2 else klines
    return np.array([float(k[4]) for k in closed], dtype=float)


def analyze_macd(
    klines: list[list], fast: int = 12, slow: int = 26, signal: int = 9,
) -> Optional[dict]:
    """MACD(fast,slow,signal)：DIF=EMA快-EMA慢，DEA=DIF 的 EMA，柱=DIF-DEA（国际口径）。

    状态优先级：3 根内金叉/死叉 > 零轴同侧动能（多头/空头）> 多空胶着。
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
    hist = dif - dea

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
        "detail": ", ".join(parts),
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

    # 带宽近 60 根滚动分位（数据足够才算）
    rank_desc = ""
    if len(closes) >= period + 60:
        bws = []
        for i in range(len(closes) - 60, len(closes)):
            w = closes[i - period + 1: i + 1]
            m = float(w.mean())
            if m > 0:
                bws.append(float(w.std(ddof=0)) * 2 * ndev / m * 100)
        if bws:
            rank = sum(1 for b in bws if b < bw) / len(bws) * 100
            zone = "收窄" if rank <= 25 else "张开" if rank >= 80 else "常态"
            rank_desc = f", 带宽近60根分位={rank:.0f}%, {zone}"

    return {
        "upper": upper, "mid": mid, "lower": lower, "bandwidth": bw,
        "state": state, "state_label": label,
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
        "detail": f"RSI={v:.1f}, {trend}",
    }


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

    # Wilder ATR
    tr = np.empty(n)
    tr[0] = h[0] - low[0]
    tr[1:] = np.maximum(
        h[1:] - low[1:], np.maximum(abs(h[1:] - c[:-1]), abs(low[1:] - c[:-1])),
    )
    atr = np.full(n, np.nan)
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

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
        "detail": ", ".join(parts),
    }


# 注入顺序与参数标注（顺序即渲染顺序）
_INDICATORS = (
    ("MACD(12,26,9)", analyze_macd),
    ("布林带(20,2)", analyze_bollinger),
    ("RSI(14)", analyze_rsi),
    ("SuperTrend(10,3)", analyze_supertrend),
)


def build_indicator_lines(klines: list[list]) -> list[str]:
    """逐指标渲染 "- 名称: 状态（detail）" 行；数据不足/计算异常的指标降级跳过"""
    lines = []
    for name, fn in _INDICATORS:
        try:
            r = fn(klines)
        except Exception as e:
            logger.warning("指标 %s 计算失败（跳过）: %s", name, e)
            continue
        if r:
            lines.append(f"- {name}: {r['state_label']}（{r['detail']}）")
    return lines


def render_indicator_block(klines: list[list]) -> str:
    """整块注入文本（含标题行，尾带换行）；无可用指标返回空串。

    P0/P1 两条管线共用：插在均线形态之后、摆动结构之前。
    """
    lines = build_indicator_lines(klines)
    if not lines:
        return ""
    return "技术指标（辅助参考，判断以K线结构与量能为主）:\n" + "\n".join(lines) + "\n"
