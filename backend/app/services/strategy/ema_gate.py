"""EMA 趋势门 + 震荡评分卡（docs/19 §2/§3）。

2026-10-10 融合拍板：影子层退役并入原生触发层（trend_pullback 升级标注）；
同日二次拍板：三件套提为筛选链硬门（scanner.detect_indicator_candidate，
一票否决语义，借鉴外部设计 check_4h_gate / check_1h_structure / calc_range_score）：
- gate_4h：4H EMA144 趋势闸门（三重否决——斜率走平 / 缠绕震荡 / ADX 弱），
  输出方向 long/short 或 None；
- structure_1h：1H 三线排列 + 快中线发散度 + EMA144 方向结构门（2026-10-11
  拍板：中慢线发散度退役为观测注记，长期趋势健康度改由「144 走平或反向
  拒绝」判定），输出结构证据或 None；
- range_score：震荡评分卡 8 项独立评分，≥3 判震荡不入场。

K 线口径：gate_4h 收原始 4h 序列（尾根未收盘，内部剔除，analyze_adx 同口径）；
structure_1h 由调用方传已剔尾序列（scanner/trigger 均传 klines[:-1]）；
range_score 收原始 1h 序列（内部自剔尾，项 5 analyze_adx 同口径）。
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.services.strategy.technical import analyze_adx, htf_core

# EMA144 斜率回看根数（4h 闸门与 1h 结构门 144 方向共用窗口）与走平阈值
# （20 根变化幅度/价格 < 0.5% → 走平）
SLOPE_BARS = 20
SLOPE_FLAT_PCT = 0.005
# 缠绕判定：近 10 根收盘价穿越 EMA144 次数 ≥3 → 震荡拒绝
WRAP_WINDOW = 10
WRAP_MAX = 2

# ── 震荡评分卡常量（docs/19 §3，≥RANGE_SCORE_REJECT 分不入场）──────────────
RANGE_SCORE_REJECT = 3
ADX_WARN_MIN = 18.0        # 项5：1h ADX 预警下限（比闸门 20 略低，作"预警"档）
BBW_HISTORY = 100          # 项6：带宽分位的回看根数
BBW_PCTL = 30.0            # 项6：当前带宽低于历史 30 分位 → 波动率压缩
BBW_MIN_HIST = 20          # 项6：历史样本下限（不足跳过该项，不给分）
CROSS_WINDOW = 20          # 项7：价格穿越 EMA21 的回看根数
CROSS_MAX = 4              # 项7：穿越 ≥4 次 → 震荡最直观的表现
VOL_CV_MIN = 0.3           # 项8：近 20 根成交量变异系数下限（<0.3 → 无规律低迷）
# 项3/4 分离史回看根数（2026-10-14，与 EmaGateConfig.div_lookback 同口径拍板）：
# 快中/中慢腿距离改取近 N 根已收盘的最大分离度——回踩的定义就是均线现值被压窄，
# 用现值打分会把「深回踩」误记成「震荡粘合」；「这波趋势曾分离过」即不给震荡分，
# 整段无分离史的真震荡照样计分（与 structure_1h 2026-10-10 三次拍板同源修复）
RANGE_DIV_LOOKBACK = 12


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


@dataclass
class EmaGateConfig:
    """方向门参数（触发层固定口径，与 docs/19 出厂值一致）。"""
    div_fast_mid: float = 0.5       # 1h |EMA21-EMA55|/ATR 下限（分离史口径）
    # div_mid_slow 已退役（2026-10-11 拍板）：|EMA55-EMA144|/ATR≥1.0 硬门撤销，
    # 长期趋势健康度改判「排列内 EMA144 走平或沿信号方向」，ms 分离度降级为
    # 证据观测注记（复盘定标留档）；55/144 距离过近仍以 +1 分参与震荡评分卡
    adx_min: float = 20.0           # 4h ADX 趋势闸门下限
    # 分离史回看根数（2026-10-10 三次拍板）：快中腿按近 N 根已收盘的最大值
    # 判定——深回踩（触 EMA55/144）会把 EMA21/55 间距压缩到不达标，触点时刻
    # 现值误杀深回踩；「这波趋势曾分离过」即可，三线黏着的假趋势整段无分离
    # 史照样被杀（反震荡能力不丢）。半窗口出厂默认，参数定标待实盘证据（docs/19 #15）
    div_lookback: int = 12


def gate_4h(k4: list[list], cfg: Optional[EmaGateConfig] = None,
            core: Optional[dict] = None) -> Optional[tuple[str, dict]]:
    """4H EMA144 趋势闸门：三重否决（走平/缠绕/ADX）全过才给方向。

    k4：原始 4h K线（尾根未收盘，内部统一剔除——与 analyze_adx 的自剔尾
    口径一致，避免对已剔除序列再剔一次导致 ADX 慢一拍）。core 为可选的 4h
    共享计算核心（technical.htf_core）——传入则复用其 EMA144/ADX/收盘价，
    免去本函数与 analyze_htf_background 双算，缺省时内部自算（向后兼容）。
    返回 (direction "long"/"short", 证据 dict) 或 None（闸门不过）。
    证据 direction 语义为 "up"/"down"（4h K 线趋势方向，观测/前端展示口径）。
    """
    cfg = cfg or EmaGateConfig()
    core = core or htf_core(k4)
    if core is None:
        return None
    closed = core["closed"]
    # 闸门额外数据门：EMA144 斜率回看（-SLOPE_BARS）需更长历史，htf_core 的
    # 155 根下限不够；各调用方固定拉 200 根=199 已收盘，此处保留 200 根口径
    if len(closed) < 200:
        return None
    c = core["closes"]
    e144 = core["e144"]
    if not np.isfinite(e144[-1]) or not np.isfinite(e144[-SLOPE_BARS]):
        return None
    price = core["price"]
    slope_pct = abs(float(e144[-1]) - float(e144[-SLOPE_BARS])) / price

    # 缠绕：近 WRAP_WINDOW 根收盘价在 EMA144 两侧的穿越次数
    wraps = 0
    for i in range(len(closed) - WRAP_WINDOW, len(closed)):
        side_now = c[i] - e144[i]
        side_prev = c[i - 1] - e144[i - 1]
        if side_now * side_prev < 0:
            wraps += 1

    adx_val = core["adx"]  # htf_core 内已按原始序列自剔尾算 ADX，口径一致

    ev = {"slope_pct": round(slope_pct, 5), "wraps": wraps,
          "adx": round(adx_val, 1) if adx_val is not None else None,
          "close_vs_ema144": round(price / float(e144[-1]) - 1, 5)}

    if slope_pct < SLOPE_FLAT_PCT:
        return None  # 走平
    if wraps > WRAP_MAX:
        return None  # 缠绕
    if adx_val is None or adx_val < cfg.adx_min:
        return None  # 无趋势

    up = price > e144[-1] and e144[-1] >= e144[-SLOPE_BARS]
    down = price < e144[-1] and e144[-1] <= e144[-SLOPE_BARS]
    if up:
        return "long", {**ev, "direction": "up"}
    if down:
        return "short", {**ev, "direction": "down"}
    return None


def structure_1h(k1: list[list], direction: str,
                 cfg: Optional[EmaGateConfig] = None) -> Optional[tuple[dict, dict]]:
    """1H 结构门：三线排列 + 快中线发散度 + EMA144 方向（direction="long"/"short"）。

    2026-10-10 三次拍板（分离史口径）：排列 aligned 仍在最后一根硬查（「趋势
    没坏」的结构声明，与回踩深度无关）；快中腿发散度按近 div_lookback 根已
    收盘的**带方向**最大值判定——深回踩把 EMA21/55 间距压缩是回踩的定义而非
    缺陷，触点时刻现值会误杀 EMA55 深回踩；「沿信号方向曾分离过」即可
    （与旧现值口径同号，反方向残差不计数），假趋势整段无分离史照样否决。

    2026-10-11 拍板：中慢线发散度（|EMA55-EMA144|/ATR≥1.0）硬门退役——早期
    趋势（55/144 刚拉开途中）被误杀面过大；长期趋势健康度改判「做多要求
    EMA144 走平或向上（近 SLOPE_BARS 根变化 ≥0），做空镜像」。「21/55 在 144
    同侧」由排列蕴含（21>55>144 ⇒ 双双在 144 上方）不重复判定；连价路径下
    排列完好而 144 严格反向是准矛盾态（价格中枢须压在 144 下方而 EMA21 均值
    又在 144 上方，共存窗口 ≤ 数根），该条件为语义声明 + 排列放松时的安全网。
    ms 分离度保留在证据中作观测注记（复盘定标留档，不再判定）。

    返回 ({d21,d55,d144,atr}, 结构证据 dict) 或 None（结构不过）。
    """
    cfg = cfg or EmaGateConfig()
    if len(k1) < 200:
        return None
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
    else:
        aligned = d21 < d55 < d144
    if not aligned:
        return None

    # EMA144 方向（2026-10-11 拍板）：做多要求走平或向上（近 SLOPE_BARS 根
    # 变化 ≥0，含走平），做空镜像。e144 有限区自 index 143 起，len≥200 时
    # -SLOPE_BARS 落点必有限
    slope144 = float(e144[-1]) - float(e144[-SLOPE_BARS])
    if direction == "long" and slope144 < 0:
        return None
    if direction == "short" and slope144 > 0:
        return None

    # 分离史：快中腿取近 div_lookback 根已收盘的带方向最大分离度（正语义 =
    # 沿信号方向曾分离，与旧现值口径同号；反方向残差如 e55×e144 刚翻多的空头
    # 排列历史不计数）。窗口须整体落在 EMA 有限区——ema_series 前 period-1 根
    # 为 NaN，e144 有限区自 index 143 起，lb 截断到 len-143；NaN 兜底
    # fail-closed（np.max 传 NaN 会让 <阈值 判 False 静默放行，必须挡住）。
    # ms 腿同窗口计算仅作观测注记（2026-10-11 退役判定，复盘对比留档）
    lb = max(int(cfg.div_lookback), 1)
    lb = min(lb, len(c) - 143)
    if lb < 1:
        return None
    if direction == "long":
        fm_seq = (e21[-lb:] - e55[-lb:]) / atr
        ms_seq = (e55[-lb:] - e144[-lb:]) / atr
        fm_now, ms_now = d21 - d55, d55 - d144
    else:
        fm_seq = (e55[-lb:] - e21[-lb:]) / atr
        ms_seq = (e144[-lb:] - e55[-lb:]) / atr
        fm_now, ms_now = d55 - d21, d144 - d55
    div_fm = float(fm_seq.max())
    div_ms = float(ms_seq.max())
    if not (np.isfinite(div_fm) and np.isfinite(div_ms)):
        return None
    ev = {"aligned": aligned,
          "div_fast_mid": round(div_fm, 3), "div_mid_slow": round(div_ms, 3),
          # 现值快照（审计口径）：复盘「深回踩压间距」占比与 lookback 定标用
          "div_fast_mid_now": round(float(fm_now / atr), 3),
          "div_mid_slow_now": round(float(ms_now / atr), 3),
          "slope144_pct": round(slope144 / float(c[-1]), 5),
          "div_lookback": lb, "atr": round(atr, 8)}
    if div_fm < cfg.div_fast_mid:
        return None
    return {"d21": d21, "d55": d55, "d144": d144, "atr": atr}, ev


def range_score(klines: list[list],
                gate_ev: Optional[dict] = None) -> tuple[int, list[str]]:
    """震荡评分卡：8 项独立评分，累计 ≥RANGE_SCORE_REJECT 判震荡不入场。

    设计思想（docs/19 §3）：单一指标可能失效，多个指标共振时判断更可靠；
    每项独立评分，得分越高震荡概率越大——评分制比硬条件更灵活。

    项 1（4H EMA144 走平）复用 gate_4h 斜率证据不重算，筛选链中位于闸门之后
    故恒为 0（standalone 使用时才可能命中）；项 2-4 与 structure_1h 同源（项 3/4
    于 2026-10-14 改分离史口径——近 RANGE_DIV_LOOKBACK 根最大分离度，回踩压窄
    均线现值不再误记震荡粘合），结构门通过时亦恒为 0——评分卡在筛选链的实际
    杀伤力来自项 5-8（1h ADX 预警 / 布林带收口 / 价格反复穿越 EMA21 / 成交量
    低迷），即闸门与结构门漏掉的残余震荡，≥3 即强震荡共识。

    klines：1h 原始K线（尾根未收盘，内部取已收盘）；gate_ev：gate_4h 证据
    （可 None，缺失跳过项 1）。返回 (score, reasons)。"""
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    score, reasons = 0, []

    # 1. 4H EMA144 走平——最硬的条件，若走平直接高分
    if gate_ev is not None and float(gate_ev.get("slope_pct") or 0) < SLOPE_FLAT_PCT:
        score += 1
        reasons.append("4H EMA144走平")

    if n < 200:  # EMA144 种子 + 余量（与 structure_1h 同门槛），不足不给分
        return score, reasons
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    e21 = ema_series(closes, 21)
    e55 = ema_series(closes, 55)
    e144 = ema_series(closes, 144)
    if any(e is None or not np.isfinite(e[-1]) for e in (e21, e55, e144)):
        return score, reasons
    d21, d55, d144 = float(e21[-1]), float(e55[-1]), float(e144[-1])

    # ATR14（与 structure_1h 同口径：TR 均值滚动）
    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    tr = np.maximum(h[1:] - low[1:],
                    np.maximum(np.abs(h[1:] - closes[:-1]), np.abs(low[1:] - closes[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0

    # 2. 1H 三线交错——排列错误 = 结构混乱
    if not (d21 > d55 > d144 or d21 < d55 < d144):
        score += 1
        reasons.append("1H三线交错")

    # 3/4. 快中/中慢线距离过近（ATR 归一化跨品种统一阈值：0.5 是"粘合"与
    # "发散"的分界线，中慢线要求更高因代表更长期趋势）。
    # 2026-10-14 分离史口径（与 structure_1h div_lookback 同源）：取近
    # RANGE_DIV_LOOKBACK 根已收盘的最大分离度——回踩时刻现值被压窄是回踩的
    # 定义而非震荡证据，曾分离过即不给分；整段无分离史的真震荡照样计分。
    if atr > 0:
        lb = min(RANGE_DIV_LOOKBACK, n - 143)  # e144 有限区自 index 143 起
        fm_div = float(np.max(np.abs(e21[-lb:] - e55[-lb:]))) / atr
        ms_div = float(np.max(np.abs(e55[-lb:] - e144[-lb:]))) / atr
        if fm_div < 0.5:
            score += 1
            reasons.append("21/55距离过近")
        if ms_div < 1.0:
            score += 1
            reasons.append("55/144距离过近")

    # 5. 1H ADX 预警——已低于 18 说明趋势明显不足
    try:
        adx_info = analyze_adx(klines, period=14)
    except Exception:
        adx_info = None
    if adx_info and float(adx_info["adx"]) < ADX_WARN_MIN:
        score += 1
        reasons.append("1H ADX过低")

    # 6. 布林带收口：当前带宽 < 过去 BBW_HISTORY 根的 BBW_PCTL 分位 = 波动率
    #    压缩，往往伴随震荡（带宽 = 4σ/中轨，布林带 (upper-lower)/mid 同构）
    if n >= 20 + BBW_MIN_HIST + 1:
        csum = np.cumsum(np.insert(closes, 0, 0.0))
        csum2 = np.cumsum(np.insert(closes ** 2, 0, 0.0))
        idx = np.arange(19, n)  # 每个位置 i 的 20 根窗口 [i-19, i]
        mid = (csum[idx + 1] - csum[idx + 1 - 20]) / 20
        var = (csum2[idx + 1] - csum2[idx + 1 - 20]) / 20 - mid ** 2
        bw = 4.0 * np.sqrt(np.maximum(var, 0.0)) / np.maximum(mid, 1e-12)
        hist = bw[max(0, len(bw) - 1 - BBW_HISTORY): len(bw) - 1]
        if len(hist) >= BBW_MIN_HIST and np.isfinite(bw[-1]) \
                and float(bw[-1]) < float(np.percentile(hist, BBW_PCTL)):
            score += 1
            reasons.append("布林带收口")

    # 7. 价格反复穿越 EMA21——近 CROSS_WINDOW 根穿越 ≥CROSS_MAX 次
    crosses = 0
    for i in range(max(1, n - CROSS_WINDOW), n):
        if (closes[i - 1] - float(e21[i - 1])) * (closes[i] - float(e21[i])) < 0:
            crosses += 1
    if crosses >= CROSS_MAX:
        score += 1
        reasons.append("价格反复穿越EMA21")

    # 8. 成交量低迷——变异系数（std/mean）低于阈值 = 无规律；趋势行情中量应
    #    有明显的放大与萎缩
    vols = np.array([float(k[5]) for k in closed[-CROSS_WINDOW:]], dtype=float)
    vmean = float(vols.mean()) if len(vols) else 0.0
    if vmean > 0 and float(vols.std(ddof=0)) / vmean < VOL_CV_MIN:
        score += 1
        reasons.append("成交量低迷")

    return score, reasons
