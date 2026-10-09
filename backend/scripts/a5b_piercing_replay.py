# -*- coding: utf-8 -*-
"""A5b 刺透线/乌云盖顶白名单回归 · 历史回放评估（docs/26）。

完全复用生产管线 detect_indicator_candidate（投票/4h 门/触点两段式全保留），
仅打桩两处：4h 数据源（按信号时刻切片的历史 K 线）与 GOLDEN_PATTERNS 白名单扩展
（piercing_line / dark_cloud_cover）。基线与变体各跑一遍同一历史，取边际信号
（变体有、基线无）按近锚止损 + 1.5R 固定止盈做假想回放，预注册规则判定
（docs/26 §5）。

用法（cwd=backend）：
    python scripts/a5b_piercing_replay.py --symbols 5 --days 30    # 冒烟
    python scripts/a5b_piercing_replay.py --symbols 80 --days 60   # 全量

已知口径差异（docs/26 §2）：retest 破轨事件不重建、无重复窗口去重——两组同等受限。
"""
import argparse
import json
import math
import statistics
import sys
import time
from bisect import bisect_left
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402
from app.services.scanner import detect_indicator_candidate  # noqa: E402
from app.services.strategy import candlestick as cs  # noqa: E402
from app.tasks.review_tasks import replay_suggestion  # noqa: E402

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
WINDOW = 240        # 1h 滑动窗（= 扫描 kline_window 默认 240）
FWD_BARS = 240      # 前向回放观察窗（10 天）
RR_TP1 = 1.5        # 止盈 = 1.5×止损距离（AI_RR_MIN 铁律下限）
STOP_BUF = 0.003    # 结构锚外 0.3%
SHADOW_GATE = 0.40  # 决断K线总影线门（与 DECISION_SHADOW_MAX_PCT 同口径）
FEE_RT = 0.0014     # 往返费率：taker 0.05%×2 + 滑点 0.02%×2
BREAKEVEN_WR = 1 / (1 + RR_TP1)  # 40%
VARIANT_PATTERNS = {"piercing_line", "dark_cloud_cover"}


# ── 数据获取 ───────────────────────────────────────────────────────────

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
                 total: int, end_ms: int | None = None) -> list:
    """向前分页拉取 total 根已收盘+当前根 K 线（升序）"""
    out: list = []
    end = end_ms
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


def top_universe(client: httpx.Client, n: int, min_vol: float) -> list[str]:
    """当前 24h 成交额 top N 的 USDT 永续（幸存者偏差见 docs/26 §6）"""
    rows = _get(client, f"{FAPI}/fapi/v1/ticker/24hr", {})
    usdt = [t for t in rows
            if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume") or 0) >= min_vol]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


class _StubPool:
    """detect_indicator_candidate 的 4h 数据源替身（get_klines(symbol, "4h", 200)）"""

    def __init__(self, htf: list | None):
        self._htf = htf

    def get_klines(self, symbol, interval, limit):
        return self._htf


# ── 单遍回放 ───────────────────────────────────────────────────────────

def _gate_ok(kl: list) -> bool:
    """决断K线（信号K线 kl[-2]）总影线 ≤ 全长 40%（吞没/启明星同口径）"""
    k = kl[-2]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    rng = h - l
    if rng <= 0:
        return False
    shadow = (h - max(o, c)) + (min(o, c) - l)
    return shadow / rng <= SHADOW_GATE


def run_pass(symbol: str, kl_all: list, htf_all: list, flt,
             golden_extra: set | None) -> list[dict]:
    """跑一遍扫描管线回放，返回 PULLBACK_CONFIRM 信号 + 假想回放结果列表"""
    htf_opens = [k[0] for k in htf_all]
    orig_golden = cs.GOLDEN_PATTERNS
    if golden_extra:
        cs.GOLDEN_PATTERNS = orig_golden | golden_extra
    try:
        sigs: list[dict] = []
        pend: dict | None = None
        event: dict = {}
        # 滑窗：kl[-1]=“未收盘”（生产语义），信号K线=kl[-2]=kl_all[t]
        for t in range(WINDOW - 1, len(kl_all) - 1):
            kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
            t_close = kl_all[t][0] + HOUR_MS  # 信号K线收盘时刻
            # 4h 切片：开盘时间 < 信号收盘时刻（末根未收盘被 analyze_htf_background 内部剔除，
            # 与生产扫描时刻的 4h 窗口语义一致，无未来数据）
            i = bisect_left(htf_opens, t_close)
            htf = htf_all[:i][-200:] or None
            cand, op = detect_indicator_candidate(
                _StubPool(htf), symbol, kl,
                pending=pend, flt=flt, retest=None, event_out=event)
            # pending 状态机：upsert op 即快照（direction/touch_ref/stage1_candle_open），
            # 终态（expire/confirm/supersede）即清——与 scanner 落库语义一致
            if op:
                if op.get("op") == "upsert":
                    pend = {"id": "sim", "direction": op["direction"],
                            "touch_ref": op.get("touch_ref"),
                            "stage1_candle_open": op["stage1_candle_open"],
                            "ema21": op.get("ema21")}
                else:
                    pend = None
            if not cand or cand["signal_type"] != "trigger_pullback_confirm":
                continue
            sig = kl_all[t]
            entry = float(sig[4])
            if entry <= 0:
                continue
            # 候选 dict 无 direction 键（生产由调用侧知道方向）：从多数决快照取符号
            d = 1 if (cand.get("votes") or {}).get("net", 0) > 0 else -1
            if d > 0:
                stop = min(float(kl_all[j][3]) for j in range(t - 4, t + 1)) * (1 - STOP_BUF)
                dist = entry - stop
            else:
                stop = max(float(kl_all[j][2]) for j in range(t - 4, t + 1)) * (1 + STOP_BUF)
                dist = stop - entry
            if dist <= 0:
                continue  # 近锚在入场价上方（罕见几何），放弃该假想单
            tp1 = entry + d * RR_TP1 * dist
            fwd = kl_all[t + 1: t + 1 + FWD_BARS]
            outcome, _, nbars = replay_suggestion(
                "long" if d > 0 else "short", entry, stop, tp1, 0.0, fwd)
            r = RR_TP1 if outcome == "win_tp1" else (-1.0 if outcome == "loss" else None)
            stop_pct = dist / entry
            sigs.append({
                "ts": sig[0], "symbol": symbol, "dir": d,
                "pattern": cand.get("pattern"), "touch_ref": cand.get("touch_ref"),
                "gate": _gate_ok(kl), "outcome": outcome, "r": r,
                "net_r": (r - FEE_RT / stop_pct) if r is not None else None,
                "nbars": nbars, "quality": cand.get("quality_score"),
            })
        return sigs
    finally:
        cs.GOLDEN_PATTERNS = orig_golden


# ── 统计与报表 ─────────────────────────────────────────────────────────

def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - rad) / denom


def agg(sigs: list[dict]) -> dict:
    decided = [s for s in sigs if s["r"] is not None]
    n, w = len(sigs), sum(1 for s in decided if s["r"] > 0)
    rs = [s["r"] for s in decided]
    net_rs = [s["net_r"] for s in decided]
    by_sym: dict[str, int] = {}
    for s in sigs:
        by_sym[s["symbol"]] = by_sym.get(s["symbol"], 0) + 1
    top_share = (max(by_sym.values()) / n) if (n and by_sym) else 0.0
    return {
        "n": n, "open": n - len(decided), "win_rate": (w / len(decided)) if decided else None,
        "wilson_lb": wilson_lb(w, len(decided)),
        "exp_r": statistics.mean(rs) if rs else None,
        "net_exp_r": statistics.mean(net_rs) if net_rs else None,
        "top_symbol_share": top_share,
        "median_bars": statistics.median([s["nbars"] for s in decided]) if decided else None,
    }


def fmt(v, pct=False, nd=2) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def verdict(a: dict, min_n: int) -> str:
    if a["n"] < min_n:
        return f"样本不足（{a['n']}<{min_n}）"
    ok = (a["wilson_lb"] > BREAKEVEN_WR and (a["exp_r"] or 0) > 0
          and (a["net_exp_r"] or 0) > 0 and a["top_symbol_share"] < 0.40)
    return "PASS" if ok else "FAIL"


def print_report(base: list, marginal_raw: list, marginal_gated: list,
                 min_n: int) -> None:
    groups = [("baseline（现行 6 形态）", base), ("marginal_raw（变体新增）", marginal_raw),
              ("marginal_gated（+40% 影线门，主判定）", marginal_gated)]
    print("\n## 分组汇总（判定规则 docs/26 §5）\n")
    print("| 组 | n | open | 胜率 | Wilson LB | 期望R | 净期望R | top1占比 | 中位K线数 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, sigs in groups:
        a = agg(sigs)
        print(f"| {name} | {a['n']} | {a['open']} | {fmt(a['win_rate'], pct=True, nd=1)} "
              f"| {fmt(a['wilson_lb'], pct=True, nd=1)} | {fmt(a['exp_r'])} "
              f"| {fmt(a['net_exp_r'])} | {fmt(a['top_symbol_share'], pct=True, nd=0)} "
              f"| {fmt(a['median_bars'], nd=0)} | {verdict(a, min_n) if 'gated' in name else '-'} |")

    print("\n## marginal_gated 细分\n")
    for dim, key in (("形态", "pattern"), ("方向", "dir"), ("触点", "touch_ref")):
        buckets: dict = {}
        for s in marginal_gated:
            buckets.setdefault(s.get(key), []).append(s)
        print(f"- 按{dim}：" + "；".join(
            f"{k}={agg(v)['n']}笔 胜率{fmt(agg(v)['win_rate'], pct=True, nd=0)}"
            for k, v in sorted(buckets.items(), key=lambda x: str(x[0]))))

    print(f"\n盈亏平衡胜率（1.5R）= {BREAKEVEN_WR:.0%}；"
          f"费率假设 = 往返 {FEE_RT:.2%}；明细 JSON 见 --out 路径")


# ── 主流程 ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="A5b 刺透/乌云盖顶白名单回归回放（docs/26）")
    ap.add_argument("--symbols", type=int, default=80)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--out", default="/tmp/a5b_piercing_report.json")
    args = ap.parse_args()

    flt = resolve_scan_filters(None)
    total_1h = args.days * 24
    client = httpx.Client()
    symbols = top_universe(client, args.symbols, args.min_vol)
    print(f"宇宙 {len(symbols)} 币（top by 24h 额 ≥{args.min_vol:,.0f}），"
          f"{args.days} 天 1h×{total_1h} 根 + 4h×600 根/币")

    base_all: list[dict] = []
    var_all: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        try:
            kl = fetch_klines(client, sym, "1h", total_1h)
            htf = fetch_klines(client, sym, "4h", 600)
        except Exception as e:
            print(f"[{i}/{len(symbols)}] {sym} 拉取失败跳过: {e}")
            continue
        if len(kl) < WINDOW + 10:
            print(f"[{i}/{len(symbols)}] {sym} 历史1h不足({len(kl)})跳过")
            continue
        base_all += run_pass(sym, kl, htf, flt, None)
        var_all += run_pass(sym, kl, htf, flt, VARIANT_PATTERNS)
        print(f"[{i}/{len(symbols)}] {sym}: "
              f"基线confirm {sum(1 for s in base_all if s['symbol'] == sym)} 笔 / "
              f"变体 {sum(1 for s in var_all if s['symbol'] == sym)} 笔")

    base_keys = {(s["symbol"], s["ts"]) for s in base_all}
    marginal_raw = [s for s in var_all if (s["symbol"], s["ts"]) not in base_keys]
    marginal_gated = [s for s in marginal_raw if s["gate"]]

    print(f"\n信号计数：基线 {len(base_all)} / 变体 {len(var_all)} / "
          f"边际 raw {len(marginal_raw)} / 边际 gated {len(marginal_gated)}")
    print_report(base_all, marginal_raw, marginal_gated, min_n=30)

    out = {"params": {"symbols": symbols, "days": args.days, "window": WINDOW,
                      "rr_tp1": RR_TP1, "fee_rt": FEE_RT},
           "baseline": base_all, "marginal_raw": marginal_raw,
           "marginal_gated": marginal_gated}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n明细已写 {args.out}")


if __name__ == "__main__":
    main()
