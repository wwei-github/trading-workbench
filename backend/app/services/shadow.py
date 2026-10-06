"""影子模式（docs/14，2026-10-04）：候选信号特征「只计算+打标+记录，不参与决策」。

四环：打标（scanner scan_one 计算 + _save_results 落 shadow_marks）
→ 归因（复用 TradeReview/TradeRecord，§五）→ 评估（tasks/shadow_tasks.py 每日
分桶统计，本模块 build_report）→ 裁决（docs/14 §七 预注册标准卡）。

铁律（docs/14 §九）：
- 打标代码任何异常必须吞掉（记 debug 日志），绝不影响主管线；
- 打标结果不写回候选 dict、不进 AI 上下文——影子数据对决策路径不可见；
- 特征启停改代码（SHADOW_FEATURES.enabled），不进配置页。
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── 特征计算（口径 = docs/14 §七 预注册标准卡）────────────────────────────

def compute_cvd_div(klines: list[list]) -> Optional[dict]:
    """CARD-01 cvd_div：CVD（累计主动买卖差）与价格背离（订单流免费子集）。

    口径：近 24 根已收盘K线，CVD_t = Σ(2×takerBuy − volume)（index 9 taker 买量）；
    price_chg = 收盘价首尾涨幅%，cvd_chg = CVD 首尾涨幅%（相对 |CVD 起点|，
    起点为 0 时用绝对差归一为成交量比）。桶：
    - diverge_bear：价升 >1% 且 CVD 未跟随（多头信号质量差）
    - diverge_bull：价跌 <-1% 且 CVD 未跟随（空头信号质量差）
    - confirm：价量同向
    样本不足（<26 根）返回 None（不打标，不产生缺失组伪统计）。
    """
    if not klines or len(klines) < 26:
        return None
    closed = klines[:-1][-24:]
    if len(closed) < 24:
        return None
    c = np.array([float(k[4]) for k in closed], dtype=float)
    v = np.array([float(k[5]) for k in closed], dtype=float)
    tb = np.array([float(k[9]) if len(k) > 9 else k[5] for k in closed], dtype=float)
    price_chg = (c[-1] / c[0] - 1) * 100
    cvd = 2 * tb - v  # 每根净主动买量
    cvd_start = float(cvd[0])
    cvd_abs_start = float(np.abs(cvd[:6]).sum()) or float(v[:6].sum()) or 1.0
    cvd_chg_pct = (float(cvd[-1]) - cvd_start) / cvd_abs_start * 100
    if price_chg > 1.0 and cvd_chg_pct < 0:
        bucket = "diverge_bear"
    elif price_chg < -1.0 and cvd_chg_pct > 0:
        bucket = "diverge_bull"
    else:
        bucket = "confirm"
    return {
        "price_chg_pct": round(price_chg, 3),
        "cvd_chg_pct": round(cvd_chg_pct, 3),
        "bucket": bucket,
    }


def compute_vol_regime(klines: list[list]) -> Optional[dict]:
    """CARD-03 vol_regime：波动率状态（ATR14/ATR100，宏观/CTA regime 过滤）。

    口径：1h 已收盘K线 Wilder ATR（atr_series，strategy/technical.py），
    ratio = ATR14 最新值 ÷ ATR100 最新值。桶：
    - compress：ratio < 0.6（波动率收缩，突破缺燃料，假突破重灾区）
    - expand：ratio > 1.4（波动扩张）
    - normal：其余
    样本不足（ATR100 未成形）返回 None。
    """
    from app.services.strategy.technical import atr_series

    a14 = atr_series(klines, period=14)
    a100 = atr_series(klines, period=100)
    if a14 is None or a100 is None:
        return None
    a14_last, a100_last = float(a14[-1]), float(a100[-1])
    if not np.isfinite(a14_last) or not np.isfinite(a100_last) or a100_last <= 0:
        return None
    ratio = a14_last / a100_last
    if ratio < 0.6:
        bucket = "compress"
    elif ratio > 1.4:
        bucket = "expand"
    else:
        bucket = "normal"
    return {"atr_ratio": round(ratio, 4), "bucket": bucket}


def compute_macd_div(klines: list[list]) -> Optional[dict]:
    """CARD-04 macd_div：MACD 背离（缠论盘整背驰可取子集）。

    口径（technical.analyze_macd_div）：近 24 根已收盘K线前后半段比较——
    - diverge_bear：后半段价格新高而 MACD 柱峰值衰减（多头信号质量差）
    - diverge_bull：后半段价格新低而柱低谷抬升（空头信号质量差）
    - confirm：其余（与 cvd_div 桶语义对齐）
    样本不足（MACD 柱序列不足 24 根）返回 None。
    """
    from app.services.strategy.technical import analyze_macd_div
    return analyze_macd_div(klines)


def compute_oi_conf_scan_stub(klines: list[list]) -> None:
    """oi_conf 扫描侧占位：该特征需要 symbol 维度的 OI 快照环与网络请求，
    无法从单币 klines 现算——扫描循环对本特征恒得 None 不打标；
    实际打标由 tag_oi_conf 在 AI 配额段（tasks/ai_tasks.py）执行。
    注册表占位仅为让评估报告（build_report）自动覆盖该特征元数据。"""
    return None


# ── 特征注册表（docs/14 §四）────────────────────────────────────────────

@dataclass
class ShadowFeature:
    key: str
    name: str
    card: str                      # 预注册标准卡（docs/14 §七）
    compute: Callable[[list], Optional[dict]]
    min_sample: int                # 卡的最小样本（有效归因计数），达标前 sufficient=False


SHADOW_FEATURES: dict[str, ShadowFeature] = {
    f.key: f for f in (
        ShadowFeature("cvd_div", "CVD 背离（订单流免费子集）", "CARD-01", compute_cvd_div, 80),
        ShadowFeature("vol_regime", "波动率状态（ATR14/ATR100）", "CARD-03", compute_vol_regime, 100),
        ShadowFeature("macd_div", "MACD 背离（缠论背驰子集）", "CARD-04", compute_macd_div, 60),
        ShadowFeature("oi_conf", "OI 确认度（24h ΔOI）", "CARD-02", compute_oi_conf_scan_stub, 60),
        # 早期保护反事实（docs/15 §五）：trade_record 宿主，打标不走扫描侧
        # compute 循环（由 tag_trade_close 在归因任务打标），注册仅为让
        # build_report 输出元数据与反事实统计。min_sample = 预注册 decided 门槛
        ShadowFeature("be_retrace", "BE@0.8R 保本反事实（A 腿）", "CARD-05", lambda k: None, 30),
        ShadowFeature("tp0_retrace", "TP0 微止盈反事实（B 腿）", "CARD-06", lambda k: None, 60),
        # 再入场前置（docs/13 #12，CARD-07）：止损后结构×续走条件概率，
        # trade_record 宿主——出场后 48h 路径在平仓时刻不存在，打标由每日
        # 回填（tag_reentry_backfill）执行，compute 占位仅为 build_report 元数据
        ShadowFeature("reentry_precond", "再入场前置（止损后结构×续走）", "CARD-07", lambda k: None, 60),
    )
}


# ── oi_conf（CARD-02，AI 侧，docs/14 §十清单4）──────────────────────────

OI_CONF_ENABLED = True  # 代码开关（§九：特征启停改代码不进配置页；紧急置 False 停打）

# 24h OI 快照环（进程内，照 market_data β 缓存模式）：symbol → [(ts, oi), ...]。
# 只在 AI 配额轮追加（每小时 ≤ 配额数个 symbol）；保留 48h 供跨日 24h 前值比较
_OI_RING: dict[str, list[tuple[float, float]]] = {}
_OI_RING_TTL_S = 48 * 3600
# 24h 前值容差窗口：定时扫描每小时一轮，取距 now-24h 最近的快照，
# 落在 [18h, 30h] 内才有效（漏扫几轮仍可比，节奏漂移过大不可比）
_OI_PREV_WINDOW_S = (18 * 3600, 30 * 3600)


def tag_oi_conf(scan_results: list) -> int:
    """AI 配额段 oi_conf 打标（CARD-02）：候选逐币拉当前 OI 快照入环，
    环内有 18~30h 前值才计算 ΔOI 分桶落 shadow_marks，否则只存快照不打标。

    分桶（CARD-02 预注册）：ΔOI 24h 符号——增=新钱入场 confirm、
    减=回补/去杠杆 diverge；信号方向（votes net 符号）只进 value_json，
    供评估期四象限细分，不参与分桶。幂等：uq_shadow_mark 重复打标覆盖。
    请求预算：每轮 ≤ len(候选)（配额默认 10）个只读请求（docs/14 §四）。
    任何异常全吞（影子铁律），返回实际落标数。
    """
    if not OI_CONF_ENABLED or not scan_results:
        return 0
    import time

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.services.exchange_pool import ExchangePool

    pool = ExchangePool()
    now = time.time()
    oi_now_map: dict[str, float] = {}
    for r in scan_results:
        if r.symbol in oi_now_map:
            continue
        try:
            oi_now_map[r.symbol] = pool.get_open_interest(r.symbol)
        except Exception as e:
            logger.debug("oi_conf OI 拉取失败（不打标）%s: %s", r.symbol, e)
    if not oi_now_map:
        return 0

    tagged = 0
    db = SessionLocal()
    try:
        for r in scan_results:
            sym = r.symbol
            if sym not in oi_now_map:
                continue
            oi_now = oi_now_map[sym]
            ring = _OI_RING.setdefault(sym, [])
            lo, hi = now - _OI_PREV_WINDOW_S[1], now - _OI_PREV_WINDOW_S[0]
            window = [p for p in ring if lo <= p[0] <= hi]
            prev = (
                min(window, key=lambda p: abs(p[0] - (now - 86400)))
                if window else None
            )
            # 快照入环（无论是否打标），TTL 清理 + 条数上限双保险
            ring.append((now, oi_now))
            _OI_RING[sym] = [p for p in ring if p[0] >= now - _OI_RING_TTL_S][-48:]

            if prev is None or prev[1] <= 0:
                continue  # 无 24h 前值：先存快照，本轮不打标
            oi_chg_pct = (oi_now - prev[1]) / prev[1] * 100
            votes = r.votes_json if isinstance(r.votes_json, dict) else {}
            net = votes.get("net") or 0
            value = {
                "oi_now": round(oi_now, 8), "oi_prev": round(prev[1], 8),
                "oi_chg_pct": round(oi_chg_pct, 3),
                "direction": "long" if net > 0 else "short" if net < 0 else None,
                "bucket": "confirm" if oi_chg_pct > 0 else "diverge",
            }
            decision = {
                "stage": "ai_quota", "signal_type": r.signal_type,
                "breakout_pct": float(r.breakout_pct or 0),
            }
            existing = db.execute(
                select(ShadowMark).where(
                    ShadowMark.feature_key == "oi_conf",
                    ShadowMark.ref_type == "scan_result",
                    ShadowMark.ref_id == r.id,
                )
            ).scalars().first()
            if existing:
                existing.value_json = value
                existing.decision_json = decision
            else:
                db.add(ShadowMark(
                    feature_key="oi_conf", ref_type="scan_result", ref_id=r.id,
                    symbol=sym, value_json=value, decision_json=decision,
                ))
            tagged += 1
        db.commit()
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("oi_conf 影子打标落库失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 早期保护反事实打标（docs/15 §五，CARD-05/06，trade_close 宿主）────────
#
# 与上面四特征（开仓前打标）不同：be_retrace / tp0_retrace 是「出场管理规则」
# 的影子——规则效果取决于持仓期价格路径，必须等平仓后对整段路径做反事实重放。
# 宿主 = trade_record（ref_type="trade_record"），打标点挂归因任务开头
# （tasks/review_tasks.run_ai_attribution_task，AI-Key 门之前，不依赖 LLM）。
# 铁律不变：任何异常吞掉；结果只进 shadow_marks，不回写 TradeRecord、不进 AI。

# BE@1R 巡检保本反事实参数（docs/15 §4.5 定稿 BE@0.8R）
BE_AT_R = 0.8
# TP0 前置微止盈反事实参数（docs/15 §4.2 定稿 0.5R/25%/gate1.2；B 腿已归档，
# 此处按 v0 定稿参数重放——变体比较靠 value_json.path 离线重算，不重拉 K 线）
TP0_R = 0.5
TP0_QTY = 0.25
TP0_GATE_R = 1.2
_PATH_MAX_POINTS = 60      # 路径数组上限（长持仓降采样）


def compute_early_protect(klines: list[list], rec) -> Optional[dict]:
    """CARD-05/06 共享路径基元：对一笔已平仓交易做持仓期逐根反事实重放。

    口径（docs/15 §二/§4.5）：R = |entry − 初始止损|，初始止损由调用方解包后
    以 rec.stop_initial 传入（TradeRecord.stop_loss 列是跟踪止损后的现值，
    不可用；正确来源 = OPEN 事件 detail.stop_loss 快照，见 tag_trade_close）。
    klines 为持仓期已收盘 1h K 线（时间升序，含开仓时刻所在的根）。

    返回 dict：共享基元（r_unit/stop_initial/path/mfe_r/mae_r/tp1_r/duration_h/
    direction/exit_reason/real_r）+ A 腿（be_retrace）+ B 腿（tp0_retrace）两组；
    无反事实意义（缺止损快照/风险额/无K线）返回 None 不打标。
    """
    entry = float(rec.entry_price or 0)
    stop0 = float(getattr(rec, "stop_initial", 0) or 0)
    risk = float(rec.risk_amount or 0)
    qty = float(rec.qty or 0)
    if entry <= 0 or stop0 <= 0 or risk <= 0 or qty <= 0 or not klines:
        return None
    d = 1 if (rec.direction or "long") == "long" else -1
    r_unit = abs(entry - stop0)
    if r_unit <= 0:
        return None

    def r_of(price: float) -> float:
        return d * (price - entry) / r_unit

    # 路径基元：逐根 [high_r, low_r]（对 R 空间），>48h 降采样保 ≤60 点
    path = [[round(r_of(float(k[2])), 4), round(r_of(float(k[3])), 4)] for k in klines]
    if len(klines) > _PATH_MAX_POINTS:
        step = -(-len(path) // _PATH_MAX_POINTS)
        path = path[::step]

    mfe_r = max(max(h, l) for h, l in path)
    mae_r = min(min(h, l) for h, l in path)
    tp1 = float(rec.tp1 or 0)
    tp1_r = r_of(tp1) if tp1 > 0 else None
    duration_h = None
    if rec.opened_at and rec.closed_at:
        duration_h = round((rec.closed_at - rec.opened_at).total_seconds() / 3600, 1)
    real_r = float(rec.realized_pnl or 0) / risk

    # ── A 腿反事实（BE@BE_AT_R 巡检保本，严格口径：触线K收盘后下一根生效）──
    armed = False
    a_r = real_r
    a_out = "carry"           # carry=窗口结束未触发，沿用真实出场
    hours_to_arm = None
    post_arm_below_entry = False
    for i, (hi_r, lo_r) in enumerate(path):
        hit_r = hi_r >= BE_AT_R if d > 0 else lo_r <= -BE_AT_R
        hit_sl = lo_r <= -1.0 if d > 0 else hi_r >= 1.0
        hit_be = lo_r <= 0.0 if d > 0 else hi_r >= 0.0
        armed_prev = armed
        if hit_r:
            armed = True      # 本根收盘后巡检武装，下一根起生效
            if hours_to_arm is None:
                hours_to_arm = round(((i + 1) * (duration_h / len(path)) if duration_h
                                      else (i + 1)), 1)
        if hit_sl and not armed_prev:
            a_r, a_out = -1.0, "loss_keep"
            break
        if armed_prev and hit_be:
            a_r, a_out = 0.0, "be_exit"
            post_arm_below_entry = True
            break

    # ── B 腿反事实（TP0 限价单挂上即守，成交即武装保本；docs/15 §4.2）──────
    gate = tp1_r is not None and tp1_r >= TP0_GATE_R
    tp0_r = d * TP0_R
    tp0_hit = False
    tp0_hit_i = -1
    if gate:
        for i, (hi_r, lo_r) in enumerate(path):
            if (hi_r >= tp0_r if d > 0 else lo_r <= tp0_r):
                tp0_hit, tp0_hit_i = True, i
                break
    b_r = real_r
    b_out = "carry"
    survived_pullback = None
    if gate and tp0_hit:
        # TP0 成交后：TP0 腿落袋 0.25×0.5R；剩余仓 SL=保本（entry±0，保守），
        # 之后先触保本（be_after_tp0）或 TP1（win_after_tp0 = TP1 份额的全程收益）
        b_r = TP0_QTY * TP0_R
        b_out = "tp0_only"
        for hi_r, lo_r in path[tp0_hit_i + 1:]:
            hit_be = lo_r <= 0.0 if d > 0 else hi_r >= 0.0
            hit_tp1 = hi_r >= tp1_r if d > 0 else lo_r <= tp1_r
            if hit_be:
                b_out = "be_after_tp0"  # 剩余仓 ≈0R
                break
            if hit_tp1:
                b_r += (1 - TP0_QTY) * tp1_r
                b_out = "win_after_tp0"
                survived_pullback = True
                break
        if b_out == "tp0_only":
            survived_pullback = True  # 撑到真实出场时刻未被保本扫出
        if b_out == "be_after_tp0":
            survived_pullback = False
    elif gate:
        b_out = "tp0_miss"        # 挂了但价格未到 0.5R：按真实出场（与 carry 同值）
    else:
        b_out = "below_gate"      # TP1 距离不足门控：不挂 TP0

    # 桶判据（docs/15 §五 评估层）
    if a_out in ("loss_keep", "be_exit") and real_r <= -0.5 and a_r >= -0.2:
        be_bucket = "saved"
    elif real_r > 0.05 and a_r <= 0.05:
        be_bucket = "injured"
    elif not armed:
        be_bucket = "no_arm"
    else:
        be_bucket = "carry"

    if b_out == "be_after_tp0" and real_r > 0.05:
        tp0_bucket = "injured"    # 真实赢利被保本扫出（误伤）
    elif b_out == "be_after_tp0" and real_r <= -0.5:
        tp0_bucket = "protected"  # 真实亏损被保护
    elif b_out == "win_after_tp0":
        tp0_bucket = "survived"   # 回踩后仍走到原出场
    elif b_out == "below_gate":
        tp0_bucket = "below_gate"
    else:
        tp0_bucket = "untouched"  # tp0_only / tp0_miss / carry（无反事实变化）

    return {
        # 共享路径基元
        "r_unit": round(r_unit, 8), "stop_initial": stop0,
        "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
        "tp1_r": round(tp1_r, 4) if tp1_r is not None else None,
        "duration_h": duration_h, "direction": rec.direction,
        "exit_reason": rec.exit_reason, "real_r": round(real_r, 4),
        "path": path,
        # A 腿（be_retrace）
        "a_r": round(a_r, 4), "a_out": a_out, "armed": armed,
        "hours_to_arm": hours_to_arm,
        "post_arm_below_entry": post_arm_below_entry,
        "a_delta_r": round(a_r - real_r, 4), "bucket": be_bucket,
        # B 腿（tp0_retrace）
        "b_r": round(b_r, 4), "b_out": b_out,
        "tp0_hit": tp0_hit, "gate": gate,
        "survived_pullback": survived_pullback,
        "b_delta_r": round(b_r - real_r, 4), "bucket_tp0": tp0_bucket,
    }


def tag_trade_close(trades: list) -> int:
    """trade_close 宿主打标（docs/15 §五）：对已平仓交易逐笔做早期保护反事实。

    每笔交易产生两行影子标记：be_retrace（CARD-05）+ tp0_retrace（CARD-06），
    value_json 同为 compute_early_protect 输出（path 基元共用，≈2KB/笔）。
    K 线由调用方（归因任务）按持仓区间拉取传入；无 K 线/缺快照的交易静默跳过。
    幂等：uq_shadow_mark 同 (feature_key, trade_record, ref_id) 覆盖更新。
    任何异常全吞（影子铁律），返回实际落标行数。
    """
    if not trades:
        return 0
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.services.exchange_pool import ExchangePool

    pool = ExchangePool()
    tagged = 0
    db = SessionLocal()
    try:
        # 初始止损快照（docs/15 口径）：OPEN 事件 detail.stop_loss——
        # stop_loss 列是跟踪止损后的现值不可用；批量一次查出避免逐笔查事件表
        trade_ids = [t.id for t in trades]
        from app.models.trade import TradeEvent
        open_events = db.execute(
            select(TradeEvent.trade_record_id, TradeEvent.detail)
            .where(
                TradeEvent.trade_record_id.in_(trade_ids),
                TradeEvent.event_type == "OPEN",
            )
        ).all()
        stop_map: dict = {}
        for tr_id, detail in open_events:
            try:
                sl = float((detail or {}).get("stop_loss") or 0)
            except (TypeError, ValueError):
                sl = 0.0
            if sl > 0:
                stop_map[tr_id] = sl
        for rec in trades:
            try:
                si = stop_map.get(rec.id)
                if si:
                    rec.stop_initial = si  # compute 读此属性（仅快照有值时挂）
                # 持仓期已收盘 1h K 线（开仓时刻起，到平仓为止）
                opened_ms = int(rec.opened_at.replace(tzinfo=None).timestamp() * 1000) \
                    if rec.opened_at else 0
                closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000) - 1 \
                    if rec.closed_at else 0
                if opened_ms <= 0 or closed_ms <= opened_ms:
                    continue
                kls = pool.get_klines_range(rec.symbol, "1h", 500, closed_ms) or []
                held = [k for k in kls if int(k[0]) >= opened_ms - 3600_000]
                if not held:
                    continue
                value = compute_early_protect(held, rec)
                if value is None:
                    continue
                decision = {
                    "stage": "trade_close",
                    "testnet": bool(rec.testnet),
                    "account_key": rec.account_key,
                    "status": rec.status,
                }
                for feat_key in ("be_retrace", "tp0_retrace"):
                    existing = db.execute(
                        select(ShadowMark).where(
                            ShadowMark.feature_key == feat_key,
                            ShadowMark.ref_type == "trade_record",
                            ShadowMark.ref_id == rec.id,
                        )
                    ).scalars().first()
                    if existing:
                        existing.value_json = value
                        existing.decision_json = decision
                    else:
                        db.add(ShadowMark(
                            feature_key=feat_key, ref_type="trade_record",
                            ref_id=rec.id, symbol=rec.symbol,
                            testnet=bool(rec.testnet),
                            value_json=value, decision_json=decision,
                        ))
                    tagged += 1
                db.commit()
            except Exception as e:
                db.rollback()
                logger.debug("trade_close 影子打标失败（忽略）%s: %s",
                             getattr(rec, "symbol", "?"), e)
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("trade_close 影子打标批量失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 再入场前置打标（docs/13 #12，CARD-07，trade_record 宿主，每日回填）────
#
# 回答 docs/13 #12 的硬前置问题：止损出场后价格是否同向续走（回到开仓价），
# 按出场时刻趋势结构是否完好分桶。出场后 48h 路径在平仓时刻不存在，
# 无法挂归因任务即时打标——由每日回填任务（tasks/shadow_tasks）调
# tag_reentry_backfill 对出窗止损单补打标，宿主同为 trade_record。
# 口径预注册（2026-10-06 锁定，事后不可回调）：
# - 总体 = CLOSED 且 exit_reason="sl"（docs/13 #12 语义：止损出场单）
# - 结构完好 structure_ok = 出场时三件全过：① EMA 排列（多头 e21>e55，
#   e144 可用时加 e55>e144，空头对称）② ADX(14,1h) ≥ 20 ③ R² ≥ 0.5
#   （止损前 48 根已收盘收盘价线性回归）；数据不可判不打标
# - 续走（主判据）= 出场后 24h 内 signed-R MFE ≥ 0（价格回到开仓价）；
#   value_json 另存 48h 全窗 post_path/mfe_post_r/hours_to_reentry 供离线
#   敏感性（N=12/24/48h、阈值 0/0.5/1R），不重拉 K 线
# - 裁决门槛：正式网 structure_ok 桶 n≥60 且 P(续走|structure_ok) ≥ 40%
#   → #12 进入预注册回放；< 40% → #12 直接关闭

REENTRY_EXIT_REASON = "sl"   # 预注册总体口径：仅止损出场单
REENTRY_POST_H = 48          # 出场后观察窗（h）
REENTRY_CONT_H = 24          # 主判据窗口（h）
REENTRY_LOOKBACK_H = 350     # 结构窗回看（h；EMA144 需 ~245 根已收盘）
REENTRY_FETCH_LIMIT = 400    # 一次拉取根数（350 结构窗 + 48 续走窗）
REENTRY_R2_BARS = 48         # R² 回归窗（根）
REENTRY_R2_MIN = 0.5         # R² 门槛（= 扫描 R_SQUARED_THRESHOLD 默认）
REENTRY_ADX_MIN = 20.0       # ADX 门槛（analyze_adx 趋势酝酿语义）


def compute_reentry_precond(klines: list[list], rec) -> Optional[dict]:
    """CARD-07 reentry_precond：止损单出场结构快照 + 出场后同向续走重放。

    klines = 截至 closed_at + REENTRY_POST_H 的已收盘 1h K 线（时间升序，
    恰含出场前 ~350h 结构窗与出场后 48h 续走窗）。R 空间同 CARD-05/06
    （signed-R：正向 = 对持仓有利）。返回 None = 不打标
    （缺止损快照 / 结构数据不可判 / 出场后无路径）。
    """
    from app.services.strategy.ema import calc_ema
    from app.services.strategy.swing import linear_regression
    from app.services.strategy.technical import analyze_adx

    entry = float(rec.entry_price or 0)
    stop0 = float(getattr(rec, "stop_initial", 0) or 0)
    if entry <= 0 or stop0 <= 0 or not klines:
        return None
    d = 1 if (rec.direction or "long") == "long" else -1
    r_unit = abs(entry - stop0)
    if r_unit <= 0:
        return None

    def r_of(price: float) -> float:
        return d * (price - entry) / r_unit

    closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000)
    exit_bar_open = closed_ms // 3_600_000 * 3_600_000  # 平仓所在根（该根不入任何窗口）
    pre = [k for k in klines if int(k[0]) + 3_600_000 <= closed_ms]
    post = [k for k in klines if int(k[0]) > exit_bar_open][:REENTRY_POST_H]
    closes = np.array([float(k[4]) for k in pre], dtype=float)
    # ADX(14) 最少 44 根 + EMA55 余量；无出场后路径则续走不可判
    if len(closes) < 66 or not post:
        return None

    # ① EMA 排列（e144 不可用降级 21/55 双线，ema144_used 记录口径）
    e21 = calc_ema(closes, 21)
    e55 = calc_ema(closes, 55)
    e144 = calc_ema(closes, 144)
    if e21 is None or e55 is None:
        return None
    f, m = float(e21[-1]), float(e55[-1])
    if d > 0:
        ema_ok = f > m and (e144 is None or m > float(e144[-1]))
    else:
        ema_ok = f < m and (e144 is None or m < float(e144[-1]))
    # ② ADX（analyze_adx 内部剔尾，pre 已全为已收盘根，无害）
    adx_v = analyze_adx(pre, period=14)
    adx = float(adx_v["adx"]) if adx_v and adx_v.get("adx") is not None else None
    adx_ok = adx is not None and adx >= REENTRY_ADX_MIN
    # ③ R²（近 48 根收盘线性回归）
    win = closes[-REENTRY_R2_BARS:]
    _slope, _icpt, r2 = linear_regression(np.arange(len(win)), win)
    r2_ok = float(r2) >= REENTRY_R2_MIN
    structure_ok = bool(ema_ok and adx_ok and r2_ok)

    # 出场后路径（signed-R，正值 = 对持仓方向有利）与续走判定
    post_path = [[round(r_of(float(k[2])), 4), round(r_of(float(k[3])), 4)] for k in post]
    mfe_post_r = max(max(h, l) for h, l in post_path)
    cont_end_ms = exit_bar_open + REENTRY_CONT_H * 3_600_000
    hours_to_reentry = None
    continued_24h = False
    for k, (h_r, l_r) in zip(post, post_path):
        if int(k[0]) > cont_end_ms:
            break
        if max(h_r, l_r) >= 0.0:
            continued_24h = True
            hours_to_reentry = round((int(k[0]) - closed_ms) / 3_600_000, 1)
            break
    real_r = None
    risk = float(rec.risk_amount or 0)
    if risk > 0:
        real_r = round(float(rec.realized_pnl or 0) / risk, 4)
    return {
        "r_unit": round(r_unit, 8), "stop_initial": stop0,
        "direction": rec.direction, "exit_reason": rec.exit_reason,
        "real_r": real_r,
        "structure_ok": structure_ok,
        "bucket": "structure_ok" if structure_ok else "structure_broken",
        "ema_ok": bool(ema_ok), "ema144_used": e144 is not None,
        "adx": round(adx, 2) if adx is not None else None, "adx_ok": bool(adx_ok),
        "r2": round(float(r2), 4), "r2_ok": bool(r2_ok),
        "post_path": post_path, "mfe_post_r": round(mfe_post_r, 4),
        "continued_24h": continued_24h, "hours_to_reentry": hours_to_reentry,
        "continued_48h": bool(mfe_post_r >= 0.0),
    }


def tag_reentry_backfill(batch: int = 40, days: int = 90) -> int:
    """CARD-07 每日回填（docs/13 #12 前置卡）：出场 48h 观察窗已过的止损单补打标。

    入选（预注册总体）：CLOSED + exit_reason="sl" + closed_at ∈
    [now-days, now-48h] + 尚无 reentry_precond 标记；每轮 ≤ batch 笔
    （每笔 1 次 K 线请求预算）。K 线 = 截至 closed_at+48h 的最近
    REENTRY_FETCH_LIMIT 根 1h（一次请求覆盖结构窗 + 续走窗）。
    已标记单永不重打（出场后路径以首个 48h 窗为准）。铁律不变：
    任何异常吞掉；结果只进 shadow_marks，不回写 TradeRecord。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.models.trade import TradeEvent, TradeRecord
    from app.services.exchange_pool import ExchangePool

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        done = set(db.execute(
            select(ShadowMark.ref_id).where(
                ShadowMark.feature_key == "reentry_precond")
        ).scalars().all())
        rows = db.execute(
            select(TradeRecord).where(
                TradeRecord.status == "CLOSED",
                TradeRecord.exit_reason == REENTRY_EXIT_REASON,
                TradeRecord.closed_at.isnot(None),
                TradeRecord.closed_at >= now - timedelta(days=days),
                TradeRecord.closed_at <= now - timedelta(hours=REENTRY_POST_H),
            ).order_by(TradeRecord.closed_at.desc())
        ).scalars().all()
        pending = [t for t in rows if t.id not in done][:batch]
        if not pending:
            return 0
        # 初始止损快照（同 tag_trade_close 口径：OPEN 事件 detail.stop_loss）
        events = db.execute(
            select(TradeEvent.trade_record_id, TradeEvent.detail).where(
                TradeEvent.trade_record_id.in_([t.id for t in pending]),
                TradeEvent.event_type == "OPEN",
            )
        ).all()
        stop_map: dict = {}
        for tr_id, detail in events:
            try:
                sl = float((detail or {}).get("stop_loss") or 0)
            except (TypeError, ValueError):
                sl = 0.0
            if sl > 0:
                stop_map[tr_id] = sl
        pool = ExchangePool()
        tagged = 0
        for rec in pending:
            try:
                si = stop_map.get(rec.id)
                if not si:
                    continue  # 无初始止损快照：R 空间不可算，跳过
                rec.stop_initial = si  # compute 读此属性
                closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000)
                kls = pool.get_klines_range(
                    rec.symbol, "1h", REENTRY_FETCH_LIMIT,
                    closed_ms + REENTRY_POST_H * 3_600_000,
                ) or []
                value = compute_reentry_precond(kls, rec)
                if value is None:
                    continue
                decision = {
                    "stage": "reentry_backfill",
                    "testnet": bool(rec.testnet),
                    "account_key": rec.account_key,
                    "status": rec.status,
                }
                existing = db.execute(
                    select(ShadowMark).where(
                        ShadowMark.feature_key == "reentry_precond",
                        ShadowMark.ref_type == "trade_record",
                        ShadowMark.ref_id == rec.id,
                    )
                ).scalars().first()
                if existing:
                    existing.value_json = value
                    existing.decision_json = decision
                else:
                    db.add(ShadowMark(
                        feature_key="reentry_precond", ref_type="trade_record",
                        ref_id=rec.id, symbol=rec.symbol,
                        testnet=bool(rec.testnet),
value_json=value, decision_json=decision,
                    ))
                db.commit()  # 逐笔提交：单笔失败回滚不拖累已打标行
                tagged += 1
            except Exception as e:
                db.rollback()
                logger.debug("reentry_backfill 影子打标失败（忽略）%s: %s",
                             getattr(rec, "symbol", "?"), e)
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("reentry_backfill 影子回填批量失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 归因与评估（docs/14 §五/§六）────────────────────────────────────────
def _bucket_stats(rows: list) -> dict:
    """一批已归因标记 → 统计。outcome ∈ win_tp1/win_tp2/loss/expired/no_analysis/
    pending；win = win_tp1/win_tp2；R 仅 trade 宿主有。"""
    n = len(rows)
    wins = 0
    counted = 0
    for _b, outcome, _r in rows:
        if outcome in ("win_tp1", "win_tp2", "loss"):
            counted += 1
            wins += 1 if outcome.startswith("win") else 0
    return {"n": n, "counted": counted, "win_rate": round(wins / counted, 4) if counted else None}


def _trade_protect_stats(rows: list, delta_key: str) -> dict:
    """早期保护特征（trade_record 宿主）分桶统计：反事实读数为主。

    rows = value_json dict 列表（compute_early_protect 输出，A/B 两组字段同构）；
    delta_key 区分特征视角（be_retrace→a_delta_r / tp0_retrace→b_delta_r）。
    核心读数：n / decided / sum_delta_r（净改善）/ avg_real_r / avg_cf_r；
    胜率口径在此不适用（bucket 已是 saved/injured 语义而非 outcome）。"""
    n = len(rows)
    if not n:
        return {"n": 0, "decided": 0, "sum_delta_r": 0.0,
                "avg_real_r": None, "avg_cf_r": None}
    deltas = [float(r.get(delta_key, 0)) for r in rows]
    real = [float(r.get("real_r", 0)) for r in rows]
    cf = [real[i] + deltas[i] for i in range(n)]
    # decided = 反事实改变了结局的笔数（|delta|>0.05R，即 saved+injured 合计）
    decided = sum(1 for d in deltas if abs(d) > 0.05)
    return {
        "n": n,
        "decided": decided,
        "sum_delta_r": round(sum(deltas), 3),
        "avg_real_r": round(sum(real) / n, 4),
        "avg_cf_r": round(sum(cf) / n, 4),
    }


def _reentry_stats(rows: list) -> dict:
    """再入场前置卡（CARD-07，docs/13 #12）统计：n / continued / cont_rate。

    continued = 出场后 24h 内价格回到开仓价（signed-R MFE ≥ 0）的笔数；
    cont_rate = continued / n（止损后同向续走条件概率的主读数）。
    structure_ok_n 为第二前置读数（结构完好占比，docs/13 #12 预估 30-40%）；
    bucket 本身即 structure_ok/broken 语义，胜率与反事实口径均不适用。"""
    n = len(rows)
    if not n:
        return {"n": 0, "continued": 0, "cont_rate": None, "structure_ok_n": 0}
    cont = sum(1 for r in rows if r.get("continued_24h"))
    ok = sum(1 for r in rows if r.get("bucket") == "structure_ok")
    return {
        "n": n,
        "continued": cont,
        "cont_rate": round(cont / n, 4),
        "structure_ok_n": ok,
    }


def build_report(db, window_days: int = 30) -> list[dict]:
    """影子评估报告（docs/14 §六）：按特征分桶 join 复盘结果统计。

    归因链（scan_result 宿主）：ScanResult → AIAnalysis → TradeReview.outcome；
    无 AI 分析的标记记 no_analysis（配额未进），未复盘的记 pending 下轮再看。
    早期保护特征（be_retrace/tp0_retrace，trade_record 宿主，docs/15 §五）：
    跳过 outcome 归因链（其 bucket 本身即 saved/injured 语义），按反事实 R 统计。
    评估不回填 marks 行（避免双写漂移）。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models.scan import AIAnalysis, ScanResult, TradeReview
    from app.models.shadow import ShadowMark

    since = datetime.utcnow() - timedelta(days=window_days)
    out: list[dict] = []
    for key, feat in SHADOW_FEATURES.items():
        marks = db.execute(
            select(ShadowMark).where(
                ShadowMark.feature_key == key,
                ShadowMark.created_at >= since,
            ).order_by(ShadowMark.created_at)
        ).scalars().all()
        if not marks:
            out.append({"feature": key, "name": feat.name, "card": feat.card,
                        "min_sample": feat.min_sample,
                        "window_days": window_days, "sufficient": False,
                        "buckets": [], "baseline": {"n": 0, "counted": 0, "win_rate": None}})
            continue

        # ── 早期保护特征（trade_record 宿主）：反事实统计分支 ────────────
        if feat.card in ("CARD-05", "CARD-06"):
            # 分层口径（docs/15 §5.3）：正式网为主判定组（decided/sufficient 基数），
            # 测试网单独成组——两网人群结构差异大（测试网首轮 77 笔以赢家为主，
            # 全量混算会把赢家误伤成本错记进正式网口径），测试网只作环境差异证据
            prod_values: list = []
            testnet_values: list = []
            for m in marks:
                v = m.value_json or {}
                if (m.decision_json or {}).get("testnet"):
                    testnet_values.append(v)
                else:
                    prod_values.append(v)
            delta_key = "a_delta_r" if key == "be_retrace" else "b_delta_r"
            bucket_key = "bucket" if key == "be_retrace" else "bucket_tp0"
            baseline = _trade_protect_stats(prod_values, delta_key)
            buckets: dict[str, list] = {}
            for v in prod_values:
                buckets.setdefault(v.get(bucket_key, "unknown"), []).append(v)
            bucket_out = []
            for b, rows in sorted(buckets.items()):
                st = _trade_protect_stats(rows, delta_key)
                if st["n"] < 30:
                    st["insufficient"] = True
                bucket_out.append({"bucket": b, **st})
            t_buckets: dict[str, int] = {}
            for v in testnet_values:
                b = v.get(bucket_key, "unknown")
                t_buckets[b] = t_buckets.get(b, 0) + 1
            out.append({
                "feature": key, "name": feat.name, "card": feat.card,
                "min_sample": feat.min_sample,
                "window_days": window_days,
                "baseline": baseline,
                "buckets": bucket_out,
                # 对照组：测试网读数 + 桶分布计数，不进判定
                "testnet_baseline": {
                    **_trade_protect_stats(testnet_values, delta_key),
                    "buckets": dict(sorted(t_buckets.items())),
                },
                # 达标判定：预注册卡按正式网 decided 数（saved+injured），
                # carry/no_arm 与测试网样本不计入
                "decided": baseline.get("decided", 0),
                "sufficient": baseline.get("decided", 0) >= feat.min_sample,
            })
            continue

        # ── 再入场前置（CARD-07，docs/13 #12）：结构桶 × 续走率 ───────────
        # 第三种宿主语义（scan_result 归因链 / CARD-05/06 反事实之外）：
        # bucket = structure_ok/broken，读数 = 止损后 24h 同向续走条件概率。
        # 分层同 CARD-05/06：正式网主判定组，测试网对照组不进判定。
        if feat.card == "CARD-07":
            prod_values: list = []
            testnet_values: list = []
            for m in marks:
                v = m.value_json or {}
                if (m.decision_json or {}).get("testnet"):
                    testnet_values.append(v)
                else:
                    prod_values.append(v)
            baseline = _reentry_stats(prod_values)
            buckets: dict[str, list] = {}
            for v in prod_values:
                buckets.setdefault(v.get("bucket", "unknown"), []).append(v)
            bucket_out = []
            for b, rows in sorted(buckets.items()):
                st = _reentry_stats(rows)
                if st["n"] < 30:
                    st["insufficient"] = True
                bucket_out.append({"bucket": b, **st})
            t_buckets: dict[str, int] = {}
            for v in testnet_values:
                b = v.get("bucket", "unknown")
                t_buckets[b] = t_buckets.get(b, 0) + 1
            out.append({
                "feature": key, "name": feat.name, "card": feat.card,
                "min_sample": feat.min_sample,
                "window_days": window_days,
                "baseline": baseline,
                "buckets": bucket_out,
                # 对照组：测试网读数（同 _reentry_stats 形状）+ 桶分布计数
                "testnet_baseline": {
                    **_reentry_stats(testnet_values),
                    "buckets": dict(sorted(t_buckets.items())),
                },
                # 达标判定：正式网打标笔数（两桶都计入——分桶本身是读数）
                "sufficient": baseline.get("n", 0) >= feat.min_sample,
            })
            continue

        # ── 常规特征（scan_result 宿主）：outcome 归因链统计 ─────────────
        # 批量取宿主链结果：ref_id → (outcome)
        ref_ids = [m.ref_id for m in marks]
        sr_rows = db.execute(
            select(ScanResult.id, AIAnalysis.id, TradeReview.outcome)
            .outerjoin(AIAnalysis, AIAnalysis.scan_result_id == ScanResult.id)
            .outerjoin(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
            .where(ScanResult.id.in_(ref_ids))
        ).all()
        outcome_map: dict = {}
        for sr_id, _ai_id, outcome in sr_rows:
            # 同一 scan_result 理论一条 AIAnalysis（唯一约束），outerjoin 重复行取首个非空
            if sr_id not in outcome_map or outcome_map[sr_id] is None:
                outcome_map[sr_id] = outcome
        buckets: dict[str, list] = {}
        for m in marks:
            # 归一：宿主缺失（ScanResult 被清）→ no_analysis；有 AIAnalysis
            # 无 TradeReview → pending（未到 24h，下轮再看）
            outcome = outcome_map.get(m.ref_id)
            label = "no_analysis" if m.ref_id not in outcome_map else (outcome or "pending")
            b = (m.value_json or {}).get("bucket", "unknown")
            buckets.setdefault(b, []).append((b, label, None))
        bucket_out = [
            {"bucket": b, **_bucket_stats(rows)}
            for b, rows in sorted(buckets.items())
        ]
        for item in bucket_out:
            if item["n"] < 30:
                item["insufficient"] = True
        out.append({
            "feature": key, "name": feat.name, "card": feat.card,
            "min_sample": feat.min_sample,
            "window_days": window_days,
            "baseline": _bucket_stats([r for rows in buckets.values() for r in rows]),
            "buckets": bucket_out,
            # 达标判定：有效归因样本 ≥ 卡最小样本（docs/14 §七 预注册后不可回调）
            "sufficient": _bucket_stats([r for rows in buckets.values() for r in rows])["counted"] >= feat.min_sample,
        })
    return out
