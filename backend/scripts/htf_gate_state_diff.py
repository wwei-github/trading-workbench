# -*- coding: utf-8 -*-
"""4h 合并 Golden Diff 验收：htf_context（合并入口）vs 旧（gate_4h + analyze_htf_background）。

旧侧 gate_4h 已于合并后从 ema_gate 删除（生产统一到 htf_context），此脚本改用
`scripts/legacy_gate_4h.py` 的冻结参考实现——本脚本是合并的验收/回归工具。

P0 合并的验收标准是「决策零变化」：同一 4h 快照上，合并入口给出的
「可开方向 / 漏斗关卡」必须与旧两道门逐点一致。

口径（P0「行为中性」验收）：
- 输入 Binance USDT 永续 4h K 线，评估时点=第 t 根已收盘（向函数传 bars[:t+2]，
  末根视作未收盘被内部剔除），与生产扫描同口径；
- 交易决策 = reject（不产生候选：闸门拒 ∨ 状态锁）vs open（放行并带方向）；
  **交易决策 + 方向必须逐点一致** —— 这是「行为中性」的硬指标；
- 两类合并预期内变化（交易决策不变，单独计数、不计为失败）：
  1. 关卡改判：同为拒绝时，统一判据把「间距/锯齿缠绕」的否决从 htf_state_lock
     前移到 gate_4h（正是合并要消除的「闸门放行而状态机缠绕」对撞）；
  2. gate 已拒快照的 state 因「缠绕并集」由 aligned_*/recover 变 tangle
     （这些快照过不了闸门，状态永不进入消费面）；
- 同为放行时，state/dir/证据 direction(up/down) 必须完全一致；
- core 透传等价：htf_context(k4, core) == htf_context(k4)。

退出码：非 0 表示出现非预期 diff（交易决策/方向/放行态状态/证据不一致）。

用法（cwd=backend）：
    python scripts/htf_gate_state_diff.py --symbols 60 --bars 900
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy import technical as tech       # noqa: E402
from app.services.strategy.technical import (             # noqa: E402
    STATE_BLOCK_BOTH, htf_core, analyze_htf_background, htf_context,
)
# 旧 gate_4h 已随合并从 ema_gate 删除；此验收脚本自带冻结参考实现作「旧侧」
from legacy_gate_4h import legacy_gate_4h                 # noqa: E402

FAPI = "https://fapi.binance.com"
MIN_CLOSED = 200


def _get(client, url, params):
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


def fetch_klines(client, symbol, total):
    out, end = [], None
    while len(out) < total:
        limit = min(1500, total - len(out))
        params = {"symbol": symbol, "interval": "4h", "limit": limit}
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


def top_universe(client, n, min_vol):
    rows = _get(client, f"{FAPI}/fapi/v1/ticker/24hr", {})
    usdt = [t for t in rows if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume") or 0) >= min_vol]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


def old_view(gate, bg):
    """旧链路（gate_4h + analyze）→ (交易决策, 方向, 关卡)。

    交易决策 reject = 不产生候选（scanner L311/L319 两条前置门均 return None）；
    open = 放行并带方向。关卡仅作漏斗归因，不改变交易决策。
    """
    if gate is None:
        return "reject", None, "gate_4h"
    if bg is not None and bg["state"] in STATE_BLOCK_BOTH:
        return "reject", None, "htf_state_lock"
    return "open", gate[0], "pass"


def new_view(ctx):
    """合并入口 → (交易决策, 方向, 关卡)（口径同 old_view）。"""
    if ctx is None or ctx["reject_reason"]:
        return "reject", None, "gate_4h"
    if ctx["block_both"]:
        return "reject", None, "htf_state_lock"
    return "open", ctx["direction"], "pass"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--bars", type=int, default=900)
    ap.add_argument("--min-vol", type=float, default=3_000_000)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    n_snap = 0
    diffs = []                 # 非预期 diff（交易决策/方向/放行态状态）
    expected_state_change = 0  # 预期内：gate 已拒 + 并集使 state 变 tangle
    exp_by_state = {}
    stage_relabel = 0          # 预期内：同为拒绝，关卡由 htf_state_lock 改判 gate_4h
    relabel_by_old_state = {}
    evidence_mismatch = 0

    with httpx.Client() as client:
        syms = top_universe(client, args.symbols, args.min_vol)
        print(f"宇宙 {len(syms)} 币")
        for i, sym in enumerate(syms, 1):
            try:
                bars = fetch_klines(client, sym, args.bars)
            except Exception as e:
                print(f"[{i}/{len(syms)}] {sym} 拉取失败: {str(e)[:70]}")
                continue
            if len(bars) < MIN_CLOSED + 2:
                continue
            for t in range(MIN_CLOSED - 1, len(bars) - 1):
                k4 = bars[:t + 2]
                try:
                    core = htf_core(k4)
                    if core is None:
                        continue
                    g = legacy_gate_4h(k4, core=core)
                    bg = analyze_htf_background(k4, core=core)
                    ctx = htf_context(k4, core=core)
                    ctx_self = htf_context(k4)   # core 透传等价校验
                except Exception as e:
                    diffs.append({"symbol": sym, "ts": bars[t][0],
                                  "kind": "exception", "detail": str(e)[:120]})
                    continue
                n_snap += 1

                o_old, d_old, s_old = old_view(g, bg)
                o_new, d_new, s_new = new_view(ctx)

                # 1) 交易决策 + 方向必须逐点一致（P0 的核心验收口径）
                if (o_old, d_old) != (o_new, d_new):
                    diffs.append({"symbol": sym, "ts": bars[t][0], "kind": "decision",
                                  "old": [o_old, d_old, s_old],
                                  "new": [o_new, d_new, s_new]})
                    continue
                if ctx_self != ctx:
                    diffs.append({"symbol": sym, "ts": bars[t][0],
                                  "kind": "core_passthrough"})
                    continue

                # 2) 关卡归因：同为拒绝时，统一判据把「间距/锯齿缠绕」的否决
                #    从状态锁(hft_state_lock)前移到闸门(gate_4h)——正是合并要消除的
                #    「闸门放行而状态机缠绕」对撞，交易决策不变 → 预期内，单独计数
                if s_old != s_new:
                    if o_old == "reject" and s_old == "htf_state_lock" \
                            and s_new == "gate_4h":
                        stage_relabel += 1
                        k = bg["state"] if bg is not None else "?"
                        relabel_by_old_state[k] = relabel_by_old_state.get(k, 0) + 1
                    else:
                        diffs.append({"symbol": sym, "ts": bars[t][0], "kind": "stage",
                                      "old": s_old, "new": s_new})
                    continue

                # 3) 同为放行：方向证据、状态、锁逆势方向必须完全一致
                if o_old == "open":
                    if g is not None and g[1].get("direction") \
                            != ctx["evidence"].get("direction"):
                        evidence_mismatch += 1
                    if bg is not None and ctx.get("state") is not None:
                        if bg["state"] != ctx["state"] or bg["dir"] != ctx["dir"]:
                            diffs.append({"symbol": sym, "ts": bars[t][0],
                                          "kind": "state_on_pass",
                                          "old": bg["state"], "new": ctx["state"]})
                else:
                    # 同为拒绝（gate_4h 关卡）：state 因并集由 aligned/recover 变
                    # tangle 属预期内（该快照过不了闸门，状态永不进入消费面）
                    if bg is not None and ctx.get("state") != bg["state"]:
                        wraps = tech._wrap_count(core["closes"], core["e144"])
                        if wraps > tech.TANGLE_WRAP_MAX and ctx["state"] == "tangle":
                            expected_state_change += 1
                            exp_by_state[bg["state"]] = \
                                exp_by_state.get(bg["state"], 0) + 1
                        else:
                            diffs.append({"symbol": sym, "ts": bars[t][0],
                                          "kind": "state_on_reject",
                                          "old": bg["state"], "new": ctx["state"]})
            print(f"[{i}/{len(syms)}] {sym} 累计快照={n_snap} 非预期diff={len(diffs)} "
                  f"关卡改判={stage_relabel} 预期state变tangle={expected_state_change}")

    s = {
        "n_snapshots": n_snap,
        "unexpected_diffs": len(diffs),
        "expected_stage_relabel_gate_4h": stage_relabel,
        "expected_stage_relabel_by_old_state": relabel_by_old_state,
        "expected_state_to_tangle": expected_state_change,
        "expected_state_to_tangle_by_state": exp_by_state,
        "evidence_direction_mismatch": evidence_mismatch,
        "diff_examples": diffs[:20],
    }
    print("\n" + "=" * 72)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if args.out:
        json.dump(s, open(args.out, "w"), ensure_ascii=False, indent=2)
    ok = len(diffs) == 0 and evidence_mismatch == 0
    print(("GOLDEN DIFF: PASS（交易决策/方向/放行态状态逐点零 diff；"
           "关卡改判与 state→tangle 为合并预期内）" if ok else "GOLDEN DIFF: FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
