# -*- coding: utf-8 -*-
"""震荡评分卡「方向感知 PNL」诊断 v2（2026-10-20）。

v1（range_score_replay.py）用**方向无关**的前向效率比做标签，结论：评分卡对前瞻
趋势度几乎无区分力（阈值 3 的拒绝集 vs 放行集 Δ=-0.001）。但 v1 回答不了闸门真正
的问题——**4h 排列方向上的回踩单，被 range_score（≥3）拦掉的那些，期望收益是否真的
更差**？本脚本补上这一层：把标签换成方向感知的近锚止损 PNL。

口径（复刻生产漏斗，复用 gate_soft_replay 的取数与收益模型）：
- 决策对象 = 走完 4h 排列 + 方向票同向 + 方向不冲突、**到达 range_score 闸门**的
  候选（这三关先于闸门，正是闸门的作用域）；
- 关键实现：**关掉 range_score 硬门**跑一遍（rs 值仅记录、不否决），得到「若无此门
  会产生的候选全集」S；S 内按 rs≥3 / rs<3 切分即闸门的一对一**反事实**；
- 另跑一遍**开闸门**（生产语义）得实际候选集 P，用 |S|-|P| 量化与状态机的交互
  （关掉闸门后 pending 演化不同，多出的信号常来自硬态根本没能进池的回踩路径——
  与 gate_soft_replay「软化后泄漏偏乐观」同类局限）;
- 收益模型：近锚止损（近 5 根极值 ±0.3%）+ 1.5R 固定止盈 + 往返费率（同
  gate_soft_replay._fwd_outcome）；仅**回踩确认**类触发计收益（突破类占配额但不入
  PNL，近锚模型只对回踩成立）。

方向敏感特征（「重构判据」的候选，v1 未含）：der24 = 顺候选方向的当下效率比
（方向上的动能在不在）；fwd_der24 = 顺方向前瞻效率比（参考标签）。

判读：
  A. 闸门反事实：拦(rs≥3) vs 放(rs<3) 两组 PNL —— 拦组期望R ≤ 放组 = 误拦
  A2. 阈值扫描 + train/test（前段定标/后 20 天验证），预注册判定
  B. 项级归因（PNL 口径：命中 vs 未命中的期望R）
  C. 候选特征 × PNL 分档 + Spearman（**负**相关 = 好的拦截信号）
  D. 方向 ER：der24 的预测力（新判据前瞻证据）

预注册判定（同 gate_soft_replay.verdict）：n≥30 ∧ Wilson LB>40% ∧ 期望R>0
∧ 净期望R>0 ∧ top1 占比<40%。

局限：①关闸门使 pending 演化偏离生产；②宇宙取当前 24h 额 top（幸存者偏差）；
③近锚止损模型不含移动止损/分批；④样本为 60 天单一 regime。

用法（cwd=backend）：
    python scripts/range_score_pnl_replay.py --symbols 8 --days 30     # 冒烟
    python scripts/range_score_pnl_replay.py --symbols 60 --days 60    # 全量
"""
import argparse
import json
import statistics
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
from range_score_replay import (                                # noqa: E402
    ITEM_OF_REASON, VR_K, FEAT_K, _bbw_pctl, _donch_pctl, _er,
    _spearman, _vr,
)
from app.config import settings                                 # noqa: E402
from app.services.scan_filters import resolve_scan_filters      # noqa: E402
from app.services.strategy import ema_gate as eg                # noqa: E402
from app.services.strategy import technical as tech             # noqa: E402
from app.services.strategy import trigger as trig               # noqa: E402
from app.services.strategy.technical import analyze_adx         # noqa: E402

TEST_DAYS = 20          # 末尾 N 天为 OOS 测试集（其余为 train）
MIN_N = 30              # 预注册样本下限


def _der(closes: np.ndarray, t: int, k: int, direction: int,
         fwd: bool) -> Optional[float]:
    """方向效率比：dir·(c[end]-c[start]) / Σ|c[i]-c[i-1]|。高正 = 沿方向走趋势。"""
    lo, hi = (t, t + k) if fwd else (t - k, t)
    if lo < 0 or hi >= len(closes):
        return None
    seg = closes[lo:hi + 1]
    path = float(np.sum(np.abs(np.diff(seg))))
    if path <= 0:
        return None
    return direction * float(seg[-1] - seg[0]) / path


def _features(kl_all: list, t: int, direction: int) -> dict:
    """信号根 t 的候选特征（只用 ≤t 的已收盘数据，无前视）"""
    closes = np.array([float(k[4]) for k in kl_all], dtype=float)
    highs = np.array([float(k[2]) for k in kl_all], dtype=float)
    lows = np.array([float(k[3]) for k in kl_all], dtype=float)
    f: dict = {}
    for k in FEAT_K:
        f[f"er{k}"] = _er(closes, t, k, fwd=False)
    f["vr24"] = _vr(closes, t, VR_K)
    f["donch_pctl"] = _donch_pctl(highs, lows, closes, t)
    f["bbw_pctl"] = _bbw_pctl(closes, t)
    adx = analyze_adx(kl_all[max(0, t + 1 - WINDOW): t + 2], period=14)
    f["adx"] = float(adx["adx"]) if adx else None
    f["der24"] = _der(closes, t, 24, direction, fwd=False)
    f["fwd_der24"] = _der(closes, t, 24, direction, fwd=True)
    return f


def _pass(symbol: str, kl_all: list, htf_all: list, flt,
          enforce_rs: bool) -> list[dict]:
    """单币走历史：复刻 gate_soft_replay._harness_pass 的编排，额外记录
    range_score 值/项命中/方向感知特征。enforce_rs=False 时硬门不否决（反事实）。"""
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend: Optional[dict] = None
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
        flip_conflict = (gate_dir is not None
                         and gate_dir != ("long" if direction == 1 else "short"))
        if flip_conflict:
            if pend:
                pend = None
            continue

        # 到达 range_score 闸门（作用域终点）——记录 rs 与项命中
        gate_ev = htf_res.get("evidence") if htf_res else None
        rs, rs_reasons = eg.range_score(kl, gate_ev=gate_ev)
        if enforce_rs and rs >= eg.RANGE_SCORE_REJECT:
            if pend:
                pend = None
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
              else {"r": None, "net_r": None, "nbars": None,
                    "outcome": "out_of_scope"})
        sigs.append({
            "ts": kl_all[t][0], "symbol": symbol, "dir": direction,
            "signal_type": trigd["type"], "in_scope": in_scope,
            "rs": rs, "items": sorted(ITEM_OF_REASON[r] for r in rs_reasons
                                      if r in ITEM_OF_REASON),
            "f_": _features(kl_all, t, direction),
            **fw,
        })
    return sigs


# ── 统计 ─────────────────────────────────────────────────────────────

def _pnl(rows: list[dict]) -> dict:
    inc = [s for s in rows if s.get("in_scope")]
    dec = [s for s in inc if s["r"] is not None]
    n = len(inc)
    by_sym: dict[str, int] = {}
    for s in inc:
        by_sym[s["symbol"]] = by_sym.get(s["symbol"], 0) + 1
    w = sum(1 for s in dec if s["r"] > 0)
    rd = [s["r"] for s in dec]
    nr = [s["net_r"] for s in dec]
    return {"n": n, "decided": len(dec), "open": n - len(dec),
            "win_rate": (w / len(dec)) if dec else None,
            "wilson_lb": wilson_lb(w, len(dec)),
            "exp_r": statistics.mean(rd) if rd else None,
            "net_exp_r": statistics.mean(nr) if nr else None,
            "top_share": (max(by_sym.values()) / n) if n and by_sym else 0.0}


def _fmt(v, pct=False, nd=2) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def _row(name: str, rows: list[dict]) -> str:
    a = _pnl(rows)
    return (f"| {name} | {a['n']} | {a['decided']} | {a['open']} "
            f"| {_fmt(a['win_rate'], pct=True, nd=1)} "
            f"| {_fmt(a['wilson_lb'], pct=True, nd=1)} | {_fmt(a['exp_r'])} "
            f"| {_fmt(a['net_exp_r'])} | {_fmt(a['top_share'], pct=True, nd=0)} |")


def _verdict(rows: list[dict]) -> str:
    a = _pnl(rows)
    if a["n"] < MIN_N:
        return f"样本不足({a['n']})"
    ok = (a["wilson_lb"] > 0.40 and (a["exp_r"] or 0) > 0
          and (a["net_exp_r"] or 0) > 0 and a["top_share"] < 0.40)
    return "PASS" if ok else "FAIL"


def _feat_table(rows: list[dict], feats: list[str], nb: int = 4) -> dict:
    """每个特征分 nb 档 → 净期望R；并报 Spearman(特征, net_r)（负=好的拦截信号）"""
    inc = [s for s in rows if s.get("in_scope") and s["net_r"] is not None]
    rho_out: dict = {}
    for key in feats:
        pair = [(s["f_"][key], s["net_r"]) for s in inc
                if s["f_"].get(key) is not None]
        if len(pair) < 40:
            print(f"  {key:<12} 样本不足 n={len(pair)}")
            continue
        pair.sort(key=lambda p: p[0])
        x = np.array([a for a, _ in pair], float)
        y = np.array([b for _, b in pair], float)
        rho = _spearman(x, y)
        rho_out[key] = rho
        size = len(pair) // nb
        print(f"  {key:<12} ρ={_fmt(rho) if rho is not None else '-':>7} n={len(pair):<6}"
              f" 分档净期望R：", end="")
        for g in range(nb):
            lo = g * size
            hi = len(pair) if g == nb - 1 else (g + 1) * size
            seg = y[lo:hi]
            xm = x[lo:hi].mean()
            print(f"[{xm:+.2f}]→{seg.mean():+.2f} ", end="")
        print()
    return rho_out


def main() -> None:
    ap = argparse.ArgumentParser(description="震荡评分卡方向感知 PNL 诊断 v2")
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import httpx
    t0 = time.time()
    flt = resolve_scan_filters(None)
    with httpx.Client() as client:
        syms = top_universe(client, args.symbols, args.min_vol)
        print(f"宇宙 {len(syms)} 币 · 每币 {args.days} 天 1h + 600 根 4h")
        soft: list[dict] = []       # enforce_rs=False：反事实全集 S
        hard: list[dict] = []       # enforce_rs=True：生产语义 P
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
                soft += _pass(s, kl, htf, flt, enforce_rs=False)
                hard += _pass(s, kl, htf, flt, enforce_rs=True)
            except Exception as e:
                print(f"  [{i}/{len(syms)}] {s} 回放异常: {str(e)[:80]}")
                continue
            print(f"  [{i}/{len(syms)}] {s} S={sum(1 for x in soft if x['symbol']==s)} "
                  f"P={sum(1 for x in hard if x['symbol']==s)} ({time.time()-t0:.0f}s)",
                  flush=True)

    if not soft:
        print("无候选")
        return

    print("\n" + "=" * 96)
    print(f"反事实全集 S = {len(soft)} 候选（无 range_score 硬门）· "
          f"生产集 P = {len(hard)}（开硬门）· 硬门净拦 = {len(soft)-len(hard)}")
    n_inc_s = sum(1 for s in soft if s["in_scope"])
    n_inc_h = sum(1 for s in hard if s["in_scope"])
    print(f"计提收益（回踩确认）S={n_inc_s} / P={n_inc_h}")

    # A. 闸门反事实（S 内按 rs 切分）
    thr = eg.RANGE_SCORE_REJECT
    print(f"\n── A. 闸门反事实：拦(rs≥{thr}) vs 放(rs<{thr})（同一决策点、同一收益模型）──")
    print("| 组 | n | decided | open | 胜率 | Wilson LB | 期望R | 净期望R | top1 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    blocked = [s for s in soft if s["rs"] >= thr]
    passed = [s for s in soft if s["rs"] < thr]
    print(_row(f"拦 rs≥{thr}（硬门否决）", blocked))
    print(_row(f"放 rs<{thr}（硬门放行）", passed))
    ab, ap = _pnl(blocked), _pnl(passed)
    if ab["net_exp_r"] is not None and ap["net_exp_r"] is not None:
        print(f"Δ净期望R（拦−放）= {ab['net_exp_r'] - ap['net_exp_r']:+.3f}"
              f"（负=拦得对，拦掉的是更差的；≥0=误拦）")
    print(f"读法：若拦组期望R 不低于放组 → 该门在此口径下**没有筛出更差的单**，"
          f"即误拦。判定：拦组 {_verdict(blocked)} / 放组 {_verdict(passed)}")

    # A2. 阈值扫描 + train/test
    all_ts = [s["ts"] for s in soft]
    cut = max(all_ts) - TEST_DAYS * 24 * HOUR_MS
    train = [s for s in soft if s["ts"] < cut]
    test = [s for s in soft if s["ts"] >= cut]
    print(f"\n── A2. 阈值扫描（train<{time.strftime('%m-%d', time.gmtime(cut/1000))} "
          f"n={len(train)} / test 后{TEST_DAYS}天 n={len(test)}）──")
    print(f"{'阈值':<6}{'组':<6}{'n':>6}{'decided':>9}{'期望R':>9}{'净期望R':>9}"
          f"{'胜率':>8}{'WilsonLB':>10}")
    for thr in (1, 2, 3, 4, 5):
        for tag, sub in (("train", train), ("test", test)):
            rej = [s for s in sub if s["rs"] >= thr]
            pas = [s for s in sub if s["rs"] < thr]
            for nm, grp in (("拒", rej), ("放", pas)):
                a = _pnl(grp)
                if a["n"] < MIN_N:
                    continue
                print(f"{thr:<6}{tag + '-' + nm:<6}{a['n']:>6}{a['decided']:>9}"
                      f"{_fmt(a['exp_r']):>9}{_fmt(a['net_exp_r']):>9}"
                      f"{_fmt(a['win_rate'], pct=True, nd=1):>8}"
                      f"{_fmt(a['wilson_lb'], pct=True, nd=1):>10}")
        print()

    # B. 项级归因（PNL 口径）
    print("── B. 项级归因（PNL 口径：命中 vs 未命中的净期望R）──")
    print(f"{'项':<4}{'命中n':>7}{'命中expR':>10}{'命中netR':>10}"
          f"{'未命中netR':>11}{'Δ(netR)':>10}  判读")
    for item in range(1, 9):
        hit = [s for s in soft if item in s["items"]]
        no = [s for s in soft if item not in s["items"]]
        ah, an = _pnl(hit), _pnl(no)
        if ah["decided"] < 10 or an["decided"] < 10:
            print(f"{item:<4}{ah['n']:>7}   样本不足")
            continue
        d = (ah["net_exp_r"] or 0) - (an["net_exp_r"] or 0)
        tag = "误拦嫌疑" if d > 0.15 else ("有效" if d < -0.15 else "中性")
        print(f"{item:<4}{ah['n']:>7}{_fmt(ah['exp_r']):>10}{_fmt(ah['net_exp_r']):>10}"
              f"{_fmt(an['net_exp_r']):>11}{d:>+10.3f}  {tag}")

    # C. 候选特征 × PNL
    print("\n── C. 候选特征 × 净期望R（ρ<0 = 好的拦截信号）──")
    feats = ["er24", "er48", "vr24", "donch_pctl", "bbw_pctl", "adx", "der24"]
    _feat_table(soft, feats)

    # D. 方向 ER 的预测力（新判据前瞻证据）
    print("\n── D. 方向 ER：der24（当下顺方向动能）→ fwd_der24（前瞻）──")
    pair = [(s["f_"].get("der24"), s["f_"].get("fwd_der24")) for s in soft
            if s["f_"].get("der24") is not None and s["f_"].get("fwd_der24") is not None]
    if len(pair) >= 40:
        x = np.array([a for a, _ in pair], float)
        y = np.array([b for _, b in pair], float)
        print(f"  Spearman(der24, fwd_der24) = {_fmt(_spearman(x, y))} n={len(pair)}"
              f"（正=当下顺方向动能有延续性 → 可作「顺势才放行」型判据）")

    summary = {"n_soft": len(soft), "n_hard": len(hard),
               "blocked_net_r": ab["net_exp_r"], "passed_net_r": ap["net_exp_r"],
               "blocked_n": ab["n"], "passed_n": ap["n"]}
    if args.out:
        json.dump(summary, open(args.out, "w"), ensure_ascii=False, indent=2)
        print(f"\nsummary → {args.out}")


if __name__ == "__main__":
    main()
