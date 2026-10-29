# -*- coding: utf-8 -*-
"""方向票 / 方向冲突闸门「软化」反事实复盘（2026-10-17）。

问题：把 votes（方向票同向 <DIR_VOTE_MIN 否决）与 direction_flip（1h 方向票
与 4h 闸门方向冲突否决）从「一票否决」改为「加分项」是否更好？

本脚本量化回答，并显式还原两个容易被忽略的机制事实：

1. **加分项 ≠ 软闸门**。质量分只是排序键 + AI 配额选择器
   （`ai_max_per_scan`，`ai_tasks` 按 quality desc, volume desc 取 top-N），
   生产链路上**没有 min_score 硬地板**。所以软化后：
   - 轮内候选数 ≤ 配额 → 闸门完全失效（等价全放开）；
   - 轮内候选数 > 配额 → 惩罚须大到足以挤出配额才近似硬门。
   因此必须把「配额还原」一并回放，只测裸信号集回答不了「会不会泄漏」。

2. **votes 门同时是方向判定器**。生产 `scanner` 用
   `direction = 1 if votes["net"] > 0 else -1` 定向——净差为 0 时**恒判空**
   （`scan.py _hit_direction` 的「趋势斜率兜底」实际失效：
   scanner 落库的 `trend_slope` 恒为 0.0）。软化 votes 门必须补 net=0 兜底，
   否则等于给所有三票分裂信号强加空头偏置。本脚本的软通道用
   「net=0 → 4h 闸门方向；闸门不可用则不出候选」兜底，并单独统计该类占比。

通道（3 个回放 pass + 离线配额/惩罚模拟）：
- `hard`：生产语义原样（harness 复刻）；另跑 **生产原函数**
  `detect_indicator_candidate` 做自检，逐信号比对 (symbol, ts, dir, quality)
  证明 harness 无口径漂移；
- `soft_votes`：votes 门软化（同向不足不拒，net=0 走 4h 方向兜底）；
- `soft_both`：再叠加 direction_flip 软化（与 4h 方向冲突不拒）。
  软通道的释放标记随 pending 快照传递——因软/硬两态下待确认池的创建集合不同，
  软态多出的信号有相当一部分来自「硬态根本没能进池」的回踩路径。

离线模拟（不需要重跑）：对释放信号施加惩罚 p（votes/flip 可分别设），并按
(quality, 24h 额) 在同一轮（同 ts）的全池内取 top-N，得到「配额还原组」。
惩罚/惩罚都是纯排序后处理，故一次回放可扫多组参数；配额 N 默认给
[ai_max_per_scan, 3, 1] 三档——回放宇宙（60 币）小于生产宇宙（全部 USDT 永续
≥3M 额），轮内池偏小、配额偏松，需用小 N 代偿「生产等效余量」两侧。

统计口径：突破类触发同占 AI 配额（进配额池），但近锚止损模型只对回踩确认
成立——故收益统计（n/胜率/期望R）仅计回踩确认（`in_scope`），配额选择用全池。

预注册判定（与 dirvote_structure_replay 同源）：n≥30 ∧ Wilson LB>40% ∧
期望R>0 ∧ 净期望R>0 ∧ top1 占比<40%。

口径局限：
- 信号池仅含回踩确认类触发（CONFIRM_TYPES，止损模型=近锚+0.3%）——突破类未纳入，
  真实轮内池更大 → 被软化的信号在真实里更易被配额挤出 → **本回放的配额泄漏
  估计偏乐观**（结论若显示「泄漏严重」，真实只会更严重；若显示「无泄漏」，
  须按此方向打折读）；
- retest 破轨事件不重建、无重复窗口去重——各通道同等受限；
- harness 与生产函数的一致性由自检通道给出（见报告「自检」段）。

用法（cwd=backend）：
    python scripts/gate_soft_replay.py --symbols 5 --days 30     # 冒烟
    python scripts/gate_soft_replay.py --symbols 60 --days 60    # 全量
"""
import argparse
import json
import math
import statistics
import sys
import time
from bisect import bisect_left
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402
from app.services.scanner import detect_indicator_candidate  # noqa: E402
from app.services.strategy import ema_gate as eg  # noqa: E402
from app.services.strategy import technical as tech  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.technical import HTF_SHORT  # noqa: E402
from app.services.strategy.trigger import PULLBACK_CONFIRM  # noqa: E402
from app.tasks.review_tasks import replay_suggestion  # noqa: E402

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
WINDOW = 300        # 1h 滑动窗（= 扫描 limit=max(kline_window, 300)）
FWD_BARS = 240      # 前向回放观察窗（10 天）
RR_TP1 = 1.5        # 止盈 = 1.5×止损距离（AI_RR_MIN 铁律下限）
STOP_BUF = 0.003    # 结构锚外 0.3%
FEE_RT = 0.0014     # 往返费率：taker 0.05%×2 + 滑点 0.02%×2
BREAKEVEN_WR = 1 / (1 + RR_TP1)  # 40%
CONFIRM_TYPES = {"trigger_pullback_confirm", "trigger_trend_pullback"}
VOL_WINDOW = 24     # 24h 额近似窗口（同 ts 配额排序的次级键）


# ── 数据获取（复用 dirvote_structure_replay 口径）───────────────────────

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
                 total: int, end_ms: Optional[int] = None) -> list:
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
    """当前 24h 成交额 top N 的 USDT 永续（幸存者偏差：宇宙取当前 top，非历史 top）"""
    rows = _get(client, f"{FAPI}/fapi/v1/ticker/24hr", {})
    usdt = [t for t in rows
            if t["symbol"].endswith("USDT")
            and float(t.get("quoteVolume") or 0) >= min_vol]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


class _StubPool:
    """detect_indicator_candidate 的 4h 数据源替身（get_klines(symbol, "4h", 300)）"""

    def __init__(self, htf: Optional[list]):
        self._htf = htf

    def get_klines(self, symbol, interval, limit):
        return self._htf


# ── 前向假想回放（与两个既有 replay 脚本同源）───────────────────────────

def _fwd_outcome(kl_all: list, t: int, direction: int) -> dict:
    """近锚止损 + 1.5R 固定止盈的假想单结果（返回 r/net_r/nbars，进不了场的 r=None）"""
    entry = float(kl_all[t][4])
    if entry <= 0:
        return {"r": None, "net_r": None, "nbars": None, "outcome": "bad_entry"}
    if direction > 0:
        stop = min(float(kl_all[j][3]) for j in range(t - 4, t + 1)) * (1 - STOP_BUF)
        dist = entry - stop
    else:
        stop = max(float(kl_all[j][2]) for j in range(t - 4, t + 1)) * (1 + STOP_BUF)
        dist = stop - entry
    if dist <= 0:
        # 近锚落在入场价错误侧（罕见几何）——放弃该假想单
        return {"r": None, "net_r": None, "nbars": None, "outcome": "bad_anchor"}
    tp1 = entry + direction * RR_TP1 * dist
    fwd = kl_all[t + 1: t + 1 + FWD_BARS]
    outcome, _, nbars = replay_suggestion(
        "long" if direction > 0 else "short", entry, stop, tp1, 0.0, fwd)
    r = RR_TP1 if outcome == "win_tp1" else (-1.0 if outcome == "loss" else None)
    stop_pct = dist / entry
    return {"r": r, "net_r": (r - FEE_RT / stop_pct) if r is not None else None,
            "nbars": nbars, "outcome": outcome}


def _vol24(kl_all: list, t: int) -> float:
    """信号根t的 24h 额近似（近 VOL_WINDOW 根已收盘 1h 报价额求和）"""
    lo = max(0, t + 1 - VOL_WINDOW)
    return sum(float(k[7]) for k in kl_all[lo: t + 1])


# ── harness：生产编排复刻，两个闸门可通过开关软化 ────────────────────────

def _min_same(votes: dict) -> int:
    return max(int(votes["bull_n"]), int(votes["bear_n"]))


def _slope_dir(kl: list) -> int:
    """趋势斜率符号兜底（生产 scan.py _hit_direction 的语义；此处用近 20 根
    已收盘收盘价的最小二乘斜率——生产落库 trend_slope 恒 0.0，该兜底实际失效，
    故软通道额外用 4h 闸门方向作为第一兜底）"""
    closed = [float(k[4]) for k in kl[:-1]][-20:]
    if len(closed) < 3:
        return 0
    n = len(closed)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(closed) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, closed)) / den
    return 1 if slope > 0 else -1 if slope < 0 else 0


def _harness_pass(symbol: str, kl_all: list, htf_all: list, flt,
                  soft_votes: bool, soft_flip: bool) -> list[dict]:
    """单币走历史：逐根复刻 detect_indicator_candidate 的编排。

    与生产的唯一差别就是两个闸门可按开关软化；其余（4h 合并入口 htf_context/
    range_score/trigger/白名单/质量分/pending 状态机）与 scanner 逐行同义。
    """
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend: Optional[dict] = None
    for t in range(WINDOW - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
        t_close = kl_all[t][0] + HOUR_MS  # 信号K线收盘时刻
        i = bisect_left(htf_opens, t_close)
        htf = htf_all[:i][-300:] or None
        htf_res = None
        if htf:
            # 4h 合并入口（2026-10-20 用户拍板：原 gate_4h + 状态机合并）
            try:
                htf_res = tech.htf_context(htf)
            except Exception:
                htf_res = None
            else:
                if htf_res is None or htf_res.get("reject_reason"):
                    # 闸门主动否决（异常路径 htf_res=None 降级放行）
                    if pend:
                        pend = None
                    continue
            if htf_res is not None and htf_res.get("block_both"):
                if pend:
                    pend = None
                continue
        # 4h 方向权威（原 gate_4h 的 long/short；htf_res=None 异常降级时无方向）
        gate_dir = htf_res.get("direction") if htf_res is not None else None

        votes = tech.compute_indicator_votes(kl, htf_bg=htf_res)
        votes_fail = (not votes) or (_min_same(votes) < tech.DIR_VOTE_MIN)

        if votes_fail and not soft_votes:
            if pend:
                pend = None
            continue

        # 方向：净差符号 → 软通道补齐 net=0 兜底（4h 闸门方向 → 斜率符号）
        direction = 0
        net_zero_fallback = False
        if votes and int(votes["net"]) != 0:
            direction = 1 if votes["net"] > 0 else -1
        elif soft_votes:
            net_zero_fallback = True
            if gate_dir is not None:
                direction = 1 if gate_dir == "long" else -1
            else:
                direction = _slope_dir(kl)
            if direction == 0:
                if pend:
                    pend = None
                continue
        else:
            direction = -1  # 生产语义：net==0 恒判空（此处不会到达：票不足已拒）

        if not votes:
            # 无任何可用指标：生产在 votes 门即拒，软态下也需要 votes 才能算质量分
            if pend:
                pend = None
            continue

        flip_conflict = (gate_dir is not None
                         and gate_dir != ("long" if direction == 1 else "short"))
        if flip_conflict and not soft_flip:
            if pend:
                pend = None
            continue

        rs, _ = eg.range_score(
            kl, gate_ev=htf_res.get("evidence") if htf_res else None)
        # 注：本脚本对 range_score 仍按旧口径「一律硬拒」（含回踩），线上 2026-10-21
        # 起回踩已改降权不拒——故此处 compute_quality_score 恒不传 range_penalty。
        # 两端口径差异属独立议题（会改变历史基线可比性），本次不在此处对齐。
        if rs >= eg.RANGE_SCORE_REJECT:
            if pend:
                pend = None
            continue

        trigd, pend_op = trig.detect_trigger(
            kl, direction, pend, flt=flt, retest=None, htf=htf,
            htf_dir=gate_dir)

        # 释放标记随 pending 传递（软态新进池的待确认，其确认根不一定复现旗标）
        origin = (votes_fail, flip_conflict, net_zero_fallback)
        prev_pend = pend
        if pend_op and pend_op.get("op") == "upsert":
            # 待确认池准入闸：回踩确认类型须在白名单内
            if PULLBACK_CONFIRM not in flt.allowed_triggers:
                pend = None
                continue
            pend = {
                "id": "sim", "direction": pend_op["direction"],
                "touch_ref": pend_op.get("touch_ref"),
                "stage1_candle_open": pend_op["stage1_candle_open"],
                "soft_origin": origin,
            }
            continue
        if pend_op:
            pend = None  # confirm/supersede/expire 终态

        if trigd is None:
            continue
        if trigd["type"] not in flt.allowed_triggers:
            continue

        # 次根确认（Stage-2）释放旗标取自 Stage-1 触碰根快照；当根确认取本根旗标
        if pend_op and pend_op.get("op") == "confirm" and prev_pend:
            vf, fc, nz = prev_pend.get("soft_origin") or origin
        else:
            vf, fc, nz = origin
        quality = tech.compute_quality_score(
            direction, votes, trigd["type"], klines=kl,
            stabilize=trigd.get("stabilize"), pattern=trigd.get("pattern"),
            golden_combo=bool(trigd.get("golden_combo")),
            swing_order=flt.swing_order,
            structure_gate=bool(trigd.get("structure_gate", True)),
            # 触点类型（2026-10-29，docs/23 §8）：不传则深回踩（ema55/ema144/
            # ema21_4h）拿不到质量分豁免，回放分数系统性低于线上
            touch_ref=trigd.get("touch_ref"))
        # 突破类同属生产候选（占 AI 配额），但近锚止损模型只对回踩确认成立——
        # 故全类型入库（配额池真实性），仅回踩确认计收益（in_scope）
        in_scope = trigd["type"] in CONFIRM_TYPES
        fw = _fwd_outcome(kl_all, t, direction) if in_scope else {
            "r": None, "net_r": None, "nbars": None, "outcome": "out_of_scope"}
        sigs.append({
            "ts": kl_all[t][0], "symbol": symbol, "dir": direction,
            "pattern": trigd.get("pattern"), "touch_ref": trigd.get("touch_ref"),
            "signal_type": trigd["type"], "in_scope": in_scope,
            "structure_gate": trigd.get("structure_gate", True),
            "net": (votes or {}).get("net"), "voted": (votes or {}).get("voted"),
            "vmax": _min_same(votes) if votes else 0,
            "gate_dir": gate_dir,
            "votes_fail": bool(vf), "flip_conflict": bool(fc),
            "net_zero_fallback": bool(nz),
            "quality": quality, "vol24": _vol24(kl_all, t),
            **fw,
        })
    return sigs


def _prod_pass(symbol: str, kl_all: list, htf_all: list, flt) -> list[dict]:
    """生产原函数 pass（自检用）：完全走 detect_indicator_candidate。"""
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend = None
    for t in range(WINDOW - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
        t_close = kl_all[t][0] + HOUR_MS
        i = bisect_left(htf_opens, t_close)
        htf = htf_all[:i][-300:] or None
        cand, op = detect_indicator_candidate(
            _StubPool(htf), symbol, kl, pending=pend, flt=flt, retest=None)
        if op:
            if op.get("op") == "upsert":
                pend = {"id": "sim", "direction": op["direction"],
                        "touch_ref": op.get("touch_ref"),
                        "stage1_candle_open": op["stage1_candle_open"]}
            else:
                pend = None
        if not cand:
            continue
        d = int(cand.get("direction")
                or (1 if (cand.get("votes") or {}).get("net", 0) > 0 else -1))
        sigs.append({"ts": kl_all[t][0], "symbol": symbol, "dir": d,
                     "quality": cand.get("quality_score"),
                     "signal_type": cand["signal_type"]})
    return sigs


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
    """收益统计——仅回踩确认（in_scope）；突破类只占配额、不计收益"""
    sigs = [s for s in sigs if s.get("in_scope", True)]
    decided = [s for s in sigs if s["r"] is not None]
    n, w = len(sigs), sum(1 for s in decided if s["r"] > 0)
    rs = [s["r"] for s in decided]
    net_rs = [s["net_r"] for s in decided]
    by_sym: dict[str, int] = {}
    for s in sigs:
        by_sym[s["symbol"]] = by_sym.get(s["symbol"], 0) + 1
    top_share = (max(by_sym.values()) / n) if (n and by_sym) else 0.0
    return {
        "n": n, "open": n - len(decided),
        "win_rate": (w / len(decided)) if decided else None,
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


def verdict(a: dict, min_n: int = 30) -> str:
    if a["n"] < min_n:
        return f"样本不足（{a['n']}<{min_n}）"
    ok = (a["wilson_lb"] > BREAKEVEN_WR and (a["exp_r"] or 0) > 0
          and (a["net_exp_r"] or 0) > 0 and a["top_symbol_share"] < 0.40)
    return "PASS" if ok else "FAIL"


# ── 离线配额 / 惩罚模拟（纯后处理：一次回放可扫多组参数）─────────────────

def _penalized(sigs: list[dict], p_votes: float, p_flip: float) -> list[dict]:
    """对释放信号施加惩罚（votes/flip 可分别设，两类同时命中则叠加）"""
    out = []
    for s in sigs:
        q = float(s["quality"])
        if s["votes_fail"]:
            q += p_votes
        if s["flip_conflict"]:
            q += p_flip
        out.append({**s, "eff_quality": q})
    return out


def _quota_filter(sigs: list[dict], quota: int) -> list[dict]:
    """同一轮（同 ts）全池内按 (eff_quality, vol24) 降序取 top-N —— ai_tasks 配额同源"""
    if quota <= 0:
        return sigs
    rounds: dict[int, list[dict]] = {}
    for s in sigs:
        rounds.setdefault(s["ts"], []).append(s)
    kept: list[dict] = []
    for _ts, rows in rounds.items():
        rows.sort(key=lambda x: (x.get("eff_quality", x["quality"]),
                                 x.get("vol24", 0)), reverse=True)
        kept += rows[:quota]
    return kept


def _row(name: str, sigs: list[dict]) -> str:
    a = agg(sigs)
    return (f"| {name} | {a['n']} | {len(sigs)} | {a['open']} "
            f"| {fmt(a['win_rate'], pct=True, nd=1)} "
            f"| {fmt(a['wilson_lb'], pct=True, nd=1)} | {fmt(a['exp_r'])} "
            f"| {fmt(a['net_exp_r'])} | {fmt(a['top_symbol_share'], pct=True, nd=0)} "
            f"| {fmt(a['median_bars'], nd=0)} | {verdict(a)} |")


def quota_diag(name: str, sigs: list[dict], quota: int) -> None:
    """配额绑定度诊断：惩罚是否真有杠杆，取决于轮内池能否超过配额"""
    rounds: dict[int, int] = {}
    for s in sigs:
        rounds[s["ts"]] = rounds.get(s["ts"], 0) + 1
    sizes = sorted(rounds.values())
    if not sizes:
        print(f"- {name}：无信号")
        return
    over = sum(1 for v in sizes if v > quota)
    p95 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.95))]
    print(f"- {name}：轮数 {len(sizes)}；轮内池 > {quota} 的轮次 {over}"
          f"（{over / len(sizes):.1%}）；池均值 {sum(sizes) / len(sizes):.2f}；"
          f"p95 {p95}；max {sizes[-1]}")


def _bucket(sigs: list[dict], keyfn) -> dict:
    buckets: dict = {}
    for s in sigs:
        buckets.setdefault(keyfn(s), []).append(s)
    return buckets


def _release_reason(s: dict) -> str:
    if s["votes_fail"] and s["flip_conflict"]:
        return "both"
    if s["votes_fail"]:
        return "votes"
    if s["flip_conflict"]:
        return "flip"
    return "other(pending路径)"


def report(hard: list, soft_votes: list, soft_both: list, quotas: list[int],
           penalties: list[float]) -> dict:
    """三组裸池 + 配额还原组 + 惩罚敏感性"""
    k_hard = {(s["symbol"], s["ts"]) for s in hard}
    gained_v = [s for s in soft_votes if (s["symbol"], s["ts"]) not in k_hard]
    k_softv = {(s["symbol"], s["ts"]) for s in soft_votes}
    k_softb = {(s["symbol"], s["ts"]) for s in soft_both}
    gained_b = [s for s in soft_both if (s["symbol"], s["ts"]) not in k_hard]
    gained_vb = [s for s in soft_both if (s["symbol"], s["ts"]) not in k_softv]

    print("\n## 一、裸信号池（不还原配额；「n」= 回踩确认计收益笔数，「池」= 含突破类的全候选）\n")
    print("| 组 | n | 池 | open | 胜率 | Wilson LB | 期望R | 净期望R | top1占比 | 中位K线数 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, sigs in (("hard（生产语义）", hard),
                       ("soft_votes（votes 软化）", soft_votes),
                       ("soft_both（votes+flip 软化）", soft_both),
                       ("· gained_votes（相对 hard 新增）", gained_v),
                       ("· gained_both（相对 hard 新增）", gained_b),
                       ("· gained_by_flip（叠加 flip 再增）", gained_vb)):
        print(_row(name, sigs))

    print(f"\n信号计数（含突破类）：hard {len(hard)} / soft_votes {len(soft_votes)}"
          f"（+{len(gained_v)}） / soft_both {len(soft_both)}（+{len(gained_b)}），"
          f"soft_both 相对 soft_votes +{len(gained_vb)}；"
          f"hard 反向丢失 {len([s for s in hard if (s['symbol'], s['ts']) not in k_softb])}"
          f"；计收益笔数 hard {agg(hard)['n']} / soft_both {agg(soft_both)['n']}")

    print("\n## 二、配额绑定度诊断（惩罚是否真有杠杆）\n")
    print(f"（回放宇宙 {len({s['symbol'] for s in hard})} 币；生产宇宙更大 → 真实轮内池更大、"
          "配额更易绑定，故下方同时给多档 N 兜住「生产等效余量」两侧）")
    for n in quotas:
        quota_diag(f"hard 全候选池 @N={n}", hard, n)
        quota_diag(f"soft_both 无惩罚池 @N={n}", soft_both, n)

    print("\n## 三、释放信号归因（two-pass 差异来源）\n")
    for name, sigs in (("gain(soft_votes)", gained_v), ("gain(soft_both)", gained_b)):
        parts = []
        for k, v in sorted(_bucket(sigs, _release_reason).items()):
            a = agg(v)
            parts.append(f"{k}={a['n']}笔 胜率{fmt(a['win_rate'], pct=True, nd=0)}"
                         f" 期望R{fmt(a['exp_r'])}")
        print(f"- {name}：" + "；".join(parts))
    nz = [s for s in soft_both if s["net_zero_fallback"]]
    print(f"- net=0 兜底信号（生产语义恒判空、软化后走 4h 方向）：{len(nz)} 笔"
          f"，胜率{fmt(agg(nz)['win_rate'], pct=True, nd=0)}，期望R{fmt(agg(nz)['exp_r'])}")
    print("- 方向分布（gain(soft_both)）：long="
          f"{sum(1 for s in gained_b if s['dir'] > 0)} / short="
          f"{sum(1 for s in gained_b if s['dir'] < 0)}")

    rows_out: dict = {}
    for n in quotas:
        print(f"\n### 四.{n} 配额还原 N={n}（同轮全池取 top-{n}，惩罚扫描）\n")
        print("| 组（votes罚 / flip罚） | n | 池 | open | 胜率 | Wilson LB | 期望R | 净期望R | top1占比 | 中位K线数 | 判定 |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        hard_q = _quota_filter(_penalized(hard, 0, 0), n)
        print(_row("hard（基准）", hard_q))
        if n not in rows_out:
            rows_out[n] = {"hard": agg(hard_q), "penalties": {}}
        for p in penalties:
            sv = _quota_filter(_penalized(soft_votes, -p, 0), n)
            sb = _quota_filter(_penalized(soft_both, -p, -p), n)
            print(_row(f"soft_votes（罚{-p:.0f} / 0）", sv))
            print(_row(f"soft_both（罚{-p:.0f} / 罚{-p:.0f}）", sb))
            rows_out[n]["penalties"][p] = {"soft_votes": agg(sv), "soft_both": agg(sb)}
        infinite = _quota_filter(_penalized(soft_both, 0, 0), n)
        print(_row("soft_both 无惩罚（等价全放开）", infinite))
        rows_out[n]["soft_both_unpenalized"] = agg(infinite)

    print(f"\n盈亏平衡胜率（1.5R）= {BREAKEVEN_WR:.0%}；费率假设 = 往返 {FEE_RT:.2%}；"
          f"配额档 N = {quotas}（生产默认 ai_max_per_scan={settings.AI_MAX_PER_SCAN}）；"
          f"惩罚 = 对释放信号的质量分扣减档 {[-p for p in penalties]}")
    print("读法：配额组与「hard 基准」的差异 = 软化后真实会进 AI 的池子的变化；"
          "与「无惩罚」组的差异 = 惩罚挤配额的有效性。若各惩罚档与无惩罚档完全相同，"
          "则说明该 N 下配额不绑定、加分项退化为全放开。")
    return rows_out


def selfcheck(prod: list[dict], harness: list[dict]) -> None:
    """harness-hard 与生产原函数逐信号比对（证明口径无漂移）"""
    pd = {(s["symbol"], s["ts"]): (s["dir"], s["quality"]) for s in prod}
    hd = {(s["symbol"], s["ts"]): (s["dir"], s["quality"]) for s in harness}
    only_p = set(pd) - set(hd)
    only_h = set(hd) - set(pd)
    diff = [k for k in (set(pd) & set(hd))
            if pd[k][0] != hd[k][0] or int(pd[k][1] or 0) != int(hd[k][1] or 0)]
    print("\n## 五、自检：harness-hard vs 生产 detect_indicator_candidate\n")
    print(f"- 生产 {len(pd)} 笔 / harness {len(hd)} 笔；仅生产 {len(only_p)} 笔；"
          f"仅 harness {len(only_h)} 笔；同键但 (dir,quality) 不一致 {len(diff)} 笔")
    for k in list(only_p)[:5]:
        print(f"  · 仅生产 {k} {pd[k]}")
    for k in list(only_h)[:5]:
        print(f"  · 仅 harness {k} {hd[k]}")
    for k in diff[:5]:
        print(f"  · 不一致 {k} 生产={pd[k]} harness={hd[k]}")
    if not (only_p or only_h or diff):
        print("- 结论：逐信号完全一致 → harness 编排与生产无口径漂移，"
              "软通道差异可归因于两个闸门开关。")


# ── 主流程 ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="方向票/方向冲突闸门软化回放（2026-10-17）")
    ap.add_argument("--symbols", type=int, default=60)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-vol", type=float, default=settings.MIN_VOLUME_24H)
    ap.add_argument("--quotas", default=None,
                    help="配额档，逗号分隔（默认 [ai_max_per_scan, 3, 1]——回放宇宙小于"
                         "生产宇宙，需用小 N 代偿生产等效余量）")
    ap.add_argument("--penalties", default="0,8,15,25",
                    help="释放信号惩罚档（正数，脚本内取负），逗号分隔")
    ap.add_argument("--skip-prod", action="store_true", help="跳过生产自检 pass（省时）")
    ap.add_argument("--out", default="/tmp/gate_soft_report.json")
    args = ap.parse_args()
    penalties = [float(x) for x in args.penalties.split(",") if x.strip()]
    quotas = ([int(x) for x in args.quotas.split(",")]
              if args.quotas else [settings.AI_MAX_PER_SCAN, 3, 1])

    flt = resolve_scan_filters(None)
    total_1h = args.days * 24
    client = httpx.Client()
    symbols = top_universe(client, args.symbols, args.min_vol)
    print(f"宇宙 {len(symbols)} 币（top by 24h 额 ≥{args.min_vol:,.0f}），"
          f"{args.days} 天 1h×{total_1h} 根 + 4h×600 根/币；配额档 N={quotas}")

    hard_all: list[dict] = []
    sv_all: list[dict] = []
    sb_all: list[dict] = []
    prod_all: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        t0 = time.time()
        try:
            kl = fetch_klines(client, sym, "1h", total_1h)
            htf = fetch_klines(client, sym, "4h", 600)
        except Exception as e:
            print(f"[{i}/{len(symbols)}] {sym} 拉取失败跳过: {e}")
            continue
        if len(kl) < WINDOW + 10:
            print(f"[{i}/{len(symbols)}] {sym} 历史1h不足({len(kl)})跳过")
            continue
        try:
            hard_all += _harness_pass(sym, kl, htf, flt, soft_votes=False, soft_flip=False)
            sv_all += _harness_pass(sym, kl, htf, flt, soft_votes=True, soft_flip=False)
            sb_all += _harness_pass(sym, kl, htf, flt, soft_votes=True, soft_flip=True)
            if not args.skip_prod:
                prod_all += _prod_pass(sym, kl, htf, flt)
        except Exception as e:
            print(f"[{i}/{len(symbols)}] {sym} 回放异常跳过: {e}")
            continue
        print(f"[{i}/{len(symbols)}] {sym}: hard "
              f"{sum(1 for s in hard_all if s['symbol'] == sym)} / soft_both "
              f"{sum(1 for s in sb_all if s['symbol'] == sym)} 笔"
              f"（{time.time() - t0:.1f}s）", flush=True)

    rows = report(hard_all, sv_all, sb_all, quotas, penalties)
    if prod_all:
        selfcheck(prod_all, hard_all)

    out = {"params": {"symbols": symbols, "days": args.days, "window": WINDOW,
                      "rr_tp1": RR_TP1, "fee_rt": FEE_RT, "quotas": quotas,
                      "penalties": penalties},
           "hard": hard_all, "soft_votes": sv_all, "soft_both": sb_all,
           "prod": prod_all, "quota_rows": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n明细已写 {args.out}")


if __name__ == "__main__":
    main()
