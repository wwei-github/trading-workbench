# -*- coding: utf-8 -*-
"""4h 趋势闸门（gate_4h）× 4h 状态机（analyze_htf_background）冲突回放（2026-10-20）。

问题：生产链路里 4h 有两道独立前置门——`ema_gate.gate_4h`（价格×EMA144 + EMA144
20 根斜率，输出决定性方向 long/short）与 `technical.analyze_htf_background`（三线
EMA21/55/144 交叉状态机，STATE_BLOCK_BOTH 双向禁开 / STATE_LOCK_CONTRA 锁逆势）。
两者方向定义**不同源**，可能对同一币给出相反结论。本脚本量化这种对撞的频率、
其对「可开仓方向」的影响（是否双向全封），以及冲突时后续走势偏向谁（判断是否
过度封杀）。

口径要点（避免复现性争议，docs/17 §3.3 曾指出旧「74 笔」判据无留存脚本）：
- 输入：Binance USDT 永续 4h K 线（fapi 公共接口），逐根已收盘走历史；
- 计算：**直接调用生产函数** `htf_core` + `analyze_htf_background`；旧侧 `gate_4h`
  已于合并后删除，改用 `scripts/legacy_gate_4h.py` 的冻结参考实现（逐行同口径）
  （同一份 core 透传，与 scanner 同源），无二次实现、无口径漂移；
- 时点对齐：评估时点 = 第 t 根 4h **收盘时刻**，向函数传 bars[:t+2]（末根 t+1
  为「未收盘根」，被 htf_core/analyze_adx 内部剔除）——与生产扫描同口径；
- 前瞻：以 4h 收盘价计 t→t+h 收益（h=6 根≈24h / 18 根≈72h），并给 ATR(14,4h)
  归一化收益（跨币可比）。

输出：① 各分类样本占比；② 双向全封（无任何方向可开）发生率及归因；③ 冲突组
前瞻收益分布 vs「同向一致组」对照；④ 冲突按状态细分。

用法（cwd=backend）：
    python scripts/htf_gate_state_conflict_replay.py --symbols 8 --bars 600   # 冒烟
    python scripts/htf_gate_state_conflict_replay.py --symbols 60 --bars 900  # 全量
"""
import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy import technical as tech         # noqa: E402
from app.services.strategy.technical import (               # noqa: E402
    STATE_BLOCK_BOTH, STATE_LOCK_CONTRA, STATE_TRANSITION,
    _STATE_DIR, _sign_changes, analyze_htf_background, htf_core,
)
# 旧 gate_4h 已随 2026-10-20 合并从 ema_gate 删除；本审计脚本用冻结参考实现
from legacy_gate_4h import legacy_gate_4h                  # noqa: E402

FAPI = "https://fapi.binance.com"
MIN_CLOSED = 200        # gate_4h 额外数据门（已收盘根数下限）
FWD_24H = 6             # 前瞻 24h（4h×6）
FWD_72H = 18            # 前瞻 72h（4h×18）


# ── 数据获取（复用 gate_soft_replay 口径）─────────────────────────────
def _get(client: httpx.Client, url: str, params: dict) -> list:
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    return []


def fetch_klines(client: httpx.Client, symbol: str, interval: str,
                 total: int) -> list:
    """向前分页拉取 total 根 4h K 线（升序，含当前未收盘根）"""
    out: list = []
    end: Optional[int] = None
    while len(out) < total:
        limit = min(1500, total - len(out))
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end:
            params["endTime"] = end
        batch = _get(client, f"{FAPI}/fapi/v1/klines", params)
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        if len(batch) < limit:
            break
        time.sleep(0.12)
    return out


def top_universe(client: httpx.Client, n: int, min_vol: float) -> list:
    """当前 24h 成交额 top N 的 USDT 永续（幸存者偏差：宇宙取当前 top）"""
    rows = _get(client, f"{FAPI}/fapi/v1/ticker/24hr", {})
    usdt = [t for t in rows
            if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume") or 0) >= min_vol]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


# ── 闸门拒因标注（仅诊断用，与 gate_4h 阈值同源常量）───────────────────
def _gate_reject_reason(core: dict) -> str:
    """gate_4h 返回 None 时的归因（走平/缠绕/ADX/方向歧义），与生产阈值同源。"""
    closed = core["closed"]
    c, e144, price = core["closes"], core["e144"], core["price"]
    if len(closed) < MIN_CLOSED:
        return "data<200"
    if not (math.isfinite(float(e144[-1]))
            and math.isfinite(float(e144[-tech.SLOPE_BARS]))):
        return "nan"
    slope_pct = abs(float(e144[-1]) - float(e144[-tech.SLOPE_BARS])) / price
    if slope_pct < tech.SLOPE_FLAT_PCT:
        return "flat"
    wraps = tech._wrap_count(c, e144)
    if wraps > tech.TANGLE_WRAP_MAX:
        return "wrap"
    adx = core["adx"]
    if adx is None or adx < tech.TANGLE_ADX_MIN:
        return "adx"
    return "dir_ambiguous"   # 斜率/穿越与价格位置不同号（罕见几何）


def tangle_components(core: dict) -> dict:
    """「缠绕」两套口径的分量拆解（口径审计用，不改判定）。

    gate 口径：price×EMA144 近 TANGLE_WRAP_N 根穿越次数（>TANGLE_WRAP_MAX 即缠绕）；
    状态机口径：ADX<20 ∨ |EMA21−EMA55|/price<0.1% ∨ 21×55 近 10 根交叉 ≥2。
    """
    c, e21, e55, e144 = core["closes"], core["e21"], core["e55"], core["e144"]
    price = core["price"]
    wraps = tech._wrap_count(c, e144)
    adx = core["adx"]
    return {
        "gate_wrap": wraps,                                   # >2 即 gate 判缠绕
        "st_adx": 1 if (adx is not None and adx < 20) else 0,
        "st_gap": 1 if abs(float(e21[-1]) - float(e55[-1])) / price < 0.001 else 0,
        "st_zigzag": 1 if _sign_changes(e21, e55, tech.TANGLE_CROSS_LOOKBACK) >= 2 else 0,
    }


# ── 时点分类 ──────────────────────────────────────────────────────────
def classify(gate_dir: Optional[str], state: Optional[str]) -> dict:
    """给定闸门方向与状态，判定该 4h 快照的「可开方向」与对撞类型。

    long_ok / short_ok：该快照下是否存在可开该方向的通路（忽略 1h 方向票，
    只问 4h 两道门本身是否放行）。gate 只给一个方向，故至多一向为 True。
    """
    st_dir = _STATE_DIR.get(state, 0) if state else 0
    st_block = state in STATE_BLOCK_BOTH
    st_lock = state in STATE_LOCK_CONTRA

    long_ok = short_ok = False
    if gate_dir == "long":
        long_ok = (not st_block) and not (st_lock and st_dir == -1)
    elif gate_dir == "short":
        short_ok = (not st_block) and not (st_lock and st_dir == 1)

    both_blocked = (not long_ok) and (not short_ok)

    conflict = None
    if gate_dir == "long" and st_lock and st_dir == -1:
        conflict = "conflict_long_gate_bear_state"
    elif gate_dir == "short" and st_lock and st_dir == 1:
        conflict = "conflict_short_gate_bull_state"
    elif gate_dir is None and st_dir != 0:
        conflict = "gate_none_state_has_dir"
    elif gate_dir is not None and st_block:
        conflict = "gate_dir_state_block_both"

    agree = None
    if gate_dir == "long" and st_dir == 1 and not st_block:
        agree = "agree_long"
    elif gate_dir == "short" and st_dir == -1 and not st_block:
        agree = "agree_short"

    # 宽松口径：闸门方向与状态方向符号相反（不限是否锁逆势）——含过渡态
    # bull_dip(dir=-1)/bear_rally(dir=+1)，后者按设计「宏观同侧」不算冲突，
    # 故仅作诊断计数，判据仍以 conflict（LOCK_CONTRA 作用域）为准。
    dir_sign_opposite = (
        (gate_dir == "long" and st_dir == -1)
        or (gate_dir == "short" and st_dir == 1)
    )

    return {
        "state": state, "state_dir": st_dir, "gate_dir": gate_dir,
        "long_ok": long_ok, "short_ok": short_ok, "both_blocked": both_blocked,
        "conflict": conflict, "agree": agree,
        "dir_sign_opposite": dir_sign_opposite,
        "transition": state in STATE_TRANSITION,
    }


def fwd_ret(bars: list, t: int, h: int, atr: float) -> dict:
    """t→t+h 收益（收盘价），含 ATR 归一化；数据不足返回 None。"""
    if t + h >= len(bars):
        return {"pct": None, "atr": None}
    c0, c1 = float(bars[t][4]), float(bars[t + h][4])
    if c0 <= 0:
        return {"pct": None, "atr": None}
    return {"pct": c1 / c0 - 1.0, "atr": (c1 - c0) / atr if atr > 0 else None}


# ── 单币走历史 ────────────────────────────────────────────────────────
def replay_symbol(symbol: str, bars: list) -> list[dict]:
    """逐根 4h 已收盘时点评估两道门，返回每时点记录。"""
    recs: list[dict] = []
    for t in range(MIN_CLOSED - 1, len(bars) - 1):
        k4 = bars[:t + 2]          # 末根 t+1 视作未收盘根（被内部剔除）
        try:
            core = htf_core(k4)
            if core is None:
                continue
            g = legacy_gate_4h(k4, core=core)
            bg = analyze_htf_background(k4, core=core)
        except Exception:
            continue
        if bg is None:
            continue
        gate_dir = g[0] if g else None
        gate_reason = None if g else _gate_reject_reason(core)
        cls = classify(gate_dir, bg["state"])
        atr = float(core["atr"])
        r24 = fwd_ret(bars, t, FWD_24H, atr)
        r72 = fwd_ret(bars, t, FWD_72H, atr)
        recs.append({
            "symbol": symbol, "ts": int(bars[t][0]),
            "gate_dir": gate_dir, "gate_reason": gate_reason,
            "state": bg["state"], "state_dir": bg["dir"],
            **tangle_components(core),
            **{k: cls[k] for k in
               ("long_ok", "short_ok", "both_blocked", "conflict", "agree",
                "dir_sign_opposite", "transition")},
            "ret24_pct": r24["pct"], "ret24_atr": r24["atr"],
            "ret72_pct": r72["pct"], "ret72_atr": r72["atr"],
        })
    return recs


# ── 统计 ──────────────────────────────────────────────────────────────
def _summ(vals: list) -> dict:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    up = sum(1 for x in v if x > 0)
    return {
        "n": len(v), "mean": round(statistics.fmean(v), 5),
        "median": round(statistics.median(v), 5),
        "up_rate": round(up / len(v), 4),
        "p25": round(sorted(v)[len(v) // 4], 5),
        "p75": round(sorted(v)[3 * len(v) // 4], 5),
    }


def summarize(recs: list[dict]) -> dict:
    n = len(recs)
    if n == 0:
        return {"n": 0}

    def group(key, pred):
        return [r for r in recs if pred(r)]

    out: dict = {"n_snapshots": n, "n_symbols": len({r["symbol"] for r in recs})}

    # 闸门方向分布 / 拒因
    out["gate_dir_counts"] = {
        d: sum(1 for r in recs if r["gate_dir"] == d)
        for d in (None, "long", "short")
    }
    out["gate_reject_reasons"] = {}
    for r in recs:
        if r["gate_reason"]:
            out["gate_reject_reasons"][r["gate_reason"]] = \
                out["gate_reject_reasons"].get(r["gate_reason"], 0) + 1

    # 状态分布
    out["state_counts"] = {}
    for r in recs:
        out["state_counts"][r["state"]] = out["state_counts"].get(r["state"], 0) + 1

    # 分类
    out["both_blocked_rate"] = round(
        sum(1 for r in recs if r["both_blocked"]) / n, 4)
    out["conflict_counts"] = {}
    for r in recs:
        if r["conflict"]:
            out["conflict_counts"][r["conflict"]] = \
                out["conflict_counts"].get(r["conflict"], 0) + 1

    # 宽松口径：闸门方向与状态方向符号相反（含过渡态设计内的 bull_dip/bear_rally）
    opp = [r for r in recs if r["dir_sign_opposite"]]
    out["dir_sign_opposite_n"] = len(opp)
    out["dir_sign_opposite_by_state"] = {}
    for r in opp:
        key = f'{r["gate_dir"]}|{r["state"]}(dir={r["state_dir"]})'
        out["dir_sign_opposite_by_state"][key] = \
            out["dir_sign_opposite_by_state"].get(key, 0) + 1

    # 「缠绕」口径审计（gate 口径 vs 状态机口径 → 统一方案的净决策影响）
    out["tangle_component_counts"] = {
        "gate_wrap>WRAP_MAX": sum(1 for r in recs
                                  if r["gate_wrap"] > tech.TANGLE_WRAP_MAX),
        "st_adx<20": sum(1 for r in recs if r["st_adx"]),
        "st_gap<0.1%": sum(1 for r in recs if r["st_gap"]),
        "st_zigzag>=2": sum(1 for r in recs if r["st_zigzag"]),
    }
    wrap_rej = [r for r in recs if r["gate_reason"] == "wrap"]
    wrap_rej_free = [r for r in wrap_rej if r["state"] not in STATE_BLOCK_BOTH]
    out["tangle_unify_audit"] = {
        # 方案 A（gate 收敛到状态机三口径 = 删掉 price×EMA144 缠绕）：
        #   原本因 wrap 被拒、且状态机不拦的时点 → 会新增放行
        "wrap_rejected_total": len(wrap_rej),
        "wrap_rejected_state_block_both": len(wrap_rej) - len(wrap_rej_free),
        "optA_newly_allowed": len(wrap_rej_free),
        "optA_newly_allowed_by_state": {},
        # 方案 B（把 price×EMA144 缠绕并入状态机 tangle）：
        #   wrap>2 的时点几乎已被 gate_4h 拒（gate 自身含 wrap 条件）→ 净新增拒绝≈0
        "optB_newly_rejected": sum(
            1 for r in recs if r["gate_wrap"] > tech.TANGLE_WRAP_MAX
            and r["gate_reason"] is None),
    }
    for r in wrap_rej_free:
        out["tangle_unify_audit"]["optA_newly_allowed_by_state"][r["state"]] = \
            out["tangle_unify_audit"]["optA_newly_allowed_by_state"].get(r["state"], 0) + 1

    # 硬对撞明细（LOCK_CONTRA 作用域，逐条列出供人工核对）
    out["hard_conflict_examples"] = [
        {"symbol": r["symbol"], "ts": r["ts"], "gate_dir": r["gate_dir"],
         "state": r["state"], "ret24_atr": r["ret24_atr"],
         "ret72_atr": r["ret72_atr"]}
        for r in recs if r["conflict"] and r["conflict"].startswith("conflict_")
    ]

    # 双向全封归因
    bb = [r for r in recs if r["both_blocked"]]
    attrib = {"gate_none": 0, "state_block_both": 0, "dir_conflict_lock": 0,
              "gate_dir_state_block_both": 0}
    for r in bb:
        if r["gate_dir"] is None:
            attrib["gate_none"] += 1
        elif r["state"] in STATE_BLOCK_BOTH:
            attrib["state_block_both"] = attrib["state_block_both"] + 1
            attrib["gate_dir_state_block_both"] += 1
        elif r["conflict"] and r["conflict"].startswith("conflict_"):
            attrib["dir_conflict_lock"] += 1
    out["both_blocked_attribution"] = attrib

    # 前瞻收益对照（ATR 归一化）
    def stats_for(pred, tag):
        g = group(tag, pred)
        return {
            "n": len(g),
            "ret24_atr": _summ([r["ret24_atr"] for r in g]),
            "ret72_atr": _summ([r["ret72_atr"] for r in g]),
        }

    out["fwd_by_group"] = {
        "conflict_long_gate_bear_state": stats_for(
            lambda r: r["conflict"] == "conflict_long_gate_bear_state", "cA"),
        "conflict_short_gate_bull_state": stats_for(
            lambda r: r["conflict"] == "conflict_short_gate_bull_state", "cB"),
        "agree_long": stats_for(lambda r: r["agree"] == "agree_long", "al"),
        "agree_short": stats_for(lambda r: r["agree"] == "agree_short", "as"),
        "transition": stats_for(lambda r: r["transition"], "tr"),
        "gate_none": stats_for(lambda r: r["gate_dir"] is None, "gn"),
        "all": stats_for(lambda r: True, "all"),
    }
    out["fwd_by_group"]["wrap_rejected_not_state_blocked"] = {
        "n": len(wrap_rej_free),
        "ret24_atr": _summ([r["ret24_atr"] for r in wrap_rej_free]),
        "ret72_atr": _summ([r["ret72_atr"] for r in wrap_rej_free]),
    }

    # 冲突按状态细分（冲突组内计数）
    out["conflict_by_state"] = {}
    for r in recs:
        if r["conflict"]:
            key = f'{r["conflict"]}|{r["state"]}'
            out["conflict_by_state"][key] = out["conflict_by_state"].get(key, 0) + 1

    return out


def _fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return "n=0"
    a = s["ret24_atr"]
    b = s["ret72_atr"]
    exp = (f"fwd24(ATR) n={a['n']} 均值{a['mean']} 中位{a['median']} 上涨率{a['up_rate']}"
           if a["n"] else "fwd24 n=0")
    exp2 = (f"fwd72(ATR) n={b['n']} 均值{b['mean']} 中位{b['median']} 上涨率{b['up_rate']}"
            if b["n"] else "fwd72 n=0")
    return f"{exp}\n            {exp2}"


def report(s: dict) -> str:
    L = []
    L.append(f"快照数 {s['n_snapshots']} / 币数 {s['n_symbols']}")
    L.append(f"gate 方向分布 {s['gate_dir_counts']}")
    L.append(f"gate 拒因 {s['gate_reject_reasons']}")
    L.append(f"双向全封率 {s['both_blocked_rate']}  归因 {s['both_blocked_attribution']}")
    L.append(f"对撞计数 {s['conflict_counts']}")
    L.append(f"方向符号相反(宽松) {s['dir_sign_opposite_n']} → {s['dir_sign_opposite_by_state']}")
    L.append(f"缠绕分量 {s['tangle_component_counts']}")
    L.append(f"缠绕统一审计 {s['tangle_unify_audit']}")
    for e in s.get("hard_conflict_examples", []):
        L.append(f"  · 硬对撞明细 {e}")
    L.append("— 前瞻收益对照（ATR 归一化）—")
    for k in ("conflict_long_gate_bear_state", "conflict_short_gate_bull_state",
              "agree_long", "agree_short", "transition", "gate_none",
              "wrap_rejected_not_state_blocked", "all"):
        L.append(f"  [{k}] {_fmt(s['fwd_by_group'][k])}")
    L.append("— 对撞 × 状态细分 —")
    for k, v in sorted(s["conflict_by_state"].items(), key=lambda kv: -kv[1]):
        L.append(f"  {k}: {v}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=8)
    ap.add_argument("--bars", type=int, default=600, help="每币 4h 根数")
    ap.add_argument("--min-vol", type=float, default=3_000_000)
    ap.add_argument("--out", type=str, default=None, help="结果 JSON 输出路径")
    args = ap.parse_args()

    all_recs: list[dict] = []
    with httpx.Client() as client:
        syms = top_universe(client, args.symbols, args.min_vol)
        print(f"宇宙 {len(syms)} 币: {syms}")
        for i, sym in enumerate(syms, 1):
            try:
                bars = fetch_klines(client, sym, "4h", args.bars)
            except Exception as e:
                print(f"[{i}/{len(syms)}] {sym} 拉取失败: {str(e)[:80]}")
                continue
            if len(bars) < MIN_CLOSED + 2:
                print(f"[{i}/{len(syms)}] {sym} 样本不足 {len(bars)}")
                continue
            recs = replay_symbol(sym, bars)
            all_recs.extend(recs)
            nb = sum(1 for r in recs if r["both_blocked"])
            cf = sum(1 for r in recs if r["conflict"])
            print(f"[{i}/{len(syms)}] {sym} bars={len(bars)} 快照={len(recs)} "
                  f"全封={nb} 对撞={cf}")

    if not all_recs:
        print("无样本")
        return
    s = summarize(all_recs)
    print("\n" + "=" * 72)
    print(report(s))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": s}, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写: {args.out}")


if __name__ == "__main__":
    main()
