# -*- coding: utf-8 -*-
"""震荡评分卡（ema_gate.range_score）诊断回放（2026-10-20）。

目的：先量化 range_score 的失败模式，再谈优化（用户已定：诊断先行、方向偏「重构判据」）。

背景：4h 前置门已收紧为「仅排列放行」（725fa0c），故到达 range_score 的候选已是
4h 排列明确；评分卡的职责收窄为「4h 排列但 1h 仍震荡」。项 1（4h EMA144 走平）
在链路中恒为 0（位于闸门之后）——诊断聚焦 1h 层面。

三项诊断（全部方向无关，因 range_score 是「震荡探测器」而非方向信号）：
  A. 分档单调性：score=0/1/2/3/4+ 各档的前向「趋势度」ER（越接近 0 越震荡）。
     若 3+ 档并不比 0 档更震荡 → 阈值/项本身有问题；若单调，看拐点是否在 3。
  B. 项级归因：逐项命中/未命中子样本的 forward ER。命中却后续「更趋势」= 误拦源
     （预计慢项 2/3/4/5）；命中且后续更震荡 = 有效项。
  C. 候选特征前瞻：各候选度量（ER / 方差比 VR / Donchian 宽度分位 / BBW 分位 /
     ADX）与 forward ER 的秩相关（负相关 = 好的震荡信号），为「重构判据」提供依据。

滞后（D）：score≥3 的连续段长度分布；以及「forward 趋势起点」前若干根内 score 是否
已在 ≥3（量化「拦得久 / 复位滞后」）。

标签口径（方向无关）：forward ER(k) = |c[t+k]-c[t]| / Σ|c[i]-c[i-1]|（i=t+1..t+k），
k=24/48；ER→0 为纯震荡，ER→1 为单边趋势。trend_flag = fwd_er24 ≥ TREND_ER。

局限：①方向无关标签，未含近锚止损 PNL（那需漏斗方向，属下一步）；②宇宙取当前
24h 额 top（幸存者偏差，与既有 replay 脚本同）；③ER 阈值 0.5 为经验值，脚本同时
报 0.4/0.6 敏感性。

用法（cwd=backend）：
    python scripts/range_score_replay.py --symbols 8 --days 30     # 冒烟
    python scripts/range_score_replay.py --symbols 60 --days 60    # 全量
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))              # 兄弟脚本

from gate_soft_replay import (                                          # noqa: E402
    FWD_BARS, HOUR_MS, WINDOW, fetch_klines, top_universe,
)
from app.services.strategy.ema_gate import (                            # noqa: E402
    RANGE_SCORE_REJECT, ema_series, range_score,
)
from app.services.strategy.technical import analyze_adx                 # noqa: E402

TREND_ER = 0.5          # forward ER ≥ 此值视为「趋势」（脚本同时报 0.4/0.6）
FWD_ER_K = (24, 48)     # forward 趋势度窗口（小时）
FEAT_K = (24, 48)       # 特征 ER 窗口
VR_K = 24               # 方差比窗口
DONCH_N = 20            # Donchian 窗口
PCTL_LOOKBACK = 100     # 分位回看根数
MIN_WARM = 200          # EMA144 种子下限（range_score 内部门槛）

ITEM_OF_REASON = {
    "4H EMA144走平": 1, "1H三线交错": 2, "21/55距离过近": 3,
    "55/144距离过近": 4, "1H ADX过低": 5, "布林带收口": 6,
    "价格反复穿越EMA21": 7, "成交量低迷": 8,
    "波动过度扩张": 9,   # 2026-10-20 重构后新增（原 8 项已退役 1/3/6/7/8）
}


def _ranks(a: np.ndarray) -> np.ndarray:
    """平均秩（并列取均值）"""
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=float)
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        r[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def _spearman(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    if len(x) < 30 or np.all(x == x[0]) or np.all(y == y[0]):
        return None
    rx, ry = _ranks(x), _ranks(y)
    rx, ry = rx - rx.mean(), ry - ry.mean()
    d = float(np.sqrt((rx ** 2).sum() * (ry ** 2).sum()))
    return float((rx * ry).sum() / d) if d > 0 else None


def _er(closes: np.ndarray, t: int, k: int, fwd: bool) -> Optional[float]:
    """效率比：|净位移| / 路径长度。fwd=False 用 [t-k,t]，True 用 [t,t+k]。"""
    lo, hi = (t, t + k) if fwd else (t - k, t)
    if lo < 0 or hi >= len(closes):
        return None
    seg = closes[lo:hi + 1]
    path = float(np.sum(np.abs(np.diff(seg))))
    return abs(float(seg[-1] - seg[0])) / path if path > 0 else None


def _vr(closes: np.ndarray, t: int, k: int) -> Optional[float]:
    """Lo-MacKinlay 方差比：var(k期收益)/(k·var(1期收益))。<1 均值回复(震荡)，>1 趋势。"""
    if t - k < 1:
        return None
    r1 = np.diff(closes[t - k:t + 1])
    rk = float(closes[t] - closes[t - k])
    v1 = float(np.var(r1, ddof=1)) if len(r1) > 1 else 0.0
    if v1 <= 0:
        return None
    return (rk * rk) / (k * v1)


def _donch_pctl(highs, lows, closes, t: int) -> Optional[float]:
    """当前 Donchian 宽度在近 PCTL_LOOKBACK 根的分位（0=最窄）"""
    if t - DONCH_N - PCTL_LOOKBACK + 1 < 0:
        return None
    widths = []
    for i in range(t - PCTL_LOOKBACK + 1, t + 1):
        widths.append((float(highs[i - DONCH_N + 1:i + 1].max())
                       - float(lows[i - DONCH_N + 1:i + 1].min())) / float(closes[i]))
    w = np.array(widths, dtype=float)
    return float((w <= w[-1]).mean())


def _bbw_pctl(closes: np.ndarray, t: int) -> Optional[float]:
    """布林带宽（4σ/mid）当前值在近 PCTL_LOOKBACK 根的分位"""
    if t - 20 - PCTL_LOOKBACK + 1 < 0:
        return None
    bw = []
    for i in range(t - PCTL_LOOKBACK + 1, t + 1):
        seg = closes[i - 19:i + 1]
        mid = float(seg.mean())
        bw.append(4.0 * float(seg.std(ddof=0)) / mid if mid > 0 else 0.0)
    w = np.array(bw, dtype=float)
    return float((w <= w[-1]).mean())


def snapshot_row(kl_all: list, t: int) -> Optional[dict]:
    """在已收盘 1h 根 t 上取：评分/项命中 + 特征 + forward 标签"""
    win = kl_all[max(0, t + 1 - WINDOW): t + 2]      # [-1]=当前未收盘根, [-2]=t
    if len(win) - 1 < MIN_WARM:
        return None
    score, reasons = range_score(win)
    items = {ITEM_OF_REASON[r] for r in reasons if r in ITEM_OF_REASON}

    closes = np.array([float(k[4]) for k in kl_all], dtype=float)
    highs = np.array([float(k[2]) for k in kl_all], dtype=float)
    lows = np.array([float(k[3]) for k in kl_all], dtype=float)
    n = len(closes)

    feats: dict = {}
    for k in FEAT_K:
        feats[f"er{k}"] = _er(closes, t, k, fwd=False)
    feats["vr24"] = _vr(closes, t, VR_K)
    feats["donch_pctl"] = _donch_pctl(highs, lows, closes, t)
    feats["bbw_pctl"] = _bbw_pctl(closes, t)
    adx = analyze_adx(kl_all[max(0, t + 1 - WINDOW): t + 2], period=14)
    feats["adx"] = float(adx["adx"]) if adx else None

    fwd = {}
    for k in FWD_ER_K:
        fwd[f"fwd_er{k}"] = _er(closes, t, k, fwd=True)
    if t + 24 < n:
        fwd["fwd_range24"] = (float(highs[t + 1:t + 25].max())
                              - float(lows[t + 1:t + 25].min())) / float(closes[t])
    else:
        fwd["fwd_range24"] = None

    return {"ts": kl_all[t][0], "score": score, "items": sorted(items),
            **{f"f_{a}": b for a, b in feats.items()},
            **{a: b for a, b in fwd.items()}}


def collect(client, sym: str, days: int) -> list[dict]:
    total = days * 24 + WINDOW + 40
    try:
        kl = fetch_klines(client, sym, "1h", total)
    except Exception as e:
        print(f"  {sym} 拉取失败: {str(e)[:60]}")
        return []
    if len(kl) < WINDOW + MIN_WARM + 30:
        return []
    rows = []
    hi = len(kl) - FWD_BARS - 2          # 需 forward 窗口
    for t in range(max(WINDOW - 1, MIN_WARM), hi):
        r = snapshot_row(kl, t)
        if r is not None:
            r["symbol"] = sym
            rows.append(r)
    return rows


def _bucket_stats(rows: list[dict], keyfn, label: str) -> None:
    print(f"\n── {label} ──")
    print(f"{'档':<12}{'n':>7}{'fwdER24均值':>13}{'P(ER24≥0.5)':>13}"
          f"{'P(ER24≥0.4)':>13}{'fwdRange24均值':>16}")
    for b in range(0, 6):
        sub = [r for r in rows if keyfn(r) == b]
        if not sub:
            continue
        er = [r["fwd_er24"] for r in sub if r["fwd_er24"] is not None]
        rng = [r["fwd_range24"] for r in sub if r["fwd_range24"] is not None]
        if not er:
            continue
        p5 = sum(1 for v in er if v >= 0.5) / len(er)
        p4 = sum(1 for v in er if v >= 0.4) / len(er)
        print(f"{b if b < 5 else '5+':<12}{len(sub):>7}{np.mean(er):>13.3f}"
              f"{p5:>13.1%}{p4:>13.1%}"
              f"{(np.mean(rng) * 100 if rng else float('nan')):>15.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-vol", type=float, default=3_000_000)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    import httpx
    t0 = time.time()
    with httpx.Client() as client:
        syms = top_universe(client, args.symbols, args.min_vol)
        print(f"宇宙 {len(syms)} 币 · 每币 {args.days} 天 1h")
        rows: list[dict] = []
        for i, s in enumerate(syms, 1):
            rows += collect(client, s, args.days)
            print(f"  [{i}/{len(syms)}] {s} 累计快照={len(rows)} "
                  f"({time.time() - t0:.0f}s)")
    if not rows:
        print("无样本")
        return

    print("\n" + "=" * 88)
    print(f"样本总数 = {len(rows)} 快照（{len(syms)} 币 × {args.days} 天）")

    # A. 分档单调性
    _bucket_stats(rows, lambda r: min(r["score"], 5), "A. 分档单调性（score 档 → forward 趋势度）")

    # A2. 阈值敏感性：拒绝集 vs 放行集的 forward ER
    print("\n── A2. 阈值敏感性（拒绝集的关键是「更震荡」= fwdER 更低）──")
    valid = [r for r in rows if r["fwd_er24"] is not None]
    for thr in (2, 3, 4):
        rej = [r["fwd_er24"] for r in valid if r["score"] >= thr]
        pas = [r["fwd_er24"] for r in valid if r["score"] < thr]
        if rej and pas:
            print(f"  ≥{thr} 拒：n={len(rej):<6} fwdER={np.mean(rej):.3f} "
                  f"| 放行：n={len(pas):<6} fwdER={np.mean(pas):.3f} "
                  f"| Δ={np.mean(rej) - np.mean(pas):+.3f}（负=拒得对）")

    # B. 项级归因
    print("\n── B. 项级归因（命中 vs 未命中的 forward ER；命中更趋势=误拦）──")
    print(f"{'项':<4}{'命中n':>8}{'命中ER':>10}{'未命中ER':>11}{'Δ(命中-未命中)':>16}  判读")
    for item in range(1, 10):
        hit = [r["fwd_er24"] for r in valid if item in r["items"]]
        no = [r["fwd_er24"] for r in valid if item not in r["items"]]
        if not hit or not no:
            print(f"{item:<4}{len(hit):>8}   样本不足")
            continue
        d = np.mean(hit) - np.mean(no)
        tag = "误拦嫌疑" if d > 0.05 else ("有效" if d < -0.05 else "中性")
        print(f"{item:<4}{len(hit):>8}{np.mean(hit):>10.3f}{np.mean(no):>11.3f}"
              f"{d:>+16.3f}  {tag}")

    # C. 候选特征前瞻（秩相关 vs forward ER）
    print("\n── C. 候选特征 vs forwardER24 秩相关（负=好的震荡信号）──")
    feats = ["er24", "er48", "vr24", "donch_pctl", "bbw_pctl", "adx"]
    print(f"{'特征':<14}{'Spearman':>10}{'n':>8}  说明")
    note = {"er24": "效率比(当下)", "er48": "效率比(48)", "vr24": "方差比<1均值回归",
            "donch_pctl": "通道宽度分位", "bbw_pctl": "布林带宽分位", "adx": "ADX"}
    fv = {}
    for f in feats:
        pair = [(r[f"f_{f}"], r["fwd_er24"]) for r in valid if r.get(f"f_{f}") is not None]
        if len(pair) < 30:
            print(f"{f:<14}{'-':>10}{len(pair):>8}")
            continue
        x = np.array([a for a, _ in pair], float)
        y = np.array([b for _, b in pair], float)
        rho = _spearman(x, y)
        fv[f] = rho
        print(f"{f:<14}{(rho if rho is not None else float('nan')):>10.3f}"
              f"{len(pair):>8}  {note.get(f, '')}")

    # D. 滞后：score≥3 连续段 + 趋势起点前 score 状态
    print("\n── D. 滞后 ──")
    allruns = []
    for s in syms:
        seq = [r for r in rows if r["symbol"] == s]
        cur = 0
        for r in seq:
            if r["score"] >= RANGE_SCORE_REJECT:
                cur += 1
            elif cur:
                allruns.append(cur)
                cur = 0
        if cur:
            allruns.append(cur)
    if allruns:
        a = np.array(allruns)
        print(f"  score≥{RANGE_SCORE_REJECT} 连续段：n={len(a)} 中位={np.median(a):.0f} 根 "
              f"p90={np.percentile(a, 90):.0f} 根 最长={a.max()} 根")
        print("    （长段=「拦得久」，趋势重启后慢项未复位仍拦）")
    if valid:
        prev_blocked = 0
        for s in syms:
            seq = [r for r in rows if r["symbol"] == s]
            for i in range(len(seq)):
                if seq[i]["fwd_er24"] is None or seq[i]["fwd_er24"] < TREND_ER:
                    continue
                if i >= 6 and all(seq[i - j]["score"] >= RANGE_SCORE_REJECT
                                  for j in range(1, 7)):
                    prev_blocked += 1
        tot_trend = sum(1 for r in valid if r["fwd_er24"] >= TREND_ER)
        if tot_trend:
            print(f"  趋势起点（fwdER24≥{TREND_ER}）共 {tot_trend} 个；"
                  f"其中起点前 6 根 score 全 ≥{RANGE_SCORE_REJECT} 的占 "
                  f"{prev_blocked / tot_trend:.1%}（=趋势启动前刚被拦，复位滞后）")

    summary = {"n_snapshots": len(rows), "n_symbols": len(syms), "days": args.days,
               "feature_spearman": fv, "trend_er": TREND_ER}
    if args.out:
        json.dump(summary, open(args.out, "w"), ensure_ascii=False, indent=2)
        print(f"\nsummary → {args.out}")


if __name__ == "__main__":
    main()
