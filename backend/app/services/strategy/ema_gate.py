"""EMA 趋势门 + 震荡评分卡（docs/19 §2/§3）。

2026-10-10 融合拍板：影子层退役并入原生触发层（trend_pullback 升级标注）；
同日二次拍板：三件套提为筛选链硬门（scanner.detect_indicator_candidate，
一票否决语义，借鉴外部设计 check_4h_gate / check_1h_structure / calc_range_score）：
2026-10-20 合并拍板（用户）：gate_4h 与 4h 状态机合并为 technical.htf_context
单入口——原 gate_4h 三重否决（走平/缠绕/ADX）拆解为「走平=独立拒因 flat、
ADX=缠绕第一道、缠绕=detect_tangle 统一判据」，本模块不再持有 4h 闸门。
- structure_1h：1H 三线排列 + 快中线发散度 + EMA144 方向结构门（2026-10-11
  拍板：中慢线发散度退役为观测注记，长期趋势健康度改由「144 走平或反向
  拒绝」判定），输出结构证据或 None；
- range_score：震荡闸门 4 项合取判据（任一命中即不入场）。2026-10-20 重构：
  原 8 项评分卡「≥3」经方向感知 PNL 回放证明无区分力（放行池净期望 −0.02R，
  与拦下池差异仅靠费率噪声），改为 PNL 口径下唯一 train/test 双段为正的
  4 项合取门。

K 线口径：structure_1h 由调用方传已剔尾序列（scanner/trigger 均传 klines[:-1]）；
range_score 收原始 1h 序列（内部自剔尾，ADX 项同口径）。
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np

# SLOPE_BARS/SLOPE_FLAT_PCT 已于 2026-10-20 随 4h 闸门合并迁入 technical，
# 此处从 technical 再导出（trigger 1h 结构门与 4h 闸门共用同一斜率窗口口径）
from app.services.strategy.technical import (
    SLOPE_BARS, SLOPE_FLAT_PCT, analyze_adx,
)

# ── 震荡闸门常量（2026-10-20 重构：4 项合取，≥RANGE_SCORE_REJECT 不入场）────
# 依据：range_score_pnl_replay / range_score_ablation_replay（60 币 × 60 天，
# 方向感知近锚止损 PNL 口径）。原 8 项评分卡「≥3」的放行池净期望 −0.02R，
# 且 threshold 敏感性 Δ 仅 −0.001（无区分力）；项 1 在链路中恒 0（死项），
# 项 3/7/8 滞后，项 6（布林带收口）方向相反（命中反而更好，squeeze 是突破
# 前奏）。V3 = {三线排列, 55/144 分离史, ADX} + 新增「过度扩张」四项合取，
# 是唯一在全样本（+0.10R）、train（+0.02R）、test（+0.26R）同时为正的变体。
RANGE_SCORE_REJECT = 1     # 4 项合取：任一命中即判震荡不入场
ADX_WARN_MIN = 18.0        # 1h ADX 下限（趋势强度必备）
MS_DIV_MIN = 1.0           # 55/144 近 N 根最大分离度下限（ATR 归一）
EXP_PCTL = 0.80            # 波动过度扩张分位阈值（Donchian / BBW 任一命中）
EXP_HISTORY = 100          # 分位回看根数
EXP_DONCH_N = 20           # Donchian 通道宽度窗口
EXP_BB_N = 20              # 布林带窗口（带宽 = 4σ/中轨）
# 55/144 分离史回看根数（2026-10-14，与 EmaGateConfig.div_lookback 同口径拍板）：
# 回踩的定义就是均线现值被压窄，用现值打分会把「深回踩」误记成「震荡粘合」；
# 「这波趋势曾分离过」即放行，整段无分离史的真震荡照样被拦
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
    # 分离史回看根数（2026-10-10 三次拍板）：快中腿按近 N 根已收盘的最大值
    # 判定——深回踩（触 EMA55/144）会把 EMA21/55 间距压缩到不达标，触点时刻
    # 现值误杀深回踩；「这波趋势曾分离过」即可，三线黏着的假趋势整段无分离
    # 史照样被杀（反震荡能力不丢）。半窗口出厂默认，参数定标待实盘证据（docs/19 #15）
    div_lookback: int = 12


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


def _pctl_last(vals: np.ndarray) -> Optional[float]:
    """当前值在窗口内的分位（0=最小，1=最大；并列按 ≤ 计，与回放同口径）。"""
    if len(vals) < 20:
        return None
    cur = float(vals[-1])
    if not np.isfinite(cur):
        return None
    return float((vals <= cur).mean())


def _donch_width_pctl(h: np.ndarray, low: np.ndarray,
                      closes: np.ndarray) -> Optional[float]:
    """Donchian(EXP_DONCH_N) 通道宽度（按收盘价归一）在当前值上的历史分位。"""
    if len(closes) < EXP_DONCH_N + EXP_HISTORY - 1:
        return None
    w = np.empty(EXP_HISTORY)
    for j in range(EXP_HISTORY):
        end = len(closes) - EXP_HISTORY + j + 1
        w[j] = (float(h[end - EXP_DONCH_N:end].max())
                - float(low[end - EXP_DONCH_N:end].min())) / float(closes[end - 1])
    return _pctl_last(w)


def _bbw_width_pctl(closes: np.ndarray) -> Optional[float]:
    """布林带宽（4σ/中轨，EXP_BB_N 根）在当前值上的历史分位。"""
    if len(closes) < EXP_BB_N + EXP_HISTORY - 1:
        return None
    w = np.empty(EXP_HISTORY)
    for j in range(EXP_HISTORY):
        end = len(closes) - EXP_HISTORY + j + 1
        seg = closes[end - EXP_BB_N:end]
        mid = float(seg.mean())
        w[j] = 4.0 * float(seg.std(ddof=0)) / mid if mid > 0 else 0.0
    return _pctl_last(w)


def range_score(klines: list[list],
                gate_ev: Optional[dict] = None) -> tuple[int, list[str]]:
    """震荡闸门：4 项合取判据，任一命中即判震荡不入场（≥RANGE_SCORE_REJECT）。

    2026-10-20 重构（原 8 项评分卡「≥3」退役）——判据全部来自方向感知的近锚
    止损 PNL 回放（60 币 × 60 天），而非拍脑袋的「多指标共振」：

    - 三线交错：1h EMA21/55/144 排列错误 = 结构混乱（PNL Δ −0.14）；
    - 55/144 分离史：近 RANGE_DIV_LOOKBACK 根最大分离度 < MS_DIV_MIN×ATR，
      整段无分离史的才是真震荡（回踩压窄现值不算，2026-10-14 同源修复）；
    - ADX 过低：1h ADX < ADX_WARN_MIN = 趋势强度不足（PNL Δ −0.21，最强单项）；
    - 波动过度扩张：Donchian / BBW 宽度分位 ≥ EXP_PCTL = 追在趋势末端
      （最高分位档 netR −0.19/−0.16）。

    被删除的原项及其依据：项 1（4H EMA144 走平）在链路中位于 4h 闸门之后，
    恒为 0 的死项；项 3（21/55 距离）与三项共线，重复计数；项 6（布林带收口）
    方向相反——squeeze 是突破前奏，命中反而更好（Δ +0.03），且是最高频触发
    项；项 7/8（反复穿越 / 量能低迷）滞后于 PNL 标签。

    klines：1h 原始K线（尾根未收盘，内部取已收盘）；gate_ev 保留入参仅为
    调用方兼容，当前判据不再消费 4h 证据。返回 (score, reasons)。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = len(closed)
    score, reasons = 0, []

    if n < 200:  # EMA144 种子 + 余量（与 structure_1h 同门槛），数据不足不判
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

    # 1. 1H 三线交错——排列错误 = 结构混乱
    if not (d21 > d55 > d144 or d21 < d55 < d144):
        score += 1
        reasons.append("1H三线交错")

    # 2. 中慢线分离史过近（ATR 归一化跨品种统一阈值）
    if atr > 0:
        lb = min(RANGE_DIV_LOOKBACK, n - 143)  # e144 有限区自 index 143 起
        ms_div = float(np.max(np.abs(e55[-lb:] - e144[-lb:]))) / atr
        if ms_div < MS_DIV_MIN:
            score += 1
            reasons.append("55/144距离过近")

    # 3. 1H ADX 预警——已低于 18 说明趋势明显不足
    try:
        adx_info = analyze_adx(klines, period=14)
    except Exception:
        adx_info = None
    if adx_info and float(adx_info["adx"]) < ADX_WARN_MIN:
        score += 1
        reasons.append("1H ADX过低")

    # 4. 波动过度扩张——Donchian / 布林带宽分位进极值 = 追在趋势末端
    dp = _donch_width_pctl(h, low, closes)
    bp = _bbw_width_pctl(closes)
    if (dp is not None and dp >= EXP_PCTL) or (bp is not None and bp >= EXP_PCTL):
        score += 1
        reasons.append("波动过度扩张")

    return score, reasons
