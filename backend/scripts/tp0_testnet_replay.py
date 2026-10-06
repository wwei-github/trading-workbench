# -*- coding: utf-8 -*-
"""TP0 前置微止盈 · 测试网历史数据回放（docs/15 §4.3 实证，原 25/27 合并编号）。

用币安 USDT 永续**测试网**公开 K 线（testnet.binancefuture.com/fapi/v1/klines，
公开端点无需密钥），对每笔假想单做「基线 vs TP0 增强」双口径回放：

基线口径（现状）：SL 全损 -1R；TP1(50%) 出场后 SL 移保本 → 复刻 replay_suggestion
  的逐 K 序：先触 SL=loss(-1)；触 TP1 后（保本武装）再触 SL=win_tp1(+1.5R)。
TP0 增强口径（docs/15 §4.2）：TP0=entry+0.5R 出 25%（+0.125R）→ SL 立即移保本；
  剩余仓复刻基线逻辑（SL≈保本价 → ≈0R 记 loss_be≈0；TP1 → +1.5R×0.75+0.125）。
  变体半保本锁利：SL 移 entry+0.3R（被扫 +0.3R×0.75+0.125，回撤继续走按基线）。

挂单门控（docs/15 §4.2.4）：TP1_R ≥ 1.2R 才挂 TP0；≤ 门控走基线。
样本：信号 = 生产管线 detect_indicator_candidate 的 PULLBACK_CONFIRM 假想单
（复用 A5b 回放脚本模式，docs/15 §4.2.3 是「21 笔真实止损单重放」，本脚本是
「测试网近期全历史信号重放」，样本独立，只看相对差）。

用法（cwd=backend）：python scripts/tp0_testnet_replay.py --symbols 40 --days 30
"""
import argparse
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

FAPI = "https://testnet.binancefuture.com"  # 测试网公开行情（与生产路径一致）
HOUR_MS = 3_600_000
WINDOW = 240
FWD_BARS = 240
TP0_R = 1.0          # TP0 = entry ± 1R（v2 参数：docs/15 §4.3 回放后上调）
TP0_QTY = 0.20       # TP0 出 20%（v2：从 25% 降，切仓成本近比例下降）
TP0_GATE_R = 1.5     # TP1_R ≥ 1.5R 才挂 TP0（v2：只保最宽的单）
BE_LOCK_R = 0.3      # 半保本变体：SL 移 entry+0.3R
RR_TP1 = 1.5         # 假想 TP1 = 1.5R（AI_RR_MIN 铁律下限；docs/15 §二 真实单中位 2.2R）
STOP_BUF = 0.003
FEE_RT = 0.0014


def _get(client: httpx.Client, url: str, params: dict) -> list:
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, Exception):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    return []


def fetch_klines(client: httpx.Client, symbol: str, interval: str,
                 total: int, end_ms: int | None = None) -> list:
    out: list = []
    end = end_ms
    while len(out) < total:
        limit = min(1000, total - len(out))
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
    rows = _get(client, f"{FAPI}/fapi/v1/ticker/24hr", {})
    usdt = [t for t in rows
            if str(t.get("symbol", "")).endswith("USDT")
            and float(t.get("quoteVolume") or 0) >= min_vol]
    usdt.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


class _StubPool:
    def __init__(self, htf: list | None):
        self._htf = htf

    def get_klines(self, symbol, interval, limit):
        return self._htf


def replay_tp0(direction: str, entry: float, stop: float, tp0: float,
               tp1: float, be_price: float, fwd: list) -> dict:
    """TP0 增强口径逐 K 回放。be_price = 保本/锁利 SL 价（TP0 成交后武装）。

    阶段一（TP0 未成交）：触 SL → -1R 全损；触 TP0 → 进阶段二（+0.125R 记账）。
    阶段二（剩余 75% 仓，SL=be_price 已武装）：
      触 be → 剩余仓按 (be-entry)/R 计（全保本≈0，锁利+0.3）；触 TP1 → +1.5。
      同根先 SL 后 TP 按保守顺序（与 replay_suggestion 同保守度）。
    """
    d = 1 if direction == "long" else -1
    r_unit = abs(entry - stop)
    filled_tp0 = False
    for i, k in enumerate(fwd):
        high, low = float(k[2]), float(k[3])
        if direction == "long":
            hit_stop, hit_tp0, hit_tp1 = low <= stop, high >= tp0, high >= tp1
            hit_be = low <= be_price
        else:
            hit_stop, hit_tp0, hit_tp1 = high >= stop, low <= tp0, low <= tp1
            hit_be = high >= be_price
        if not filled_tp0:
            if hit_stop:
                return {"outcome": "loss", "r": -1.0, "bars": i + 1}
            if hit_tp0:
                filled_tp0 = True
                continue
        else:
            # TP0 已成交：SL=be（只收紧），TP1 在前（先看有利侧再保守判 SL，
            # 因为 be 在 entry+0.3/0 与 tp1 之间，同根双触按先赢后输保守序不可取——
            # 与 replay_suggestion 相反：那里 SL 优先因为 SL 离 entry 更近。
            # 这里 be 价已知高于 entry（锁利），同根触 be+TP1 按段内顺序无法判定，
            # 保守取 be（更近的价先成交——限价单/止损单同根均按穿越即成）。
            if hit_be:
                rr = (d * (be_price - entry)) / r_unit
                return {"outcome": "be_exit", "r": rr, "bars": i + 1}
            if hit_tp1:
                return {"outcome": "win_tp1", "r": 1.5, "bars": i + 1}
    if filled_tp0:
        return {"outcome": "open_tp0", "r": None, "bars": len(fwd)}
    return {"outcome": "open", "r": None, "bars": len(fwd)}


def run_pass(symbol: str, kl_all: list, htf_all: list, flt) -> list[dict]:
    htf_opens = [k[0] for k in htf_all]
    sigs: list[dict] = []
    pend: dict | None = None
    event: dict = {}
    for t in range(WINDOW - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - WINDOW): t + 2]
        t_close = kl_all[t][0] + HOUR_MS
        i = bisect_left(htf_opens, t_close)
        htf = htf_all[:i][-200:] or None
        cand, op = detect_indicator_candidate(
            _StubPool(htf), symbol, kl,
            pending=pend, flt=flt, retest=None, event_out=event)
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
        d = 1 if (cand.get("votes") or {}).get("net", 0) > 0 else -1
        if d > 0:
            stop = min(float(kl_all[j][3]) for j in range(t - 4, t + 1)) * (1 - STOP_BUF)
        else:
            stop = max(float(kl_all[j][2]) for j in range(t - 4, t + 1)) * (1 + STOP_BUF)
        dist = d * (entry - stop)
        if dist <= 0:
            continue
        r_unit = dist
        tp1 = entry + d * RR_TP1 * r_unit
        tp0 = entry + d * TP0_R * r_unit
        be_full = entry + d * 0.0 * r_unit  # 全保本 ≈ entry（费缓冲忽略，保守）
        be_lock = entry + d * BE_LOCK_R * r_unit
        fwd = kl_all[t + 1: t + 1 + FWD_BARS]
        gate = RR_TP1 >= TP0_GATE_R  # tp0_gate_r 门控（假想 RR=1.5 ≥ 1.2 → 全挂）

        # 基线：无 TP0
        base_out, _, base_bars = replay_suggestion(
            "long" if d > 0 else "short", entry, stop, tp1, 0.0, fwd)
        base_r = RR_TP1 if base_out == "win_tp1" else (-1.0 if base_out == "loss" else None)

        # TP0 增强（全保本 + 锁利变体）
        e_full = replay_tp0("long" if d > 0 else "short", entry, stop, tp0, tp1, be_full, fwd)
        e_lock = replay_tp0("long" if d > 0 else "short", entry, stop, tp0, tp1, be_lock, fwd)

        def _enh_r(e: dict, be_rr: float) -> float | None:
            if e["r"] is None:
                return None
            if e["outcome"] == "loss":
                return -1.0
            if e["outcome"] == "be_exit":
                # TP0 已 +TP0_QTY×TP0_R 记账，剩余仓 rr
                return TP0_QTY * TP0_R + (1 - TP0_QTY) * e["r"]
            if e["outcome"] == "win_tp1":
                return TP0_QTY * TP0_R + (1 - TP0_QTY) * RR_TP1
            return None

        # A 腿（BE@1R 巡检兜底，docs/15 §4.2 A 主体）：不切仓、不挂 TP0。
        # 巡检小时级滞后：价格先达 be_at_r=1R 的那根 K 线收盘后才武装保本——
        # 用「首次触及 1R 的 K 线索引」模拟（该根收盘后 SL 移 entry±fee），
        # 之后触 SL → 0R（保本），触 TP1 → +1.5R；未到 1R 先触 SL → -1R。
        def _leg_a() -> tuple[str, float | None, int]:
            armed = False
            _direction = "long" if d > 0 else "short"
            for i, k in enumerate(fwd):
                high, low = float(k[2]), float(k[3])
                if _direction == "long":
                    hit_r, hit_stop, hit_tp1 = high >= entry + r_unit, low <= stop, high >= tp1
                    hit_be = low <= entry
                else:
                    hit_r, hit_stop, hit_tp1 = low <= entry - r_unit, high >= stop, low <= tp1
                    hit_be = high >= entry
                if hit_r:
                    armed = True  # 本根收盘后巡检武装（滞后半根，保守不提前）
                if hit_stop:
                    if armed:
                        # 已武装：SL≈entry 保本（同根 1R 与 SL 双触不可能——1R 在 SL 反侧）
                        return ("be_exit", 0.0, i + 1)
                    return ("loss", -1.0, i + 1)
                if armed and hit_be:
                    return ("be_exit", 0.0, i + 1)
                if hit_tp1:
                    return ("win_tp1", RR_TP1, i + 1)
            return ("open", None, len(fwd))

        a_out, a_r, _ = _leg_a()

        sigs.append({
            "ts": sig[0], "symbol": symbol, "dir": d,
            "gate": gate, "base_out": base_out, "base_r": base_r,
            "full_r": _enh_r(e_full, 0.0), "full_out": e_full["outcome"],
            "lock_r": _enh_r(e_lock, BE_LOCK_R), "lock_out": e_lock["outcome"],
            "a_out": a_out, "a_r": a_r,
        })
    return sigs


def agg(sigs: list[dict], key: str) -> dict:
    decided = [s for s in sigs if s[key] is not None]
    rs = [s[key] for s in decided]
    wins = sum(1 for r in rs if r > 0.05)
    losses = sum(1 for r in rs if r <= -0.9)
    return {
        "n": len(sigs), "decided": len(decided),
        "exp_r": statistics.mean(rs) if rs else None,
        "win_rate": (wins / len(decided)) if decided else None,
        "full_loss_rate": (losses / len(decided)) if decided else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="TP0 测试网回放（docs/15 §4.3 实证）")
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    flt = resolve_scan_filters(None)
    with httpx.Client() as client:
        universe = top_universe(client, args.symbols, settings.MIN_VOLUME_24H)
        print(f"测试网宇宙 {len(universe)} 币 × 近 {args.days} 天 1h 回放中…")
        all_sigs: list[dict] = []
        for sym in universe:
            try:
                kl = fetch_klines(client, sym, "1h", WINDOW + args.days * 24)
                htf = fetch_klines(client, sym, "4h", 200 + args.days * 6)
            except Exception as e:
                print(f"  {sym} 拉取失败跳过: {e}")
                continue
            if len(kl) < WINDOW + 10 or not htf:
                continue
            all_sigs.extend(run_pass(sym, kl, htf, flt))
            time.sleep(0.1)

    gated = [s for s in all_sigs if s["gate"]]
    print(f"\n信号总数 {len(all_sigs)}（门控内 {len(gated)}）\n")
    print("| 口径 | n | decided | 期望R | 胜率(>0.05R) | 全损率(≤-0.9R) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, key in (("基线（无保护）", "base_r"),
                      ("A腿：BE@1R 巡检兜底", "a_r"),
                      ("TP0+全保本（对照）", "full_r"),
                      ("TP0+锁利0.3R（对照）", "lock_r")):
        a = agg(gated, key)
        print(f"| {name} | {a['n']} | {a['decided']} "
              f"| {a['exp_r']:.3f} " if a["exp_r"] is not None else f"| {name} | {a['n']} | {a['decided']} | - ",
              end="")
        print(f"| {a['win_rate']:.1%} | {a['full_loss_rate']:.1%} |"
              if a["win_rate"] is not None else "| - | - |")

    # 结局迁移矩阵：基线 loss 的单在各口径下变成什么
    from collections import Counter
    mig = Counter((s["base_out"], s["a_out"]) for s in gated if s["base_r"] is not None)
    print("\n结局迁移（基线 → A腿 BE@1R）:")
    for (b, f), c in sorted(mig.items(), key=lambda x: -x[1]):
        print(f"  {b} → {f}: {c}")
    mig1 = Counter((s["base_out"], s["full_out"]) for s in gated if s["base_r"] is not None)
    print("结局迁移（基线 → TP0全保本）:")
    for (b, f), c in sorted(mig1.items(), key=lambda x: -x[1]):
        print(f"  {b} → {f}: {c}")


if __name__ == "__main__":
    main()
