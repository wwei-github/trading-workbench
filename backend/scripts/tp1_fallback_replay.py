# -*- coding: utf-8 -*-
"""第一档止盈「兜底价」误 skip · 历史回放（2026-10-19）。

背景：线上当日统计出 9 笔高置信「误 skip」——AI 把 risk_guard 的止盈一距离上限
    cap = max(min(TP1_MAX_ATR×ATR, TP1_MAX_R×止损距离), rr_min×止损距离)
误读成"≤2×ATR 的绝对硬上限"，据此把合法的"入场±rr_min×止损距离"兜底价判为越限并
skip（多笔 skip_reason 甚至写"程序复算必打回/程序不允许/没有合规止盈档"）。提示词已
改为与程序复算口径一致（ai_analyzer.render_tp1_cap_desc / ai_agent，RULE_VERSION
v2026-10-19）。

本脚本做**确定性回放**（不调用 LLM），对 9 笔逐笔验证两点：
  ① 程序侧：用「入场±rr_min×止损距离」兜底价作第一档止盈，risk_guard.parse_and_validate
     返回 0 violations（即程序会放行该 suggest）——直接证伪"程序必打回"；
  ② 市场侧：若当真采纳该兜底 suggest，用 replay_suggestion 做前向 1h 回放，给出
     outcome / 到出场根数（对齐生产 24h 观察窗）。

口径（与生产同源）：
  - K线走币安合约公共 API（免 key），信号时刻取 UTC 时点。
  - 入场价 = 信号K线（信号时刻刚收盘的那根 1h 柱）收盘价。
  - ATR14 由 atr_series(klines, period=14) 复算——该函数内部剔除最后一根（视作未收盘），
    故回放数组尾部保留一根"次日柱"占位、使 ATR 恰好定格在信号K线。
  - 止损 = 金K窗口近锚（最近 5 根已收盘K线共同极值 ±0.3%，v2026-10-12-8 口径）：
    做多 min(low)×(1-0.003)，做空 max(high)×(1+0.003)；若窄于宽度下限
    SL_MIN_ATR×ATR14 则外扩到下限（模拟合规 AI 的"外扩锚点"），并标注。
  - 兜底 tp1 = 入场 ∓ rr_min×|入场−止损|。

局限（务必知悉）：本脚本验证的是"程序口径下兜底 suggest 会放行 + 若采纳的前向表现"，
**不能证明 LLM 会真的改判**——那需用新提示词重跑 LLM（另一条验证路径）。

用法（cwd=backend）：
    python scripts/tp1_fallback_replay.py
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.risk_guard import parse_and_validate  # noqa: E402
from app.services.strategy.technical import atr_series  # noqa: E402
from app.tasks.review_tasks import replay_suggestion  # noqa: E402

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
FWD_BARS = 24          # 前向观察窗（= 生产 OBSERVATION_HOURS 24h）
STOP_BUF = 0.003       # 金K窗口极值外 0.3% 缓冲（NEAR_ANCHOR_BUFFER 同值）

# 9 笔高置信误 skip：(symbol, 方向, 信号K线收盘UTC时刻)。
# UTC 时刻 = 「刚收盘的那根 1h 柱」的收盘时间；LINKUSDT 的 22:08 只是分析落库时刻，
# 其信号K线同为 14:00 UTC 收盘。
CASES = [
    ("RVNUSDT",    "short", "2026-10-18T16:00:00Z"),
    ("ENSOUSDT",   "short", "2026-10-18T21:00:00Z"),
    ("KMNOUSDT",   "long",  "2026-10-19T08:00:00Z"),
    ("SOPHUSDT",   "short", "2026-10-19T09:00:00Z"),
    ("SOLVUSDT",   "long",  "2026-10-19T10:00:00Z"),
    ("STABLEUSDT", "short", "2026-10-19T12:00:00Z"),
    ("GRAMUSDT",   "short", "2026-10-19T14:00:00Z"),
    ("AAVEUSDT",   "short", "2026-10-19T14:00:00Z"),
    ("LINKUSDT",   "short", "2026-10-19T14:00:00Z"),
]


def _get(client: httpx.Client, url: str, params: dict) -> list:
    for attempt in range(3):
        try:
            r = client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    return []


def fetch_klines(client: httpx.Client, symbol: str, interval: str,
                 end_ms: int, total: int) -> list:
    """向前分页拉取至 end_ms（升序）"""
    out: list = []
    end = end_ms
    while len(out) < total:
        limit = min(1500, total - len(out))
        params = {"symbol": symbol, "interval": interval,
                  "limit": limit, "endTime": end}
        batch = _get(client, f"{FAPI}/fapi/v1/klines", params)
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        if len(batch) < limit:
            break
        time.sleep(0.12)
    return out


def _epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp() * 1000)


def replay_one(client: httpx.Client, symbol: str, direction: str,
               signal_iso: str) -> dict:
    t_ms = _epoch_ms(signal_iso)
    sig_open = t_ms - HOUR_MS  # 信号K线开盘时刻
    # 终端拉到信号后 FWD_BARS+2 根（实际只到 now，故近期信号的观察窗尚未走完）
    bars = fetch_klines(client, symbol, "1h", t_ms + (FWD_BARS + 2) * HOUR_MS, 340)
    idx = next((i for i, b in enumerate(bars) if int(b[0]) == sig_open), None)
    if idx is None:
        raise RuntimeError(f"{symbol} {signal_iso} 未取到信号K线（open={sig_open}）")

    # 生产口径：尾部留一根"未收盘"占位，使 atr_series / 近锚窗口都定格在信号K线
    klines = bars[: idx + 2] if idx + 1 < len(bars) else bars[: idx + 1]
    entry = float(bars[idx][4])

    _arr = atr_series(klines, period=14)
    atr14 = float(_arr[-1]) if _arr is not None and len(_arr) else 0.0

    # ── 金K窗口近锚止损（最近 5 根已收盘K线共同极值 ±0.3%）──
    win = bars[max(0, idx - 4): idx + 1]
    lows = [float(c[3]) for c in win if float(c[3]) > 0]
    highs = [float(c[2]) for c in win if float(c[2]) > 0]
    if direction == "long":
        anchor = min(lows) * (1 - STOP_BUF)
    else:
        anchor = max(highs) * (1 + STOP_BUF)

    stop_dist = abs(entry - anchor)
    floor = float(settings.SL_MIN_ATR) * atr14
    widened = False
    if floor > 0 and stop_dist < floor:
        stop_dist = floor
        widened = True
    stop = entry - stop_dist if direction == "long" else entry + stop_dist

    # ── 兜底 tp1（入场 ± rr_min×止损距离）──
    rr_min = float(settings.AI_RR_MIN)
    fb_dist = rr_min * stop_dist
    tp1_fb = entry + fb_dist if direction == "long" else entry - fb_dist

    # ── 程序复算口径的上限（复刻 risk_guard L410-417）──
    _caps = []
    if float(settings.TP1_MAX_R) > 0:
        _caps.append(float(settings.TP1_MAX_R) * stop_dist)
    if atr14 > 0 and float(settings.TP1_MAX_ATR) > 0:
        _caps.append(float(settings.TP1_MAX_ATR) * atr14)
    cap = max(min(_caps), rr_min * stop_dist) if _caps else 0.0

    # ── 校验① 兜底 tp1 ──
    sig = {"symbol": symbol, "signal_type": "trigger_trend_breakout",
           "signal_bar_provisional": False}

    def _validate(tp1: float) -> tuple[bool, list]:
        raw = {
            "trade_decision": "suggest", "direction": direction,
            "trade_type": "structure_break", "stop_anchor": "structure",
            "entry_price": entry, "stop_loss": stop, "take_profit_1": tp1,
            "take_profit_2": 0.0, "risk_reward_ratio": rr_min,
            "recommendation": 70.0, "analysis": "",
        }
        ok, viol, _ = parse_and_validate(raw, sig, klines)
        return ok, viol

    ok_fb, viol_fb = _validate(tp1_fb)
    # 对照：一个明显越过上限的远端结构位（模拟 AI 原本想挂的远位）
    tp1_far = (entry + cap * 1.3) if direction == "long" else (entry - cap * 1.3)
    ok_far, viol_far = _validate(tp1_far)

    # ── 校验② 前向 1h 回放（若采纳兜底 suggest；实际只到 now）──
    fwd = bars[idx + 1: idx + 1 + FWD_BARS]
    outcome, tp1_hit, bars_to = replay_suggestion(
        direction, entry, stop, tp1_fb, 0.0, fwd)

    return {
        "symbol": symbol, "direction": direction, "signal_iso": signal_iso,
        "entry": entry, "stop": stop, "stop_dist": stop_dist,
        "atr14": atr14, "cap": cap, "fb_dist": fb_dist,
        "tp1_fb": tp1_fb, "tp1_far": tp1_far, "widened": widened,
        "ok_fb": ok_fb, "viol_fb": viol_fb,
        "ok_far": ok_far, "viol_far": viol_far,
        "outcome": outcome, "tp1_hit": tp1_hit, "bars_to": bars_to,
        "fwd_n": len(fwd),
    }


def main() -> None:
    print(f"RULE_VERSION = {settings.RULE_VERSION}")
    print(f"TP1_MAX_ATR={settings.TP1_MAX_ATR:g}  TP1_MAX_R={settings.TP1_MAX_R:g}  "
          f"AI_RR_MIN={settings.AI_RR_MIN:g}  AI_RR_TOL={settings.AI_RR_TOL:g}  "
          f"SL_MIN_ATR={settings.SL_MIN_ATR:g}")
    print(f"兜底上限口径：cap = max(min(TP1_MAX_ATR×ATR, TP1_MAX_R×止损距), "
          f"{settings.AI_RR_MIN:g}×止损距)\n")

    rows = []
    with httpx.Client() as client:
        for symbol, direction, iso in CASES:
            try:
                rows.append(replay_one(client, symbol, direction, iso))
            except Exception as e:  # noqa: BLE001
                print(f"[ERR] {symbol} {iso}: {e}")

    ok_cnt = 0
    for r in rows:
        if r["ok_fb"]:
            ok_cnt += 1
        flag = "兜底√" if r["ok_fb"] else "兜底×"
        widen = " (锚外扩至宽度下限)" if r["widened"] else ""
        print(f"── {r['symbol']:>10} {r['direction']:<5} {r['signal_iso']}  {flag}")
        print(f"   入场={r['entry']:.8g}  止损={r['stop']:.8g} "
              f"(距={r['stop_dist']:.8g}, ≈{r['stop_dist'] / r['atr14']:.2f}×ATR){widen}")
        print(f"   ATR14={r['atr14']:.8g}  上限cap={r['cap']:.8g} "
              f"(≈{r['cap'] / r['stop_dist']:.2f}R)  兜底距={r['fb_dist']:.8g} "
              f"(= {settings.AI_RR_MIN:g}R, {'≤' if r['fb_dist'] <= r['cap'] * (1 + 1e-9) else '>'}cap)")
        print(f"   兜底tp1={r['tp1_fb']:.8g}  校验① violations={r['viol_fb'] or '无'}")
        print(f"   对照(远位 tp1={r['tp1_far']:.8g}) 校验② violations={r['viol_far'] or '无'}")
        print(f"   回放({r['fwd_n']}/{FWD_BARS}h 已走完) outcome={r['outcome']}  "
              f"tp1_hit={r['tp1_hit']}  bars_to_exit={r['bars_to']}")
        print()

    print(f"=== 兜底价程序放行：{ok_cnt}/{len(rows)} 笔 ===")
    print("说明：本结果是「程序口径下兜底 suggest 会放行 + 若采纳的前向表现」的确定性回放，"
          "非 LLM 是否改判——后者需以新提示词重跑模型。")


if __name__ == "__main__":
    main()
