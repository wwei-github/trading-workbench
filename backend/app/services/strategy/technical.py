"""技术指标包：MACD / 布林带 / RSI / SuperTrend / ADX / OBV / 4h 背景

AI 分析两条管线共用注入（P0 ai_analyzer._build_messages 与 P1 ai_agent._build_user_msg）：
方向按指标票数多数决（2026-09-17 起）：多空票数谁多以谁为主，不再强制"以 K 线为准"；
票数统计由程序完成，注入文本中的"指标方向"行直接给结论。

- 全部基于已收盘 K 线（剔除最后一根未收盘），与 ema.py 同口径
- 每个指标一个 analyze_* 函数：数据不足返回 None，返回 dict 含 state/state_label/detail，
  方向性指标额外带 dir（1 多 / -1 空 / 0 中性）供共振统计
- render_indicator_block 供两条管线一行接入；单指标异常只降级该行，不影响分析
- calc_ema 复用 ema.py（其不依赖 strategy 包内其他模块，无循环导入）
"""
import logging
from typing import Optional

import numpy as np

from app.services.strategy.ema import calc_ema
from app.services.strategy.indicators import volume_ratio

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
        "dir": 1 if state in ("golden_cross", "bullish")
        else -1 if state in ("death_cross", "bearish") else 0,
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


def analyze_htf_background(htf_klines: list[list], fast: int = 21, slow: int = 55) -> Optional[dict]:
    """4h 高周期背景：ADX(14) 门控 + EMA21/55 排列与间距分级（1h 分析的大方向参照）。

    htf_klines 为 4h 周期 K 线（最后一根未收盘，内部剔除）。
    - ADX<20：4h 震荡，EMA 排列不投方向票（治震荡市 EMA 反复交叉的误标）
    - ADX≥20 按 EMA 排列投方向，间距分级：≥1% 强趋势 / 0.1%~1% 弱趋势
      （刚金叉的小间距趋势可信度低，标签供 AI 区分背景强度）
    - ADX 计算失败静默降级为仅 EMA 判定（单指标失败不阻塞背景注入）
    """
    if not htf_klines or len(htf_klines) < slow + 12:
        return None
    closed = htf_klines[:-1] if len(htf_klines) >= 2 else htf_klines
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    if len(closes) < slow + 10:
        return None
    f = calc_ema(closes, fast)
    s = calc_ema(closes, slow)
    if f is None or s is None:
        return None
    fn, sn = float(f[-1]), float(s[-1])
    if sn <= 0 or not np.isfinite(fn):
        return None
    price = float(closes[-1])

    # ADX 门控：analyze_adx 内部同样剔除未收盘根，口径一致；失败降级不门控
    adx_res = analyze_adx(htf_klines)
    adx_v = float(adx_res["adx"]) if adx_res else None
    if adx_v is not None and adx_v < 20:
        return {
            "state": "ranging", "state_label": "震荡（无趋势，不作方向参考）", "dir": 0,
            "detail": f"ADX={adx_v:.1f}(<20), EMA{fast}/{slow}排列仅记录不投票",
        }

    dist_pct = abs(fn - sn) / sn * 100
    if dist_pct < 0.1:
        state, label, d = "mixed", "均线纠缠", 0
    elif fn > sn:
        pos = "价格在EMA21上方" if price > fn else "价格回落至EMA21下方"
        grade = "strong" if dist_pct >= 1.0 else "weak"
        state, label, d = "bullish", f"{'强' if grade == 'strong' else '弱'}多头背景（{pos}）", 1
    else:
        pos = "价格在EMA21下方" if price < fn else "价格反弹至EMA21上方"
        grade = "strong" if dist_pct >= 1.0 else "weak"
        state, label, d = "bearish", f"{'强' if grade == 'strong' else '弱'}空头背景（{pos}）", -1

    detail = f"EMA{fast}={fn:.6g}, EMA{slow}={sn:.6g}, 间距{dist_pct:.2f}%"
    if adx_v is not None:
        detail += f", ADX={adx_v:.1f}"
    return {
        "state": state, "state_label": label, "dir": d, "detail": detail,
        # 趋势强度分级（仅方向性背景有值）：质量分加权与 AI 方向硬约束都读它
        "grade": grade if state in ("bullish", "bearish") else None,
    }


def enforce_htf_constraint(ai_result: dict, htf_klines: Optional[list]) -> dict:
    """4h 强背景方向硬约束的程序化兜底（原地修改并返回 ai_result）。

    强多头背景下 AI 返回开空 / 强空头背景下返回开多 → 强制改 skip
    （LLM 可能无视 prompt 注入的约束行，开单前程序把关）。无 4h 数据、
    非强趋势、非 suggest 或方向合规时原样返回。
    """
    try:
        if not htf_klines or ai_result.get("trade_decision") != "suggest":
            return ai_result
        htf = analyze_htf_background(htf_klines)
        if not htf or htf.get("grade") != "strong":
            return ai_result
        direction = (ai_result.get("direction") or "").lower()
        if htf["state"] == "bullish" and direction == "short":
            ai_result["trade_decision"] = "skip"
            ai_result["skip_reason"] = (
                "方向约束：4h强多头背景禁止开空（程序强制 skip）"
            )
        elif htf["state"] == "bearish" and direction == "long":
            ai_result["trade_decision"] = "skip"
            ai_result["skip_reason"] = (
                "方向约束：4h强空头背景禁止开多（程序强制 skip）"
            )
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
HTF_DISPLAY, HTF_SHORT = "4h背景(EMA21/55)", "4h背景"


def _compute_all(klines: list[list], htf_klines: Optional[list] = None) -> list[tuple]:
    """计算全部指标，返回 [(显示名, 短名, result|None)]；单指标异常降级为 None"""
    results = []
    for display, short, fn in _INDICATORS:
        try:
            r = fn(klines)
        except Exception as e:
            logger.warning("指标 %s 计算失败（跳过）: %s", display, e)
            r = None
        results.append((display, short, r))
    if htf_klines:
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


def render_indicator_block(klines: list[list], htf_klines: Optional[list] = None) -> str:
    """整块注入文本（含标题、4h 背景与多数决结论行，尾带换行）；无可用指标返回空串。

    P0/P1 两条管线共用：插在均线形态之后、摆动结构之前。
    结论行由程序统计（方向指标多空票数），提示词按此行执行多数决规则。
    """
    results = _compute_all(klines, htf_klines)
    lines = [
        f"- {display}: {r['state_label']}（{r['detail']}）"
        for display, _, r in results if r
    ]
    if not lines:
        return ""

    # 多数决统计（布林带按中轨上下投票）
    bull = [s for _, s, r in results if r and int(r.get("dir") or 0) > 0]
    bear = [s for _, s, r in results if r and int(r.get("dir") or 0) < 0]
    nb, ns = len(bull), len(bear)
    if nb > ns:
        lines.append(
            f"- 指标方向: 看多{nb}项（{'/'.join(bull)}） vs 看空{ns}项"
            f" —— 看多为主，方向判断以看多指标为依据"
        )
    elif ns > nb:
        lines.append(
            f"- 指标方向: 看多{nb}项 vs 看空{ns}项（{'/'.join(bear)}）"
            f" —— 看空为主，方向判断以看空指标为依据"
        )
    else:
        lines.append(
            f"- 指标方向: 多空平衡（看多{nb}项 vs 看空{ns}项）"
            f" —— 无多数方向，自行结合K线结构与量能判断"
        )

    # 4h 强背景方向硬约束（2026-09-24）：强多只开多 / 强空只开空，
    # 优先级高于指标多数决；程序侧另有 enforce_htf_constraint 兜底
    htf_r = next((r for _, s, r in results if s == HTF_SHORT and r), None)
    if htf_r and htf_r.get("grade") == "strong":
        if htf_r["state"] == "bullish":
            lines.append(
                "- 方向约束（硬性）: 4h强多头背景 —— 开仓方向仅可 long，"
                "开空一律 skip；此约束优先于指标多数决"
            )
        else:
            lines.append(
                "- 方向约束（硬性）: 4h强空头背景 —— 开仓方向仅可 short，"
                "开多一律 skip；此约束优先于指标多数决"
            )

    return "技术指标（方向=多空票数多数决，以票数多的方向为主）:\n" + "\n".join(lines) + "\n"


def compute_indicator_votes(
    klines: list[list], htf_klines: Optional[list] = None,
) -> Optional[dict]:
    """指标方向票数统计——扫描筛选与 AI 注入同一把尺子（指标驱动管线核心）。

    返回 None = 无任何可用指标；否则：
    {bull: [多票短名], bear: [空票短名], bull_n, bear_n, net, voted,
     results: [(短名, 指标结果dict)]}
    """
    results = [(short, r) for _, short, r in _compute_all(klines, htf_klines) if r]
    if not results:
        return None
    bull = [s for s, r in results if int(r.get("dir") or 0) > 0]
    bear = [s for s, r in results if int(r.get("dir") or 0) < 0]
    return {
        "bull": bull, "bear": bear,
        "bull_n": len(bull), "bear_n": len(bear),
        "net": len(bull) - len(bear),
        "voted": len(results),
        "results": results,
    }


def compute_quality_score(
    direction: int, votes: dict, trigger_type: str,
    klines: Optional[list] = None,
) -> int:
    """候选质量分（0-100）：方向一致性为主，修饰项只影响排序不改变方向。

    direction: 候选方向（1 多 / -1 空，= net 的符号）
    trigger_type: trigger_breakout / trigger_trend_breakout / trigger_pullback_confirm
    klines: 可选，金K类形态触发的量能加分需要（突破类量能已是触发门槛不重复加）
    修饰规则（2026-09-17 设计评审定稿；2026-09-24 强 4h 背景提权）：
    - 4h背景同向：强 +10 / 弱 +5；反向：强 -12 / 弱 -8
      （强 4h 趋势币在质量分排序与 AI 配额中前置；逆强背景降权更狠，
      与「强多只开多/强空只开空」硬约束呼应）
    - 突破触发且 ADX≥25 +5（强趋势中的突破可信）
    - 金K类形态触发（同向形态/回踩金K确认）且量比 ≥1.2 +5（放量金K，2026-09-25；
      缩量形态不加不减，仅在 AI 配额排序中自然落后）
    - RSI>75 做多 / RSI<25 做空 -8（追高杀低）
    - OBV 顶背离做多 / 底背离做空 -6（量价背离）
    - 突破触发且布林带宽分位 ≤10 -5（带宽收窄的突破易假）
    """
    by_short = dict(votes["results"])
    score = 50 + direction * votes["net"] * 7  # net≥3 → 基础 ≥71

    htf = by_short.get(HTF_SHORT)
    if htf and int(htf.get("dir") or 0):
        same = int(htf["dir"]) == direction
        strong = htf.get("grade") == "strong"
        if same:
            score += 10 if strong else 5
        else:
            score -= 12 if strong else 8

    adx = by_short.get("ADX")
    if (trigger_type in ("trigger_breakout", "trigger_trend_breakout")
            and adx and float(adx.get("adx") or 0) >= 25):
        score += 5

    # 金K类形态触发的量能加分（2026-09-25）：放量金K +5；缩量/平量不加不减。
    # 突破类不加——量比 ≥1.2 已是触发门槛，全体通过者无区分度
    if trigger_type == "trigger_pullback_confirm" and klines:
        try:
            if volume_ratio(klines) >= 1.2:
                score += 5
        except (ValueError, TypeError, IndexError, ZeroDivisionError):
            pass  # 量能数据异常只降级该修饰项，不影响基础分

    rsi = by_short.get("RSI")
    if rsi:
        v = float(rsi.get("rsi") or 50)
        if (direction == 1 and v > 75) or (direction == -1 and v < 25):
            score -= 8

    obv = by_short.get("OBV")
    if obv:
        st = obv.get("state")
        if (direction == 1 and st == "bear_div") or (direction == -1 and st == "bull_div"):
            score -= 6

    boll = by_short.get("布林带")
    if trigger_type == "trigger_breakout" and boll:
        rank = boll.get("bw_rank")
        if rank is not None and float(rank) <= 10:
            score -= 5

    return max(0, min(100, int(round(score))))
