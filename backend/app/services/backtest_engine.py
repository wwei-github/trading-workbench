# -*- coding: utf-8 -*-
"""合约回测/前向回放引擎（docs/16 P-06）——纯函数核心，不读 DB 不调交易所。

三层结构：
1. replay_exit：出场规则引擎（本文件上半部）。基线=生产语义
   （replay_suggestion 保守序 + _tp_quantities 分批 + TP1 保本 + 摆动/ATR
   双腿跟进），变体以 overrides 叠加（BE 提前武装 / TP0 微止盈 / ATR k），
   一次回测跑 基线+N 变体输出配对 delta。
2. 采样器：build_signals_from_history（源A：历史 AI 建议单+veto+真实单）、
   build_signals_by_scan_replay（源B：滚动跑 detect_indicator_candidate 造
   假想入场）——统一输出信号 dict 列表。
3. simulate_portfolio：简版组合模拟（固定风险额 + 在跑上限 + 同向上限）
   与 aggregate（逐变体统计 + 配对 delta）。

同根 K 线多目标的保守序（统一规则，消歧义，复刻两处既有语义）：
- 逆侧止损（SL 在 entry 不利侧，long: sl<entry）：整根优先，同根目标位一律
  忽略——replay_suggestion「先触 SL 保守计 loss」语义；
- 顺侧锁利止损（保本/跟进 SL 已在有利侧）：更近的价先成交，距离相等止损
  优先——tp0_testnet_replay「be 近于 tp1 先成」语义；
- 目标位之间：离 entry 近者先成（TP0 0.5R < TP1 1.5R → TP0 先成）。
止损/保本武装一律「判定在根内、生效在下一根」（shadow A 腿滞后语义）。
"""
import math
from bisect import bisect_right
from typing import Optional

import app.config as config

# ── 引擎版本（随结果落库，口径演进可追溯）──
BACKTEST_ENGINE_VERSION = "fbe-1"

# 变体 overrides 合法键白名单（未知键由 API 层拒绝；此处为最终防线）
OVERRIDE_KEYS = (
    "be_trigger_r",     # None=基线（TP1 成交才武装保本）；float X=MFE≥X R 提前武装（CARD-05 X=0.8）
    "be_fee_buffer",    # True=保本价外推往返费缓冲（生产 _breakeven_stop_price 语义）
    "tp0_r",            # None=不挂 TP0；float Y=entry±Y·R 挂微止盈（CARD-06 Y=0.5）
    "tp0_qty_pct",      # TP0 出仓比例（默认 0.25）
    "tp0_gate_r",       # 门控：tp1_r ≥ Z 才挂 TP0（默认 1.2，docs/15 §4.2.4）
    "trail_atr_k",      # None=settings.ATR_TRAIL_K；0=关 ATR 腿；float=指定 k
    "trail_swing_bars", # 摆动腿回看根数（默认 3，生产 _trailing_stop 语义）
    "atr_period",       # ATR 周期（默认 22，生产 _chandelier_sl 语义）
    "fee_rate",         # taker 费率（默认 settings.TAKER_FEE_RATE）
    "fwd_bars",         # 前向窗口根数上限（超时 outcome=open）
)

# 基线参数（默认=生产语义）
_BASELINE = {
    "be_trigger_r": None,
    "be_fee_buffer": True,
    "tp0_r": None,
    "tp0_qty_pct": 0.25,
    "tp0_gate_r": 1.2,
    "trail_atr_k": None,       # None → 运行时取 settings.ATR_TRAIL_K
    "trail_swing_bars": 3,
    "atr_period": 22,
    "fee_rate": None,          # None → 运行时取 settings.TAKER_FEE_RATE
    "fwd_bars": None,          # None → 用完调用方给的 bars 即 open
}


def _resolve_params(overrides: Optional[dict]) -> dict:
    """基线浅合并变体（调用方已校验键白名单；此处再过滤一次防呆）。"""
    p = dict(_BASELINE)
    for k, v in (overrides or {}).items():
        if k in OVERRIDE_KEYS:
            p[k] = v
    if p["fee_rate"] is None:
        p["fee_rate"] = float(getattr(config.settings, "TAKER_FEE_RATE", 0.0005))
    if p["trail_atr_k"] is None:
        p["trail_atr_k"] = float(getattr(config.settings, "ATR_TRAIL_K", 3.0))
    return p


def _tp_quantities(tp2: float) -> tuple[float, float, float]:
    """分批数量（生产 _tp_quantities 铁律，不开放配置）：
    有 TP2 → TP1 50% / TP2 25% / 剩 25% 跟进；无 → TP1 75% / 剩 25% 跟进。"""
    if tp2 and tp2 > 0:
        return 0.50, 0.25, 0.25
    return 0.75, 0.0, 0.25


def _atr_series_daily(daily_bars: list, period: int) -> list[float]:
    """D1 ATR（Wilder 平滑，与生产 strategy.technical.atr_series 同口径的简化实现）。

    daily_bars: [[ts,o,h,l,c,v]] 时间升序。返回与输入等长的 ATR 数组
    （前 period-1 根为 NaN 占位——调用方按 as-of 索引取已完成日）。"""
    trs: list[float] = []
    prev_close: Optional[float] = None
    for k in daily_bars:
        high, low, close = float(k[2]), float(k[3]), float(k[4])
        tr = high - low if prev_close is None else max(
            high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    out = [math.nan] * len(trs)
    if len(trs) < period:
        return out
    atr = sum(trs[:period]) / period
    out[period - 1] = atr
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def _atr_asof(atr_arr: list[float], daily_ts: list[int], ts_ms: int) -> Optional[float]:
    """取 ts_ms 时刻「最近一根已完成日K」的 ATR（open_time+1d ≤ ts）。"""
    idx = bisect_right(daily_ts, ts_ms - 86_400_000) - 1
    if idx < 0 or idx >= len(atr_arr):
        return None
    a = atr_arr[idx]
    return float(a) if a == a and a > 0 else None  # NaN 过滤


def _final_outcome(filled: dict, armed: bool) -> str:
    """终局命名（对齐既有口径）：tp2 → win_tp2；tp1 → win_tp1；
    仅 tp0 或保本/跟进扫出 → be_exit（shadow A/B 腿同款）；全无 → loss。"""
    if "tp2" in filled:
        return "win_tp2"
    if "tp1" in filled:
        return "win_tp1"
    if "tp0" in filled or armed:
        return "be_exit"
    return "loss"


def replay_exit(
    direction: str, entry: float, stop: float, tp1: float, tp2: float,
    bars: list, overrides: Optional[dict] = None,
    daily_bars: Optional[list] = None,
) -> dict:
    """逐 K 出场回放（纯函数）。

    参数：direction long/short；entry/stop/tp1/tp2 三价（tp2 可 0）；
    bars [[ts,o,h,l,c,v]] 已收盘、时间升序、自入场后第一根起；
    daily_bars 可选 D1 K线（ATR 跟进腿需要；None=ATR 腿退化关闭，与生产
    _chandelier_sl 数据不可得时退化摆动跟进同口径）。

    返回 {outcome, r, legs, sl_price, bars, armed_be, trail_used}：
    - outcome: loss / win_tp1 / win_tp2 / tp0_be / open
    - r: 含费净 R（全部腿合计）；open 为 None（已落袋部分见 legs，
      不进 decided 分母——P-04 口径教训）
    """
    p = _resolve_params(overrides)
    d = 1 if direction == "long" else -1
    r_unit = abs(entry - stop)
    _invalid = {"outcome": "invalid", "r": None, "legs": [], "sl_price": None,
                "bars": 0, "armed_be": False, "trail_used": False}
    if r_unit <= 0 or entry <= 0 or not direction:
        return _invalid
    fee = float(p["fee_rate"])
    fee_r_unit = (entry * 2 * fee) / r_unit  # 往返费（开+平各一次）折 R

    tp1_r = d * (tp1 - entry) / r_unit
    use_tp2 = bool(tp2 and tp2 > 0)
    tp0_price = None
    tp0_qty = 0.0
    if p["tp0_r"] and float(p["tp0_r"]) > 0 and tp1_r >= float(p["tp0_gate_r"]):
        tp0_price = entry + d * float(p["tp0_r"]) * r_unit
        tp0_qty = float(p["tp0_qty_pct"])
    q1, q2, _q_trail = _tp_quantities(tp2 if use_tp2 else 0.0)

    # 保本价（生产 _breakeven_stop_price：成本价 ± 往返费缓冲）
    be_buf = entry * 2 * fee if p["be_fee_buffer"] else 0.0
    be_price = entry + d * be_buf

    atr_arr = daily_ts = None
    if daily_bars and float(p["trail_atr_k"]) > 0:
        atr_arr = _atr_series_daily(daily_bars, int(p["atr_period"]))
        daily_ts = [int(k[0]) for k in daily_bars]

    sl = stop                      # 当前生效 SL（根内判定用上根收盘后的值）
    armed = False                  # 保本武装（根收盘判定、下根生效）
    filled: dict[str, float] = {}
    legs: list[dict] = []
    mfe_r = 0.0
    hh = ll = None                 # 持仓期极值（Chandelier 腿用，自入场起）
    trail_used = False
    n_bars = 0

    def _leg(name: str, price: float, qty_pct: float) -> None:
        gross = d * (price - entry) / r_unit
        legs.append({"name": name, "price": round(price, 10),
                     "qty_pct": round(qty_pct, 4),
                     "r": round((gross - fee_r_unit) * qty_pct, 6)})

    def _remaining() -> float:
        return 1.0 - sum(filled.values())

    def _t_r(price: float) -> float:
        return d * (price - entry) / r_unit

    def _finish(outcome: str, sl_price: Optional[float], nb: int) -> dict:
        return {"outcome": outcome,
                # open 不给 r（不进 decided 分母）；已落袋腿见 legs
                "r": None if outcome == "open" else round(
                    sum(l["r"] for l in legs), 6),
                "legs": legs, "sl_price": sl_price, "bars": nb,
                "armed_be": armed, "trail_used": trail_used}

    for i, k in enumerate(bars):
        if p["fwd_bars"] is not None and i >= int(p["fwd_bars"]):
            break
        n_bars = i + 1
        high, low, close = float(k[2]), float(k[3]), float(k[4])
        if d > 0:
            hh = high if hh is None else max(hh, high)
            mfe_r = max(mfe_r, (hh - entry) / r_unit)
        else:
            ll = low if ll is None else min(ll, low)
            mfe_r = max(mfe_r, (entry - ll) / r_unit)

        # ── 1) 本根触发判定（SL 用上根收盘后的当前值）──
        hit_sl = low <= sl if d > 0 else high >= sl
        tg: list[tuple[float, str, float]] = []
        if tp0_price is not None and "tp0" not in filled and (
                (high >= tp0_price) if d > 0 else (low <= tp0_price)):
            tg.append((tp0_price, "tp0", tp0_qty))
        if "tp1" not in filled and ((high >= tp1) if d > 0 else (low <= tp1)):
            tg.append((tp1, "tp1", q1))
        if use_tp2 and "tp2" not in filled and (
                (high >= tp2) if d > 0 else (low <= tp2)):
            tg.append((tp2, "tp2", q2))
        sl_name = ("trail_tp2" if "tp2" in filled
                   else "trail_tp1" if "tp1" in filled
                   else "be_after_tp0" if "tp0" in filled
                   else "be_exit" if armed else "sl")

        if hit_sl:
            adverse = (sl < entry) if d > 0 else (sl > entry)
            if adverse:
                # 基线保守语义（replay_suggestion）：逆侧止损整根优先，
                # 同根触发目标一律忽略；已落袋腿保留（win_tp1 口径）
                _leg(sl_name, sl, _remaining())
                return _finish(_final_outcome(filled, armed), sl, n_bars)
            # 顺侧锁利止损（保本/跟进）：全部触发价按离 entry 的 R 距离升序，
            # 距离相等止损优先（tp0_testnet_replay 语义）；SL 成交即清剩余仓，
            # 其后目标不再成交
            hits = tg + [(sl, sl_name, _remaining())]
            hits.sort(key=lambda t: (_t_r(t[0]), 0 if t[1] == sl_name else 1))
            for price, name, qty in hits:
                _leg(name, price, qty)
                if name == sl_name:
                    break
                filled[name] = qty
            return _finish(_final_outcome(filled, armed), sl, n_bars)

        # ── 2) 未触 SL：触发的目标按保守序成交 ──
        for price, name, qty in sorted(tg, key=lambda t: _t_r(t[0])):
            _leg(name, price, qty)
            filled[name] = qty
        if _remaining() <= 1e-6:
            # 仓位已全部落袋（如 TP0 25% + TP1 75%）：即告终局，不再等跟进
            return _finish(_final_outcome(filled, armed), None, n_bars)

        # ── 3) 根收盘：武装判定 + SL 更新（下根生效）──
        if not armed:
            if "tp1" in filled or "tp2" in filled:
                armed = True  # 基线：TP1 成交武装保本
            elif p["be_trigger_r"] is not None and mfe_r >= float(p["be_trigger_r"]):
                armed = True  # 变体：MFE 触线收盘武装（CARD-05）
        if tp0_price is not None and "tp0" in filled:
            armed = True  # CARD-06：TP0 成交立即武装保本

        trailing_active = ("tp2" in filled) or ("tp1" in filled and not use_tp2)
        new_sl = sl
        if armed:
            new_sl = max(new_sl, be_price) if d > 0 else min(new_sl, be_price)
        if trailing_active:
            trail_used = True
            nb = max(1, int(p["trail_swing_bars"]))
            window = bars[max(0, i - nb + 1): i + 1]  # 最近 nb 根已收盘（含当前）
            if d > 0:
                new_sl = max(new_sl, min(float(b[3]) for b in window))
                if "tp2" in filled:
                    new_sl = max(new_sl, tp1)  # TP2 后保本夹紧（生产语义）
            else:
                new_sl = min(new_sl, max(float(b[2]) for b in window))
                if "tp2" in filled:
                    new_sl = min(new_sl, tp1)
            if atr_arr is not None:
                a = _atr_asof(atr_arr, daily_ts, int(k[0]) + 3_600_000)
                if a is not None:
                    kk = float(p["trail_atr_k"])
                    chand = (hh - kk * a) if d > 0 else (ll + kk * a)
                    new_sl = max(new_sl, chand) if d > 0 else min(new_sl, chand)
        # 生产「新 SL 挂上即立即触发则跳过本轮」语义（本根收盘价近似结算时现价）
        imm = (new_sl >= close) if d > 0 else (new_sl <= close)
        if new_sl != sl and not imm:
            sl = new_sl

    return _finish("open", None, n_bars)


# ══════════════════════════════════════════════════════════════════════
# 采样器：统一信号格式 {symbol, direction, entry, stop, tp1, tp2,
# entry_time_ms, meta}——出场引擎与组合模拟只认这一种形状。
# ══════════════════════════════════════════════════════════════════════

HOUR_MS = 3_600_000


def _ms(dt) -> int:
    """datetime（naive=UTC）→ epoch ms。"""
    return int(dt.timestamp() * 1000) if dt else 0


def build_signals_from_history(ai_rows: list, trade_rows: list,
                               trade_open_events: Optional[dict] = None) -> list[dict]:
    """源A：历史 AI 建议单 + veto 否决单 + 真实单（actual）。

    ai_rows: (AIAnalysis, ScanResult|None) 二元组列表——suggest 直取三价；
    skip 且带 veto_original 的否决单取快照三价（回放「被否掉的建议」假想表现，
    docs/24 A5a 口径）。trade_rows: TradeRecord 列表（真实出场对照样本）。
    trade_open_events: {trade_id(str): OPEN 事件 detail}——初始三价快照。
    rec.stop_loss 是现值（TP1 后移保本/跟进后更新，历史只在 trade_events），
    直接回放会低估初始风险，故必须优先取 OPEN 快照，无事件行兜底现值。
    无三价/方向残缺的行静默跳过（计数进 meta 不可能——由调用方记 skipped）。
    """
    sigs: list[dict] = []
    for a, sr in (ai_rows or []):
        veto = None
        if a.trade_decision != "suggest":
            if not a.veto_original:
                continue  # 无 veto 快照的 skip 无三价可回放
            veto = a.veto_original
        src = veto or a
        direction = str(src.get("direction") or "") if veto else a.direction
        entry = float(src.get("entry_price") or 0) if veto else float(a.entry_price or 0)
        stop = float(src.get("stop_loss") or 0) if veto else float(a.stop_loss or 0)
        tp1 = float(src.get("take_profit_1") or 0) if veto else float(a.take_profit_1 or 0)
        tp2 = float(src.get("take_profit_2") or 0) if veto else float(a.take_profit_2 or 0)
        if entry <= 0 or stop <= 0 or tp1 <= 0 or not direction:
            continue
        sigs.append({
            "symbol": a.symbol, "direction": direction,
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "entry_time_ms": _ms(a.created_at),
            "meta": {
                "src": "veto" if veto else "suggest",
                "signal_type": getattr(sr, "signal_type", None),
                "position": getattr(sr, "position", None),
                "ema_state": getattr(sr, "ema_state", None),
                "quality_score": getattr(sr, "quality_score", None),
                "recommendation": (float(a.recommendation) if a.recommendation is not None else None),
                "rule_version": a.rule_version,
            },
        })
    for rec in (trade_rows or []):
        entry = float(rec.entry_price or 0)
        ev = (trade_open_events or {}).get(str(rec.id)) or {}
        stop = float(ev.get("stop_loss") or rec.stop_loss or 0)
        tp1 = float(ev.get("tp1") or rec.tp1 or 0)
        if entry <= 0 or stop <= 0 or tp1 <= 0 or not rec.direction:
            continue
        sigs.append({
            "symbol": rec.symbol, "direction": rec.direction,
            "entry": entry, "stop": stop, "tp1": tp1,
            "tp2": float(ev.get("tp2") or rec.tp2 or 0),
            "entry_time_ms": _ms(rec.opened_at),
            "meta": {
                "src": "actual",
                "exit_reason": rec.exit_reason,
                "realized_pnl": float(rec.realized_pnl) if rec.realized_pnl is not None else None,
                "risk_amount": float(rec.risk_amount) if rec.risk_amount is not None else None,
            },
        })
    sigs.sort(key=lambda s: s["entry_time_ms"])
    return sigs


class _StubPool:
    """detect_indicator_candidate 的 4h K线替身（脚本 _StubPool 同款）。"""

    def __init__(self, htf: Optional[list]):
        self._htf = htf

    def get_klines(self, symbol, interval, limit):
        return self._htf


def derive_signal_prices(
    direction: int, entry: float, recent5: list, *,
    rr_tp1: float = 1.5, stop_buf: float = 0.003, tp2_rr: float = 0.0,
) -> Optional[dict]:
    """规则三价推导（源B 公式抽取，P-07 虚拟账本实时挂点同源调用）。

    direction: +1 long / -1 short；entry: 信号K收盘价；
    recent5: 含信号K在内的最近 5 根**已收盘** [[ts,o,h,l,c,v]]（信号K为末根）。
    stop=近5根极值∓stop_buf（long 取最低×(1-buf)，short 对称）；
    tp1=entry±rr_tp1·r_unit；tp2=tp2_rr>0 时 entry±tp2_rr·r_unit 否则 0（不设）。
    返回 {entry, stop, tp1, tp2, r_unit}；entry<=0 或 r_unit<=0 为 None。
    """
    if entry is None or entry <= 0 or not recent5:
        return None
    if direction > 0:
        stop = min(float(k[3]) for k in recent5) * (1 - stop_buf)
    else:
        stop = max(float(k[2]) for k in recent5) * (1 + stop_buf)
    r_unit = direction * (entry - stop)
    if r_unit <= 0:
        return None
    tp1 = entry + direction * rr_tp1 * r_unit
    tp2 = entry + direction * tp2_rr * r_unit if tp2_rr and tp2_rr > 0 else 0.0
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "r_unit": r_unit}


def build_signals_by_scan_replay(
    symbol: str, kl_all: list, htf_all: list, flt,
    *, window: int = 240, rr_tp1: float = 1.5, stop_buf: float = 0.003,
    tp2_rr: float = 0.0, signal_types: tuple = ("trigger_pullback_confirm",),
    repeat_bars: int = 24,
) -> list[dict]:
    """源B：滚动跑生产同款 detect_indicator_candidate 造假想入场
    （镜像 scripts/tp0_testnet_replay.py run_pass 模式，可配置化）。

    kl_all: 该币 1h 全历史（含 window 根预热）；htf_all: 4h 背景。
    假想单：entry=信号K收盘、stop=近5根极值±stop_buf、tp1=rr_tp1×r_unit、
    tp2=tp2_rr×r_unit（0=不设）。repeat_bars：同向信号最小间隔（生产
    repeat_window_hours 同语义，防同段行情重复计入样本）。
    """
    from bisect import bisect_left

    from app.services.scanner import detect_indicator_candidate

    htf_opens = [int(k[0]) for k in htf_all]
    sigs: list[dict] = []
    pend: Optional[dict] = None
    event: dict = {}
    last_sig_t = -10**9
    last_dir = 0
    for t in range(window - 1, len(kl_all) - 1):
        kl = kl_all[max(0, t + 1 - window): t + 2]
        t_close = int(kl_all[t][0]) + HOUR_MS
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
        if not cand or cand.get("signal_type") not in signal_types:
            continue
        direction = 1 if (cand.get("votes") or {}).get("net", 0) > 0 else -1
        # 同向信号去重：repeat_bars 内的同向重复不计（生产 repeat 抑制同语义）
        if direction == last_dir and t - last_sig_t < repeat_bars:
            continue
        prices = derive_signal_prices(
            direction, float(kl_all[t][4]), kl_all[max(0, t - 4): t + 1],
            rr_tp1=rr_tp1, stop_buf=stop_buf, tp2_rr=tp2_rr,
        )
        if prices is None:
            continue
        sigs.append({
            "symbol": symbol, "direction": "long" if direction > 0 else "short",
            **prices,
            "entry_time_ms": int(kl_all[t][0]) + HOUR_MS,  # 收盘即入场
            "meta": {
                "src": "scan",
                "signal_type": cand.get("signal_type"),
                "touch_ref": cand.get("touch_ref"),
                "quality_score": cand.get("quality_score"),
                "votes_net": (cand.get("votes") or {}).get("net"),
            },
        })
        last_sig_t, last_dir = t, direction
    return sigs


# ══════════════════════════════════════════════════════════════════════
# 组合模拟与聚合统计
# ══════════════════════════════════════════════════════════════════════


def simulate_portfolio(pairs: list, *, risk_usdt: float = 100.0,
                       max_open_trades: int = 5,
                       same_direction_cap: int = 0) -> dict:
    """简版组合模拟（单变体）：信号按入场时间序过闸——在跑上限 + 同向上限
    （0=不限，与生产 SAME_DIRECTION_CAP 语义一致）；每笔固定风险额（不复利）。

    pairs: [(signal, replay_exit 结果)]；占用区间=[entry_time, entry+bars×1h)，
    open 单按 fwd 窗口全长占用（保守）。权益入账：closed 取 r；open 取已落袋
    腿合计（剩余按 0 保守）。输出 R/USDT 双口径日曲线与回撤、拒单计数。
    """
    events = sorted(pairs, key=lambda sp: sp[0]["entry_time_ms"])
    open_until: list[tuple[int, str]] = []  # (释放时刻, direction)
    equity_r = 0.0
    curve: list[dict] = []
    rejected = {"slot_full": 0, "same_dir_cap": 0}
    peak = 0.0
    max_dd = 0.0
    for sig, res in events:
        t0 = sig["entry_time_ms"]
        # 清理已释放槽
        open_until = [(u, dr) for (u, dr) in open_until if u > t0]
        hold = max(1, int(res.get("bars") or 1)) * HOUR_MS
        t1 = t0 + hold if res["outcome"] != "open" else t0 + hold
        if len(open_until) >= max(1, int(max_open_trades)):
            rejected["slot_full"] += 1
            continue
        if same_direction_cap and same_direction_cap > 0:
            same_n = sum(1 for _, dr in open_until if dr == sig["direction"])
            if same_n >= int(same_direction_cap):
                rejected["same_dir_cap"] += 1
                continue
        open_until.append((t1, sig["direction"]))
        r = res["r"] if res["r"] is not None else sum(
            l["r"] for l in (res.get("legs") or []))
        equity_r += r
        peak = max(peak, equity_r)
        max_dd = max(max_dd, peak - equity_r)
        curve.append({"t_ms": t1, "r": round(r, 6),
                      "equity_r": round(equity_r, 6),
                      "equity_usdt": round(equity_r * risk_usdt, 2)})
    # 聚合到日（按平仓日）
    daily: dict[str, dict] = {}
    for c in curve:
        day = datetime_utc_date(c["t_ms"])
        d = daily.setdefault(day, {"date": day, "equity_r": 0.0,
                                   "equity_usdt": 0.0, "closed": 0})
        d["equity_r"] = c["equity_r"]  # 曲线已累计，日取末值
        d["equity_usdt"] = c["equity_usdt"]
        d["closed"] += 1
    return {
        "final_equity_r": round(equity_r, 4),
        "final_equity_usdt": round(equity_r * risk_usdt, 2),
        "max_dd_r": round(max_dd, 4),
        "trades": len(curve),
        "rejected": rejected,
        "daily_series": sorted(daily.values(), key=lambda x: x["date"]),
    }


def datetime_utc_date(t_ms: int) -> str:
    """epoch ms → YYYY-MM-DD（UTC 日界）。"""
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(t_ms / 1000).strftime("%Y-%m-%d")


def _variant_stats(results: list) -> dict:
    """单变体统计（P-04 口径：open/invalid 不进 decided 分母，单列 excluded）。"""
    from collections import Counter

    n = len(results)
    closed = [r for r in results if r["outcome"] not in ("open", "invalid")]
    invalid = sum(1 for r in results if r["outcome"] == "invalid")
    open_n = n - len(closed) - invalid
    rs = [r["r"] for r in closed]
    dist = Counter(r["outcome"] for r in closed)
    return {
        "n": n,
        "decided": len(closed),
        "excluded_open": open_n + invalid,
        "exp_r": round(sum(rs) / len(rs), 4) if rs else None,
        "sum_r": round(sum(rs), 3) if rs else 0.0,
        "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 4) if rs else None,
        "full_loss_rate": round(dist.get("loss", 0) / len(closed), 4) if closed else None,
        "avg_bars": round(sum(r["bars"] for r in closed) / len(closed), 1) if closed else None,
        "outcome_dist": dict(dist),
    }


def aggregate(base_results: list, variants: list, signals: list,
              *, risk_usdt: float = 100.0, max_open_trades: int = 5,
              same_direction_cap: int = 0) -> dict:
    """聚合：variants = [(name, overrides, results)]（首个为基线）。

    配对 delta 只取基线与变体双双终局（均 closed）的样本（P-04 教训：
    一方 open 不计 pairs），输出 exp_r 差、胜率差与结局迁移矩阵 top5。
    """
    base_stats = _variant_stats(base_results)
    out_variants = [{"name": "baseline", **base_stats}]
    out_deltas: list[dict] = []
    from collections import Counter

    for name, overrides, results in variants:
        st = _variant_stats(results)
        out_variants.append({"name": name, "overrides": overrides, **st})
        if name == "baseline":
            continue
        pairs_r = [(b["r"], v["r"]) for b, v in zip(base_results, results)
                   if b["outcome"] not in ("open", "invalid")
                   and v["outcome"] not in ("open", "invalid")]
        if pairs_r:
            deltas = [v - b for b, v in pairs_r]
            mig = Counter((b["outcome"], v["outcome"])
                          for b, v in zip(base_results, results)
                          if b["outcome"] not in ("open", "invalid")
                          and v["outcome"] not in ("open", "invalid"))
            out_deltas.append({
                "name": name,
                "n_pairs": len(pairs_r),
                "delta_exp_r": round(sum(deltas) / len(deltas), 4),
                "delta_sum_r": round(sum(deltas), 3),
                "delta_win_rate": round(
                    sum(1 for b, v in pairs_r if v > 0) / len(pairs_r)
                    - sum(1 for b, v in pairs_r if b > 0) / len(pairs_r), 4),
                "migration_top": [
                    {"from": k[0], "to": k[1], "n": n}
                    for k, n in mig.most_common(5)],
            })
        else:
            out_deltas.append({"name": name, "n_pairs": 0,
                               "delta_exp_r": None, "delta_sum_r": 0.0,
                               "delta_win_rate": None, "migration_top": []})
    # 组合模拟：基线 + 各变体各跑一份（纯算术，成本低）
    portfolio = []
    all_pairs = [("baseline", list(zip(signals, base_results)))]
    for name, _ov, results in variants:
        if name != "baseline":
            all_pairs.append((name, list(zip(signals, results))))
    for name, pairs in all_pairs:
        ps = simulate_portfolio(pairs, risk_usdt=risk_usdt,
                                max_open_trades=max_open_trades,
                                same_direction_cap=same_direction_cap)
        portfolio.append({"name": name, **ps})
    return {"variants": out_variants, "deltas": out_deltas,
            "portfolio": portfolio}
