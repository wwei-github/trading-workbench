# -*- coding: utf-8 -*-
"""方向票拆角色（O9）+ 1H 结构门降级 · 历史反事实复盘（阶段0）。

背景（docs/21 O9，2026-10-15 拍板）：
- 方案二：方向判定由「7 票多数决 |net|≥3 ∧ voted≥5」收敛为「方向票
  （MACD/布林带/RSI）同向 ≥2 张」（`DIR_VOTE_MIN`）；ADX/OBV/SuperTrend/4h背景
  退出方向票。
- 方案一：1H 结构门在 1h EMA21 触点由**硬门否决**降级为**软校验**——不过门
  照常出信号、由质量分重罚 −15。

两类改动本质都是「放行更多信号」，本回放量化新增（marginal）信号的假信号风险，
预注册判定释放信号不劣化再上线。

回放口径（docs/26 §2/§5 同源）：完全复用生产管线 detect_indicator_candidate。
- 基线 = 修订前行为（两处 monkeypatch 复刻）：
    (a) votes 门槛还原为 7 票 |net|≥3 ∧ voted≥5（`technical._compute_all` 全指标
        统计，未过门时把 bull_n/bear_n 归零让 scanner 的 DIR_VOTE_MIN 检查天然否决）
    (b) 结构门否决还原：`detect_trigger` 遇 ema21 触点 structure_gate=False →
        旧语义（不产生信号 / 不 upsert，pending expire structure_1h）
- 变体 = 修订后生产行为
两组各跑同一历史，取边际信号（变体有、基线无）按近锚止损 + 1.5R 固定止盈做假想
回放，预注册判定：n≥30 且 Wilson LB > 盈亏平衡胜率（40%）且期望R>0 且净期望R>0
且 top1 占比 <40%。

已知口径差异（同 docs/26 §2）：
- 质量分变化（net×7→×10 / 结构门 −15 / SuperTrend −6）只影响 AI 配额排序，不改变
  信号集合，故不在本回放还原；
- retest 破轨事件不重建、无重复窗口去重——两组同等受限。

用法（cwd=backend）：
    python scripts/dirvote_structure_replay.py --symbols 5 --days 30    # 冒烟
    python scripts/dirvote_structure_replay.py --symbols 60 --days 60   # 全量
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
from app.services import scanner as scanner_mod  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402
from app.services.scanner import detect_indicator_candidate  # noqa: E402
from app.services.strategy import technical as tech  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
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
OLD_VOTE_MIN_ABS_NET = 3   # 修订前：7 票净差下限
OLD_VOTE_MIN_VOTED = 5     # 修订前：参与投票数下限

_ORIG_VOTES = scanner_mod.compute_indicator_votes
_ORIG_TRIGGER = scanner_mod.detect_trigger


# ── 数据获取（同 engulf_body_gate_replay）───────────────────────────────

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


# ── 基线复刻（修订前行为）：votes 门槛 + 结构门否决 ──────────────────────

def _old_votes(klines, htf_klines=None, htf_bg=None):
    """修订前 votes：全指标（含 SuperTrend/ADX/OBV/4h背景）净差；门槛 |net|≥3 ∧ voted≥5。

    未过门时把 bull_n/bear_n 归零——scanner 的 `max(bull_n,bear_n) < DIR_VOTE_MIN`
    检查即天然否决（与旧 scanner 的 votes reject 分支语义等价：拒绝并 expire pending）。
    """
    results = [(s, r) for _, s, r in tech._compute_all(klines, htf_klines, htf_bg) if r]
    if not results:
        return None
    bull = [s for s, r in results if int(r.get("dir") or 0) > 0]
    bear = [s for s, r in results if int(r.get("dir") or 0) < 0]
    net = len(bull) - len(bear)
    passed = abs(net) >= OLD_VOTE_MIN_ABS_NET and len(results) >= OLD_VOTE_MIN_VOTED
    if passed:
        return {"bull": bull, "bear": bear, "bull_n": len(bull), "bear_n": len(bear),
                "net": net, "voted": len(results), "results": results}
    return {"bull": bull, "bear": bear, "bull_n": 0, "bear_n": 0,
            "net": 0, "voted": len(results), "results": results}


def _old_trigger(*args, **kwargs):
    """修订前 trigger：1h EMA21 触点结构门不过 → 硬门否决（不产生信号 / 不 upsert）。

    - 行2 当根确认 / 行3-4 次根确认：trigger dict 带 structure_gate=False → 丢弃信号，
      pending 按旧死因 expire(structure_1h)。
    - 行5 转 pending：op=upsert 且 structure_gate=False → 旧语义 expire(structure_1h)。
    """
    t, op = _ORIG_TRIGGER(*args, **kwargs)
    if t is not None and t.get("structure_gate") is False:
        return None, {"op": "expire", "reason": "structure_1h"}
    if op and op.get("op") == "upsert" and op.get("structure_gate") is False:
        return t, {"op": "expire", "reason": "structure_1h"}
    return t, op


# ── 诊断探针：统计 (ema21 触点, 结构门 None) 联合分布（覆盖全部 bar）───────

SG_PROBE = {"ema21_touch": 0, "ema21_touch_struct_none": 0, "struct_none": 0}
_cur_struct_none = {"v": False}


def _install_sg_probe():
    """包装 trigger._detect_ema_touch / trigger.structure_1h 计数（透明，不改判定）。

    structure_1h 在每根 bar 先于触点探测调用，用 _cur_struct_none 记同根结果，
    在 _detect_ema_touch(period=21) 命中（返回非 None）时配对统计——用于回答
    「ema21 触点里结构门不过的比例」，避免只看信号 bar 导致的幸存者偏差。
    """
    orig_touch = trig._detect_ema_touch
    orig_struct = trig.structure_1h

    def _touch(klines, direction, period, flt):
        r = orig_touch(klines, direction, period, flt)
        if period == 21 and r is not None:
            SG_PROBE["ema21_touch"] += 1
            if _cur_struct_none["v"]:
                SG_PROBE["ema21_touch_struct_none"] += 1
        return r

    def _struct(k1, direction):
        r = orig_struct(k1, direction)
        _cur_struct_none["v"] = r is None
        if r is None:
            SG_PROBE["struct_none"] += 1
        return r

    trig._detect_ema_touch = _touch
    trig.structure_1h = _struct

    def _restore():
        trig._detect_ema_touch = orig_touch
        trig.structure_1h = orig_struct
    return _restore


# ── 单遍回放 ───────────────────────────────────────────────────────────

def run_pass(symbol: str, kl_all: list, htf_all: list, flt,
             baseline: bool) -> list[dict]:
    """跑一遍扫描管线回放，返回回踩确认信号 + 假想回放结果列表。

    baseline=True 挂两处 monkeypatch（修订前行为）；False 用修订后生产行为。
    """
    if baseline:
        scanner_mod.compute_indicator_votes = _old_votes
        scanner_mod.detect_trigger = _old_trigger
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
            # 归因元数据：released-votes / released-structure 分类用（变体侧有效）
            cand_votes = cand.get("votes") or {}
            res = cand_votes.get("results") or []
            o_bull = [s for s, rr in res if int((rr or {}).get("dir") or 0) > 0]
            o_bear = [s for s, rr in res if int((rr or {}).get("dir") or 0) < 0]
            o_net = len(o_bull) - len(o_bear)
            sigs.append({
                "ts": sig[0], "symbol": symbol, "dir": d,
                "pattern": cand.get("pattern"), "touch_ref": cand.get("touch_ref"),
                "signal_type": cand["signal_type"],
                "structure_gate": cand.get("structure_gate", True),
                "old_net": o_net, "old_voted": len(res),
                "new_net": cand_votes.get("net"),
                "outcome": outcome, "r": r,
                "net_r": (r - FEE_RT / stop_pct) if r is not None else None,
                "nbars": nbars, "quality": cand.get("quality_score"),
            })
        return sigs
    finally:
        scanner_mod.compute_indicator_votes = _ORIG_VOTES
        scanner_mod.detect_trigger = _ORIG_TRIGGER


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


def _release_reason(s: dict) -> str:
    """marginal 信号被旧门槛挡下的原因（变体 votes.results 反推旧口径）"""
    votes_fail = (abs(s["old_net"]) < OLD_VOTE_MIN_ABS_NET
                  or s["old_voted"] < OLD_VOTE_MIN_VOTED)
    struct_fail = s["structure_gate"] is False
    if votes_fail and struct_fail:
        return "both"
    if votes_fail:
        return "votes_gate"
    if struct_fail:
        return "structure_gate"
    return "other"


def print_report(base: list, var: list, marginal: list, dropped: list, min_n: int) -> None:
    groups = [("baseline（修订前：7票|net|≥3 ∧ 结构硬门）", base),
              ("variant（修订后生产：方向票≥2 ∧ 结构软校验）", var),
              ("marginal（修订新增释放：variant⊄baseline）", marginal)]
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

    print(f"\n信号计数：baseline {len(base)} / variant {len(var)} / "
          f"marginal {len(marginal)} / dropped {len(dropped)}")

    print("\n## marginal 细分\n")
    print("- 按释放原因：" + "；".join(
        f"{k}={agg(v)['n']}笔 胜率{fmt(agg(v)['win_rate'], pct=True, nd=0)}"
        f" 期望R{fmt(agg(v)['exp_r'])}"
        for k, v in sorted(_bucket(marginal, _release_reason).items())))
    for dim, key in (("形态", "pattern"), ("方向", "dir"), ("触点", "touch_ref"),
                     ("信号类型", "signal_type")):
        buckets = _bucket(marginal, lambda s, k=key: s.get(k))
        print(f"- 按{dim}：" + "；".join(
            f"{k}={agg(v)['n']}笔 胜率{fmt(agg(v)['win_rate'], pct=True, nd=0)}"
            for k, v in sorted(buckets.items(), key=lambda x: str(x[0]))))

    print(f"\n盈亏平衡胜率（1.5R）= {BREAKEVEN_WR:.0%}；"
          f"费率假设 = 往返 {FEE_RT:.2%}；明细 JSON 见 --out 路径")


def _bucket(sigs: list[dict], keyfn) -> dict:
    buckets: dict = {}
    for s in sigs:
        buckets.setdefault(keyfn(s), []).append(s)
    return buckets


# ── 主流程 ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="方向票拆角色 + 结构门降级回放评估（docs/21 O9）")
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--out", default="/tmp/dirvote_structure_report.json")
    args = ap.parse_args()

    flt = resolve_scan_filters(None)
    total_1h = args.days * 24
    client = httpx.Client()
    _restore_probe = _install_sg_probe()
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
        base_all += run_pass(sym, kl, htf, flt, baseline=True)
        var_all += run_pass(sym, kl, htf, flt, baseline=False)
        print(f"[{i}/{len(symbols)}] {sym}: "
              f"基线 {sum(1 for s in base_all if s['symbol'] == sym)} 笔 / "
              f"变体 {sum(1 for s in var_all if s['symbol'] == sym)} 笔", flush=True)

    base_keys = {(s["symbol"], s["ts"]) for s in base_all}
    var_keys = {(s["symbol"], s["ts"]) for s in var_all}
    marginal = [s for s in var_all if (s["symbol"], s["ts"]) not in base_keys]
    dropped = [s for s in base_all if (s["symbol"], s["ts"]) not in var_keys]

    print(f"\n信号计数：基线 {len(base_all)} / 变体 {len(var_all)} / "
          f"边际 {len(marginal)} / 反向丢失 {len(dropped)}")
    print_report(base_all, var_all, marginal, dropped, min_n=30)
    _restore_probe()
    tot = SG_PROBE["ema21_touch"]
    fail = SG_PROBE["ema21_touch_struct_none"]
    print(f"\n## 结构门绑定度诊断\n")
    print(f"- structure_1h 全 bar 不过门次数：{SG_PROBE['struct_none']}")
    print(f"- ema21 触点 bar 数：{tot}；其中同根结构门不过：{fail}"
          + (f"（{fail / tot:.1%}）" if tot else ""))

    out = {"params": {"symbols": symbols, "days": args.days, "window": WINDOW,
                      "rr_tp1": RR_TP1, "fee_rt": FEE_RT,
                      "old_vote_min": [OLD_VOTE_MIN_ABS_NET, OLD_VOTE_MIN_VOTED]},
           "baseline": base_all, "variant": var_all,
           "marginal": marginal, "dropped": dropped}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n明细已写 {args.out}")


if __name__ == "__main__":
    main()
