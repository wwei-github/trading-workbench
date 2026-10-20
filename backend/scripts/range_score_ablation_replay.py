# -*- coding: utf-8 -*-
"""震荡评分卡「判据消融 A/B」回放（2026-10-20）。

v2（range_score_pnl_replay.py）已证明：以方向感知近锚止损 PNL 为标签，现有
8 项评分卡在 thr=3 上是**有效**的（拦组 netR −0.23 vs 放组 −0.02，train/test
双侧 Δ≈−0.2）。但同时暴露三处结构问题：
  - 项 1（4H EMA144 走平）在链路中恒 0 —— 死项，白占分数预算；
  - 项 6（布林带收口）Δ=+0.03（命中反而更好）—— 方向相反，却以最高频
    （867 次）把大量单推过阈值，是最大结构缺陷（squeeze→expansion 是突破前奏）；
  - 项 3/2/4/5 同属「1h 均线/ADX 状态」族，高度共线 —— 「8 项共振」实为
    对同一信息的重复计数。
本脚本量化「砍项/换项」的收益，为重构判据定选型。

## 关键方法学：拒绝率对齐
单纯删项会降低分数、放松阈值，与「阈值调低」混淆。故所有变体都在**同一份
反事实候选集 S** 上按统一 thr 扫描，并单独给「等拒绝率（≈baseline）」对比表
——只有拒绝率对齐，Δ 才可归因于判据质量本身。

## 两阶段
Phase 1（本脚本主流程）：单次 pass 关掉硬门得到 S（pending 演化固定为「无门」
世界），逐候选记录全部变体得分 → 离线扫 variant×thr。快，用于定方向。
Phase 2（--confirm 指定变体）：按变体真实重跑漏斗（pending 演化随之改变），
确认 Phase 1 的赢家在真实状态机下不翻转。

## 变体
  V0_baseline   {1,2,3,4,5,6,7,8}  生产现状（自检锚：应与 v2 的「放 rs<3」一致）
  V1_no_dead    {2,3,4,5,7,8}      去死项 1 + 反向项 6
  V2_core_245   {2,4,5}            压缩为 PNL 口径下最有效的三项
  V3_core_245_x {2,4,5}+X          V2 + 新增「过度扩张」惩罚项
                                   X：Donchian 宽度分位 ≥0.8 或 BBW 分位 ≥0.8
                                   （v2 C 段：最高分位档 netR −0.19/−0.16）
  V4_adx_only   {5}                单 ADX 闸（v2 最强单项 Δ−0.209）

判读：同一 thr 下，accept 组 netR 越高越好；且看 train/test 是否同向（防过拟合）。

用法（cwd=backend）：
    python scripts/range_score_ablation_replay.py --symbols 8 --days 30     # 冒烟
    python scripts/range_score_ablation_replay.py --symbols 60 --days 60    # 全量
    python scripts/range_score_ablation_replay.py --confirm V1_no_dead,V3_core_245_x
"""
import argparse
import sys
import time
from bisect import bisect_left
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))        # 兄弟脚本

from gate_soft_replay import (                                  # noqa: E402
    CONFIRM_TYPES, HOUR_MS, WINDOW, _fwd_outcome, _min_same,
    fetch_klines, top_universe, wilson_lb,
)
from range_score_replay import _bbw_pctl, _donch_pctl         # noqa: E402
from app.config import settings                                 # noqa: E402
from app.services.scan_filters import resolve_scan_filters      # noqa: E402
from app.services.strategy import ema_gate as eg                # noqa: E402
from app.services.strategy import technical as tech             # noqa: E402
from app.services.strategy import trigger as trig               # noqa: E402
from app.services.strategy.technical import analyze_adx, SLOPE_FLAT_PCT  # noqa: E402
from app.services.strategy.ema_gate import (                    # noqa: E402
    ADX_WARN_MIN, RANGE_DIV_LOOKBACK,
)

# 2026-10-20 生产 range_score 已重构为 4 项合取门，原 8 项评分卡的常量随之下线。
# 本脚本是「重构成因」的历史复现，故在此钉住旧常量——不要改这里去跟随生产。
BBW_HISTORY = 100          # 旧项 6：带宽分位回看根数
BBW_PCTL = 30.0            # 旧项 6：低于历史 30 分位 → 波动率压缩
BBW_MIN_HIST = 20          # 旧项 6：历史样本下限
CROSS_WINDOW = 20          # 旧项 7：穿越 EMA21 回看根数
CROSS_MAX = 4              # 旧项 7：穿越次数上限
VOL_CV_MIN = 0.3           # 旧项 8：量能变异系数下限
BASELINE_THR = 3           # 旧 RANGE_SCORE_REJECT

TEST_DAYS = 20
MIN_N = 30
EXP_PCTL = 0.80            # 「过度扩张」分位阈值（v2 C 段最高档 ≈0.83）

VARIANTS = {
    "V0_baseline":   {1, 2, 3, 4, 5, 6, 7, 8},
    "V1_no_dead":    {2, 3, 4, 5, 7, 8},
    "V2_core_245":   {2, 4, 5},
    "V3_core_245_x": {2, 4, 5, "X"},
    "V4_adx_only":   {5},
}


# ── 成分提取（与 ema_gate.range_score 逐行同义，但只算一次供多变体复用）──────

def _ingredients(kl_all: list, t: int, gate_ev: Optional[dict]) -> dict:
    win = kl_all[max(0, t + 1 - WINDOW): t + 2]
    closed = win[:-1] if len(win) >= 2 else win
    n = len(closed)
    ing: dict = {"n": n, "item1": False, "aligned": None, "atr": 0.0,
                 "fm_div": None, "ms_div": None, "adx": None, "item6": False,
                 "crosses": 0, "item8": False,
                 "donch_pctl": None, "bbw_pctl": None}
    # 项 1：4H EMA144 走平（链路中 gate_ev 在位 → 恒不命中，此处照实计算）
    if gate_ev is not None and float(gate_ev.get("slope_pct") or 0) < SLOPE_FLAT_PCT:
        ing["item1"] = True
    if n < 200:                      # 与生产同门槛：不足则项 2-8 全不评
        return ing
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    e21 = eg.ema_series(closes, 21)
    e55 = eg.ema_series(closes, 55)
    e144 = eg.ema_series(closes, 144)
    if any(e is None or not np.isfinite(e[-1]) for e in (e21, e55, e144)):
        return ing                   # 生产此处 return（项 2-8 不评）
    d21, d55, d144 = float(e21[-1]), float(e55[-1]), float(e144[-1])
    ing["aligned"] = (d21 > d55 > d144) or (d21 < d55 < d144)

    h = np.array([float(k[2]) for k in closed], dtype=float)
    low = np.array([float(k[3]) for k in closed], dtype=float)
    tr = np.maximum(h[1:] - low[1:],
                    np.maximum(np.abs(h[1:] - closes[:-1]),
                               np.abs(low[1:] - closes[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0
    ing["atr"] = atr

    # 项 3/4：分离史口径（近 RANGE_DIV_LOOKBACK 根最大分离度 / ATR）
    if atr > 0:
        lb = min(RANGE_DIV_LOOKBACK, n - 143)
        if lb >= 1:
            ing["fm_div"] = float(np.max(np.abs(e21[-lb:] - e55[-lb:]))) / atr
            ing["ms_div"] = float(np.max(np.abs(e55[-lb:] - e144[-lb:]))) / atr

    # 项 5：1h ADX（生产用含未收盘尾根的原始 klines）
    try:
        adx_info = analyze_adx(win, period=14)
    except Exception:
        adx_info = None
    if adx_info:
        ing["adx"] = float(adx_info["adx"])

    # 项 6：布林带宽分位（逐字复刻生产 cumsum 实现，保证 baseline 可复现）
    if n >= 20 + BBW_MIN_HIST + 1:
        csum = np.cumsum(np.insert(closes, 0, 0.0))
        csum2 = np.cumsum(np.insert(closes ** 2, 0, 0.0))
        idx = np.arange(19, n)
        mid = (csum[idx + 1] - csum[idx + 1 - 20]) / 20
        var = (csum2[idx + 1] - csum2[idx + 1 - 20]) / 20 - mid ** 2
        bw = 4.0 * np.sqrt(np.maximum(var, 0.0)) / np.maximum(mid, 1e-12)
        hist = bw[max(0, len(bw) - 1 - BBW_HISTORY): len(bw) - 1]
        if len(hist) >= BBW_MIN_HIST and np.isfinite(bw[-1]) \
                and float(bw[-1]) < float(np.percentile(hist, BBW_PCTL)):
            ing["item6"] = True

    # 项 7：价格反复穿越 EMA21
    crosses = 0
    for i in range(max(1, n - CROSS_WINDOW), n):
        if (closes[i - 1] - float(e21[i - 1])) * (closes[i] - float(e21[i])) < 0:
            crosses += 1
    ing["crosses"] = crosses

    # 项 8：成交量变异系数
    vols = np.array([float(k[5]) for k in closed[-CROSS_WINDOW:]], dtype=float)
    vmean = float(vols.mean()) if len(vols) else 0.0
    if vmean > 0 and float(vols.std(ddof=0)) / vmean < VOL_CV_MIN:
        ing["item8"] = True

    # 新增候选项 X 的原料（全历史窗口，与 v2 C 段同口径）
    highs_a = np.array([float(k[2]) for k in kl_all], dtype=float)
    lows_a = np.array([float(k[3]) for k in kl_all], dtype=float)
    closes_a = np.array([float(k[4]) for k in kl_all], dtype=float)
    ing["donch_pctl"] = _donch_pctl(highs_a, lows_a, closes_a, t)
    ing["bbw_pctl"] = _bbw_pctl(closes_a, t)
    return ing


def _score(ing: dict, flags: set) -> int:
    """按变体启用项计分（每项 +1；项 1 独立于 n 门槛）"""
    s = 0
    if 1 in flags and ing["item1"]:
        s += 1
    if ing["n"] < 200 or ing["aligned"] is None:
        return s                     # 与生产同步：EMA 不可用即止步
    if 2 in flags and not ing["aligned"]:
        s += 1
    if 3 in flags and ing["fm_div"] is not None and ing["fm_div"] < 0.5:
        s += 1
    if 4 in flags and ing["ms_div"] is not None and ing["ms_div"] < 1.0:
        s += 1
    if 5 in flags and ing["adx"] is not None and ing["adx"] < ADX_WARN_MIN:
        s += 1
    if 6 in flags and ing["item6"]:
        s += 1
    if 7 in flags and ing["crosses"] >= CROSS_MAX:
        s += 1
    if 8 in flags and ing["item8"]:
        s += 1
    if "X" in flags:
        dp, bp = ing["donch_pctl"], ing["bbw_pctl"]
        if (dp is not None and dp >= EXP_PCTL) or (bp is not None and bp >= EXP_PCTL):
            s += 1
    return s


# ── harness：关掉硬门跑一遍，记录全部变体得分 ─────────────────────────

def _pass_soft(sym: str, kl_all: list, htf_all: list, flt) -> list[dict]:
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend = None
    for t in range(WINDOW - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
        t_close = kl_all[t][0] + HOUR_MS
        i = bisect_left(htf_opens, t_close)
        htf = htf_all[:i][-300:] or None
        htf_res = None
        if htf:
            try:
                htf_res = tech.htf_context(htf)
            except Exception:
                htf_res = None
            else:
                if htf_res is None or htf_res.get("reject_reason"):
                    if pend:
                        pend = None
                    continue
            if htf_res is not None and htf_res.get("block_both"):
                if pend:
                    pend = None
                continue
        gate_dir = htf_res.get("direction") if htf_res is not None else None

        votes = tech.compute_indicator_votes(kl, htf_bg=htf_res)
        if (not votes) or (_min_same(votes) < tech.DIR_VOTE_MIN):
            if pend:
                pend = None
            continue
        direction = 1 if int(votes["net"]) > 0 else -1
        if gate_dir is not None and gate_dir != ("long" if direction == 1 else "short"):
            if pend:
                pend = None
            continue

        # 硬门一律不否决（反事实全集 S），仅记录各变体得分
        gate_ev = htf_res.get("evidence") if htf_res else None
        ing = _ingredients(kl_all, t, gate_ev)
        scores = {name: _score(ing, flags) for name, flags in VARIANTS.items()}

        trigd, pend_op = trig.detect_trigger(
            kl, direction, pend, flt=flt, retest=None, htf=htf, htf_dir=gate_dir)
        if pend_op and pend_op.get("op") == "upsert":
            if trig.PULLBACK_CONFIRM not in flt.allowed_triggers:
                pend = None
                continue
            pend = {"id": "sim", "direction": pend_op["direction"],
                    "touch_ref": pend_op.get("touch_ref"),
                    "stage1_candle_open": pend_op["stage1_candle_open"]}
            continue
        if pend_op:
            pend = None
        if trigd is None or trigd["type"] not in flt.allowed_triggers:
            continue

        in_scope = trigd["type"] in CONFIRM_TYPES
        fw = (_fwd_outcome(kl_all, t, direction) if in_scope
              else {"r": None, "net_r": None, "nbars": None})
        sigs.append({"ts": kl_all[t][0], "symbol": sym, "in_scope": in_scope,
                     "scores": scores, **fw})
    return sigs


# ── 统计 ─────────────────────────────────────────────────────────────

def _pnl(rows: list[dict]) -> dict:
    dec = [s for s in rows if s["net_r"] is not None]
    w = sum(1 for s in dec if s["r"] > 0)
    nr = [s["net_r"] for s in dec]
    return {"n": len(rows), "decided": len(dec),
            "win_rate": (w / len(dec)) if dec else None,
            "wilson_lb": wilson_lb(w, len(dec)),
            "exp_r": (sum(s["r"] for s in dec) / len(dec)) if dec else None,
            "net_r": (sum(nr) / len(nr)) if nr else None}


def _f(v, nd=2, pct=False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def _split(rows: list[dict], cut: int) -> tuple[list, list]:
    return ([s for s in rows if s["ts"] < cut], [s for s in rows if s["ts"] >= cut])


def main() -> None:
    ap = argparse.ArgumentParser(description="震荡评分卡判据消融 A/B")
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--confirm", default="",
                    help="逗号分隔变体名（可写 V3_core_245_x@1 显式指定阈值）："
                         "按真实重跑漏斗确认 Phase 1 结论")
    args = ap.parse_args()

    import httpx
    t0 = time.time()
    flt = resolve_scan_filters(None)
    with httpx.Client() as client:
        syms = top_universe(client, args.symbols, args.min_vol)
        print(f"宇宙 {len(syms)} 币 · 每币 {args.days} 天 1h + 600 根 4h")
        allrows: list[dict] = []
        for i, s in enumerate(syms, 1):
            try:
                kl = fetch_klines(client, s, "1h", args.days * 24 + WINDOW + 40)
                htf = fetch_klines(client, s, "4h", 600)
            except Exception as e:
                print(f"  [{i}/{len(syms)}] {s} 拉取失败: {str(e)[:60]}")
                continue
            if len(kl) < WINDOW + 10:
                continue
            try:
                allrows += _pass_soft(s, kl, htf, flt)
            except Exception as e:
                print(f"  [{i}/{len(syms)}] {s} 回放异常: {str(e)[:80]}")
                continue
            if i % 10 == 0 or i == len(syms):
                print(f"  [{i}/{len(syms)}] 累计候选={len(allrows)} "
                      f"({time.time() - t0:.0f}s)", flush=True)

    if not allrows:
        print("无候选")
        return

    inc = [s for s in allrows if s["in_scope"]]
    cut = max(s["ts"] for s in inc) - TEST_DAYS * 24 * HOUR_MS
    tr, te = _split(inc, cut)
    print(f"\n反事实全集 S = {len(allrows)} 候选（其中回踩确认 {len(inc)}）；"
          f"train n={len(tr)} / test n={len(te)}")

    # 自检：baseline@thr3 应与 v2「放 rs<3」一致
    base_acc = [s for s in inc if s["scores"]["V0_baseline"] < BASELINE_THR]
    base_rej = [s for s in inc if s["scores"]["V0_baseline"] >= BASELINE_THR]
    a_acc, a_rej = _pnl(base_acc), _pnl(base_rej)
    print(f"自检 V0@thr{BASELINE_THR}：放行 n={a_acc['n']} netR={_f(a_acc['net_r'])} | "
          f"拦 n={a_rej['n']} netR={_f(a_rej['net_r'])} "
          f"（应与 v2 的 1758 / −0.02 与 402 / −0.23 对齐）")

    print("\n## 一、全样本扫描（行内 thr↑ = 拒得更多，accept 组应更好）")
    print("| 变体 | thr | 放行n | 拒绝率 | 放行netR | 放行胜率 | 拦下netR | Δ(拦−放) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    best: dict[str, tuple] = {}
    for vname in VARIANTS:
        maxscore = len(VARIANTS[vname])
        for thr in range(1, maxscore + 1):
            acc = [s for s in inc if s["scores"][vname] < thr]
            rej = [s for s in inc if s["scores"][vname] >= thr]
            aa, rr = _pnl(acc), _pnl(rej)
            if aa["decided"] < MIN_N:
                break
            rej_rate = len(rej) / len(inc)
            d = ((rr["net_r"] or 0) - (aa["net_r"] or 0))
            print(f"| {vname} | {thr} | {aa['n']} | {rej_rate:.1%} | "
                  f"{_f(aa['net_r'])} | {_f(aa['win_rate'], pct=True, nd=1)} | "
                  f"{_f(rr['net_r'])} | {d:+.3f} |")
            # 记录「拒绝率最接近 baseline」的档位
            cur = abs(rej_rate - len(base_rej) / len(inc))
            if vname not in best or cur < best[vname][0]:
                best[vname] = (cur, thr, aa, rr, rej_rate)
        print("| | | | | | | | |")

    # 等拒绝率对比（唯一可归因于「判据质量」的比较）
    print("\n## 二、等拒绝率对比（对齐到 baseline 拒绝率，Δ 可归因于判据本身）")
    print("| 变体 | thr | 拒绝率 | 放行n | 放行netR | 放行胜率 | WilsonLB | 拦下netR |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for vname, (_, thr, aa, rr, rate) in best.items():
        print(f"| {vname} | {thr} | {rate:.1%} | {aa['n']} | {_f(aa['net_r'])} | "
              f"{_f(aa['win_rate'], pct=True, nd=1)} | "
              f"{_f(aa['wilson_lb'], pct=True, nd=1)} | {_f(rr['net_r'])} |")

    # OOS 检验：同一 thr（不重新调参）在 test 段的放行组表现
    print(f"\n## 三、OOS 检验（沿用上表 thr，不重调；test = 末 {TEST_DAYS} 天）")
    print("| 变体 | thr | train放行netR | test放行netR | test放行n | test胜率 | test WilsonLB |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for vname, (_, thr, _aa, _rr, _rate) in best.items():
        tr_acc = [s for s in tr if s["scores"][vname] < thr]
        te_acc = [s for s in te if s["scores"][vname] < thr]
        at, ae = _pnl(tr_acc), _pnl(te_acc)
        print(f"| {vname} | {thr} | {_f(at['net_r'])} | {_f(ae['net_r'])} | "
              f"{ae['n']} | {_f(ae['win_rate'], pct=True, nd=1)} | "
              f"{_f(ae['wilson_lb'], pct=True, nd=1)} |")

    print("\n判读要点：")
    print("1. 「放行netR」越高越好（闸门越接近「只留好单」）；「Δ(拦−放)」越负越说明拦得对。")
    print("2. 拒绝率不对齐时不可横比——低拒绝率天然放行更多样本 → 净期望被拉低。")
    print("3. 只有 train/test 同向的变体才考虑落地；test 段 n<30 不看。")

    if args.confirm:
        print(f"\n## 四、Phase 2 真实重跑确认（pending 演化随各变体改变）：{args.confirm}")
        print("| 变体 | thr | 放行n | 放行netR | 拦下netR | 与 Phase 1 放行netR 差 |")
        print("| --- | --- | --- | --- | --- | --- |")
        names = [x.strip() for x in args.confirm.split(",") if x.strip()]
        for spec in names:
            vname, _, thr_s = spec.partition("@")
            if vname not in VARIANTS:
                print(f"| {spec} | 未知名，跳过 | | | | |")
                continue
            thr = int(thr_s) if thr_s else (best[vname][1] if vname in best else 1)
            rows: list[dict] = []
            with httpx.Client() as client:
                for s in syms:
                    try:
                        kl = fetch_klines(client, s, "1h", args.days * 24 + WINDOW + 40)
                        htf = fetch_klines(client, s, "4h", 600)
                        if len(kl) >= WINDOW + 10:
                            rows += _pass_confirm(s, kl, htf, flt, vname, thr)
                    except Exception:
                        continue
            acc = [s for s in rows if s.get("passed") and s["in_scope"]]
            rej = [s for s in rows if (not s.get("passed")) and s["in_scope"]]
            aa, rr = _pnl(acc), _pnl(rej)
            p1 = best[vname][2]["net_r"] if vname in best else None
            print(f"| {vname} | {thr} | {aa['n']} | {_f(aa['net_r'])} | "
                  f"{_f(rr['net_r'])} | "
                  f"{(aa['net_r'] - p1) if aa['net_r'] is not None and p1 is not None else float('nan'):+.3f} |")
            print(f"| ↑ {vname}@{thr} 全样本：放行 {_f(aa['net_r'])} / 拦下 {_f(rr['net_r'])} "
                  f"/ 胜率 {_f(aa['win_rate'], pct=True, nd=1)} / WilsonLB "
                  f"{_f(aa['wilson_lb'], pct=True, nd=1)} |")
            if acc:
                c2 = max(s["ts"] for s in acc) - TEST_DAYS * 24 * HOUR_MS
                tr2, te2 = _split(acc, c2)
                a_tr, a_te = _pnl(tr2), _pnl(te2)
                print(f"|   └ train放行 {_f(a_tr['net_r'])} / test放行 "
                      f"{_f(a_te['net_r'])}（n={a_te['n']}，胜率 "
                      f"{_f(a_te['win_rate'], pct=True, nd=1)}） |")


def _pass_confirm(sym: str, kl_all: list, htf_all: list, flt,
                  vname: str, thr: int) -> list[dict]:
    """Phase 2：按变体真实否决（pending 演化随之改变），只回踩确认计收益。"""
    flags = VARIANTS[vname]
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend = None
    for t in range(WINDOW - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
        t_close = kl_all[t][0] + HOUR_MS
        i = bisect_left(htf_opens, t_close)
        htf = htf_all[:i][-300:] or None
        htf_res = None
        if htf:
            try:
                htf_res = tech.htf_context(htf)
            except Exception:
                htf_res = None
            else:
                if htf_res is None or htf_res.get("reject_reason"):
                    if pend:
                        pend = None
                    continue
            if htf_res is not None and htf_res.get("block_both"):
                if pend:
                    pend = None
                continue
        gate_dir = htf_res.get("direction") if htf_res is not None else None
        votes = tech.compute_indicator_votes(kl, htf_bg=htf_res)
        if (not votes) or (_min_same(votes) < tech.DIR_VOTE_MIN):
            if pend:
                pend = None
            continue
        direction = 1 if int(votes["net"]) > 0 else -1
        if gate_dir is not None and gate_dir != ("long" if direction == 1 else "short"):
            if pend:
                pend = None
            continue
        gate_ev = htf_res.get("evidence") if htf_res else None
        blocked = _score(_ingredients(kl_all, t, gate_ev), flags) >= thr
        if blocked:
            # 尽管被拦，仍记录该根的假想单（Phase 2 反事实需要「拦下」的 PNL）；
            # 同时清 pending——与生产「硬门否决即清池」同义
            if pend:
                pend = None
            fw = _fwd_outcome(kl_all, t, direction)
            sigs.append({"ts": kl_all[t][0], "symbol": sym, "passed": False,
                         "in_scope": True, **fw})
            continue
        trigd, pend_op = trig.detect_trigger(
            kl, direction, pend, flt=flt, retest=None, htf=htf, htf_dir=gate_dir)
        if pend_op and pend_op.get("op") == "upsert":
            if trig.PULLBACK_CONFIRM not in flt.allowed_triggers:
                pend = None
                continue
            pend = {"id": "sim", "direction": pend_op["direction"],
                    "touch_ref": pend_op.get("touch_ref"),
                    "stage1_candle_open": pend_op["stage1_candle_open"]}
            continue
        if pend_op:
            pend = None
        if trigd is None or trigd["type"] not in flt.allowed_triggers:
            continue
        in_scope = trigd["type"] in CONFIRM_TYPES
        fw = (_fwd_outcome(kl_all, t, direction) if in_scope
              else {"r": None, "net_r": None, "nbars": None})
        sigs.append({"ts": kl_all[t][0], "symbol": sym, "passed": True,
                     "in_scope": in_scope, **fw})
    return sigs


if __name__ == "__main__":
    main()
