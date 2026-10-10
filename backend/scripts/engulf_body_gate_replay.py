# -*- coding: utf-8 -*-
"""吞没实体基线门修订（被吞没方 body1 门移除）· 历史回放评估。

背景：线上 WUSDT 2026-10-10 20:00 大阳吞小阴（被吞没方实体仅近 20 根均实体
24%）因 body1 基线门整门误拒，回踩 EMA21 企稳信号漏扫。修订：实体基线门只
约束吞没K线（body2），被吞没方不设基线（大实体吞小十字=最强变体）。

回放口径（docs/26 §2/§5 同源）：完全复用生产管线 detect_indicator_candidate
（投票/4h 门/触点两段式全保留）。
- 基线 = 修订前行为：production 之上后置 body1 基线门（strict 包装，与旧代码
  逐门 AND 语义等价）
- 变体 = 修订后生产行为
两组各跑同一历史，取边际信号（变体有、基线无）按近锚止损 + 1.5R 固定止盈
做假想回放，预注册判定：n≥30 且 Wilson LB > 盈亏平衡胜率（40%）且期望R>0
且净期望R>0 且 top1 占比 <40%。

已知口径差异（同 docs/26 §2）：retest 破轨事件不重建、无重复窗口去重——两组同等受限。

用法（cwd=backend）：
    python scripts/engulf_body_gate_replay.py --symbols 5 --days 30    # 冒烟
    python scripts/engulf_body_gate_replay.py --symbols 60 --days 60   # 全量
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
WINDOW = 300        # 1h 滑动窗（= 扫描 limit=max(kline_window, 300)）
FWD_BARS = 240      # 前向回放观察窗（10 天）
RR_TP1 = 1.5        # 止盈 = 1.5×止损距离（AI_RR_MIN 铁律下限）
STOP_BUF = 0.003    # 结构锚外 0.3%
FEE_RT = 0.0014     # 往返费率：taker 0.05%×2 + 滑点 0.02%×2
BREAKEVEN_WR = 1 / (1 + RR_TP1)  # 40%
# 回踩确认两类触发（2026-10-10 融合后趋势回踩与普通回踩同一触点/金K机制）
CONFIRM_TYPES = {"trigger_pullback_confirm", "trigger_trend_pullback"}


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
    """detect_indicator_candidate 的 4h 数据源替身（get_klines(symbol, "4h", 300)）"""

    def __init__(self, htf: list | None):
        self._htf = htf

    def get_klines(self, symbol, interval, limit):
        return self._htf


# ── 基线复刻：修订前 body1 基线门（production 之上后置过滤，AND 语义等价）──

def _strict_engulfing(orig):
    def _wrapped(klines, idx=-1):
        r = orig(klines, idx)
        if r is None or not str(r.get("pattern") or "").endswith("engulfing"):
            return r
        pos = idx if idx >= 0 else len(klines) + idx
        k_prev = klines[pos - 1]
        body1 = abs(float(k_prev[1]) - float(k_prev[4]))
        base = [abs(float(k[1]) - float(k[4]))
                for k in klines[max(0, pos - 1 - cs.ENGULF_BODY_LOOKBACK): pos - 1]]
        if len(base) < cs.ENGULF_BODY_MIN_BARS:
            return None
        avg = sum(base) / len(base)
        if avg <= 0 or body1 < avg * cs.ENGULF_BODY_MIN_RATIO:
            return None
        return r
    return _wrapped


# ── 单遍回放 ───────────────────────────────────────────────────────────

def run_pass(symbol: str, kl_all: list, htf_all: list, flt,
             strict: bool) -> list[dict]:
    """跑一遍扫描管线回放，返回回踩确认信号 + 假想回放结果列表。

    strict=True 挂基线包装（修订前行为）；False 用修订后生产行为。
    """
    orig = cs.detect_engulfing
    if strict:
        cs.detect_engulfing = _strict_engulfing(orig)
    try:
        htf_opens = [k[0] for k in htf_all]
        sigs: list[dict] = []
        pend: dict | None = None
        # 滑窗：kl[-1]="未收盘"（生产语义），信号K线=kl[-2]=kl_all[t]
        for t in range(WINDOW - 1, len(kl_all) - 1):
            kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
            t_close = kl_all[t][0] + HOUR_MS  # 信号K线收盘时刻
            # 4h 切片：开盘时间 < 信号收盘时刻（无未来数据，与生产扫描口径一致）
            i = bisect_left(htf_opens, t_close)
            htf = htf_all[:i][-300:] or None
            cand, op = detect_indicator_candidate(
                _StubPool(htf), symbol, kl,
                pending=pend, flt=flt, retest=None)
            # pending 状态机：upsert op 即快照，终态即清（与 scanner 落库语义一致）
            if op:
                if op.get("op") == "upsert":
                    pend = {"id": "sim", "direction": op["direction"],
                            "touch_ref": op.get("touch_ref"),
                            "stage1_candle_open": op["stage1_candle_open"],
                            "ema21": op.get("ema21")}
                else:
                    pend = None
            if not cand or cand["signal_type"] not in CONFIRM_TYPES:
                continue
            sig = kl_all[t]
            entry = float(sig[4])
            if entry <= 0:
                continue
            # 候选 dict 带 direction（scanner 落库同源）；votes 快照兜底
            d = int(cand.get("direction")
                    or (1 if (cand.get("votes") or {}).get("net", 0) > 0 else -1))
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
                "signal_type": cand["signal_type"],
                "outcome": outcome, "r": r,
                "net_r": (r - FEE_RT / stop_pct) if r is not None else None,
                "nbars": nbars, "quality": cand.get("quality_score"),
            })
        return sigs
    finally:
        cs.detect_engulfing = orig


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


def print_report(base: list, marginal: list, min_n: int) -> None:
    groups = [("baseline（修订前：body1+body2 双实体门）", base),
              ("marginal（修订新增：仅 body1 门放宽释放）", marginal)]
    print("\n## 分组汇总（判定规则 docs/26 §5 同源）\n")
    print("| 组 | n | open | 胜率 | Wilson LB | 期望R | 净期望R | top1占比 | 中位K线数 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, sigs in groups:
        a = agg(sigs)
        mark = verdict(a, min_n) if "marginal" in name else "-"
        print(f"| {name} | {a['n']} | {a['open']} | {fmt(a['win_rate'], pct=True, nd=1)} "
              f"| {fmt(a['wilson_lb'], pct=True, nd=1)} | {fmt(a['exp_r'])} "
              f"| {fmt(a['net_exp_r'])} | {fmt(a['top_symbol_share'], pct=True, nd=0)} "
              f"| {fmt(a['median_bars'], nd=0)} | {mark} |")

    print("\n## marginal 细分\n")
    for dim, key in (("形态", "pattern"), ("方向", "dir"), ("触点", "touch_ref"),
                     ("信号类型", "signal_type")):
        buckets: dict = {}
        for s in marginal:
            buckets.setdefault(s.get(key), []).append(s)
        print(f"- 按{dim}：" + "；".join(
            f"{k}={agg(v)['n']}笔 胜率{fmt(agg(v)['win_rate'], pct=True, nd=0)}"
            for k, v in sorted(buckets.items(), key=lambda x: str(x[0]))))

    print(f"\n盈亏平衡胜率（1.5R）= {BREAKEVEN_WR:.0%}；"
          f"费率假设 = 往返 {FEE_RT:.2%}；明细 JSON 见 --out 路径")


# ── 主流程 ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="吞没 body1 基线门修订回放评估")
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--out", default="/tmp/engulf_body_gate_report.json")
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
        base_all += run_pass(sym, kl, htf, flt, strict=True)
        var_all += run_pass(sym, kl, htf, flt, strict=False)
        print(f"[{i}/{len(symbols)}] {sym}: "
              f"基线 {sum(1 for s in base_all if s['symbol'] == sym)} 笔 / "
              f"变体 {sum(1 for s in var_all if s['symbol'] == sym)} 笔", flush=True)

    base_keys = {(s["symbol"], s["ts"]) for s in base_all}
    marginal = [s for s in var_all if (s["symbol"], s["ts"]) not in base_keys]

    print(f"\n信号计数：基线 {len(base_all)} / 变体 {len(var_all)} / 边际 {len(marginal)}")
    print_report(base_all, marginal, min_n=30)

    out = {"params": {"symbols": symbols, "days": args.days, "window": WINDOW,
                      "rr_tp1": RR_TP1, "fee_rt": FEE_RT},
           "baseline": base_all, "variant": var_all, "marginal": marginal}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n明细已写 {args.out}")


if __name__ == "__main__":
    main()
