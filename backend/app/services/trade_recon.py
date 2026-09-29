"""逐笔 R 泄漏对账（docs/24 A1 查因先行）

背景：tp1_then_sl 出场单理论约 +0.75R、实际仅约 +0.04R——每笔约 0.7R 的系统性
泄漏（滑点/穿价/费用/资金费/分批数量不符）。本服务逐笔对照「计划 vs 实际」，
把泄漏拆解为五项：

a) 入场滑点成本（实际成交 vs 计划 entry，方向性换算：不利 = 正）
b) 手续费（|fee_total|）
c) 资金费（换算为成本口径：交易所 funding_total 负 = 支付 → 正成本；收到则为负抵扣）
d) SL 穿价成本（保本/移动 SL 成交价相对触发价，取 SL_FILL 事件明细）
e) TP1 滑点（TP1 实际成交均价 vs 计划 TP1；成交均价由分档价差盈亏反推）

每项输出 USDT 与 R 双口径（R = 金额 / risk_amount），并给出 tp1_then_sl 理论 R
（tp1 档位占比 × RR1 + 余档按保本 0）与实际 R 对照：总泄漏 = 理论 − 实际，
covered = a~e 可得项之和，unexplained = 总泄漏 − covered（偏差大时在 gaps 提示）。

防御式约定：任一数据缺失对应项 null，不抛异常；交易所查询仅在记录结算字段
缺失（fee/funding 回补）或明细缺分档盈亏（tranche 拆分）时惰性发起，失败落
gaps（中文一句）。
"""
import logging
from datetime import timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trade import TradeEvent, TradeRecord
from app.services.binance_trader import BinanceTrader

logger = logging.getLogger(__name__)

# TP 分批回退比例（与 trade_engine._tp_quantities 同规则；常量就地写避免并行改动耦合）：
# 有 TP2 → TP1 50% / TP2 25%（余 25% 跟进退出）；无 TP2 → TP1 一次止盈 75%
_TP1_RATIO_WITH_TP2, _TP2_RATIO_WITH_TP2 = 0.5, 0.25
_TP1_RATIO_NO_TP2 = 0.75
# TP1 实际成交数量 vs 计划分批的容差（超出记 gap）
_QTY_TOL = 0.01
# 未解释泄漏超过该 R 数 → gaps 提示可能有未入账成本
_UNEXPLAINED_R_HINT = 0.05


def _f(v) -> Optional[float]:
    """防御式数值转换（Numeric 列 → Decimal / JSON 可能存 str / None）"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(*vals) -> Optional[float]:
    """第一个可转数值（0 是合法数值，不跳过）"""
    for v in vals:
        f = _f(v)
        if f is not None:
            return f
    return None


def _dir_sign(direction: Optional[str]) -> float:
    """方向符号：long +1 / 其余按 short −1（成本口径：成交比计划更差 → 正）"""
    return -1.0 if (direction or "").lower() == "short" else 1.0


def _r_of(usdt: Optional[float], risk: Optional[float]) -> Optional[float]:
    """USDT → R（risk 缺失或非正 → None）"""
    if usdt is None or not risk or risk <= 0:
        return None
    return round(usdt / risk, 4)


def _item(usdt: Optional[float], risk: Optional[float]) -> Optional[dict]:
    """泄漏项双口径（不可得 → null）"""
    if usdt is None:
        return None
    return {"usdt": round(usdt, 4), "r": _r_of(usdt, risk)}


def _since_ms(rec: TradeRecord) -> int:
    """交易所查询起点：opened_at（UTC naive）→ ms，前移 5s 容错时钟偏差"""
    if not rec.opened_at:
        return 0
    return int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000


def _resolve_trader(rec: TradeRecord) -> BinanceTrader:
    """按记录归属账号解析 trader（docs/17）：account_key 缺失旧行按 testnet 回落；
    账号已从 .env 移除时回落环境默认账号（回落链与 actual_review.backfill_costs
    的 _rec_trader 一致）。"""
    from app.services.account_registry import get_account

    key = rec.account_key or ("testnet-1" if rec.testnet else "live-1")
    if get_account(key):
        return BinanceTrader(key)
    return BinanceTrader(rec.testnet)


def _income_costs(rec: TradeRecord, trader: BinanceTrader) -> tuple[Optional[float], Optional[float]]:
    """交易所 income 明细 →（手续费, 资金费）回补值（异常抛给调用方落 gaps）"""
    rows = trader.income_rows(rec.symbol, _since_ms(rec))
    fee = sum(float(r.get("income") or 0) for r in rows if r.get("incomeType") == "COMMISSION")
    funding = sum(float(r.get("income") or 0) for r in rows if r.get("incomeType") == "FUNDING_FEE")
    return round(fee, 6), round(funding, 6)


def reconcile_trade(db: Session, rec: TradeRecord, trader: Optional[BinanceTrader] = None) -> dict:
    """单笔交易对账：计划/实际/泄漏分解/缺口提示（结构见模块 docstring）。

    trader 可注入（测试桩）；None 时按记录归属账号惰性解析，仅在有数据缺口
    需要查交易所时才构造。
    """
    gaps: list[str] = []

    # ── 操作历史事件（成交明细来源；查询失败不阻断，对应项 null）──
    evts: list = []
    try:
        evts = db.execute(
            select(TradeEvent)
            .where(TradeEvent.trade_record_id == rec.id)
            .order_by(TradeEvent.created_at.asc())
        ).scalars().all()
    except Exception as e:
        gaps.append(f"交易事件历史查询失败：{e}")
    open_detail: dict = next((e.detail or {} for e in evts if e.event_type == "OPEN"), None) or {}
    tp1_ev = next((e for e in evts if e.event_type == "TP1_FILL"), None)
    sl_ev = next((e for e in evts if e.event_type == "SL_FILL"), None)
    tp1_detail: dict = (tp1_ev.detail or {}) if tp1_ev else {}
    sl_detail: dict = (sl_ev.detail or {}) if sl_ev else {}

    snap = rec.ai_snapshot or {}
    raw = rec.raw or {}
    sign = _dir_sign(rec.direction)
    risk = _f(rec.risk_amount)
    if risk is None or risk <= 0:
        gaps.append("缺失 risk_amount，无法换算 R 口径")

    # ── plan：计划三价（AI 快照优先，缺则回落记录现值）+ 计划分批数量 ──
    plan_entry = _pick(snap.get("entry_price"), rec.entry_price)
    plan_sl = _pick(snap.get("stop_loss"), rec.stop_loss)
    plan_tp1 = _pick(snap.get("take_profit_1"), rec.tp1)
    plan_tp2 = _pick(snap.get("take_profit_2"), rec.tp2) or None  # 0 = 未设 TP2
    if plan_entry is None:
        gaps.append("缺失计划入场价，入场滑点无法量化")
    if plan_sl is None:
        gaps.append("缺失计划止损，RR 口径无法计算")
    if plan_tp1 is None:
        gaps.append("缺失计划 TP1，TP1 滑点与理论 R 无法量化")

    qty_total = _pick(rec.qty, open_detail.get("qty"))
    qty_tp1 = _pick(raw.get("qty_tp1"))
    qty_tp2 = _pick(raw.get("qty_tp2"))
    if qty_tp1 is None and qty_total:
        # 旧行 raw 缺分批数量：按引擎同规则估算并标记（有 TP2 50%/25%，无 75%/0%）
        if plan_tp2:
            qty_tp1, qty_tp2 = qty_total * _TP1_RATIO_WITH_TP2, qty_total * _TP2_RATIO_WITH_TP2
        else:
            qty_tp1, qty_tp2 = qty_total * _TP1_RATIO_NO_TP2, 0.0
        gaps.append("raw 缺分批数量，按引擎规则估算（有 TP2 50%/25%、无 75%/0%）")
    if qty_total is None:
        gaps.append("缺失开仓数量，理论 R 无法计算")

    # ── 惰性账号解析：仅在需要查交易所回补时构造（失败置 False 不重试）──
    trader_box: list = [trader]

    def _trader() -> Optional[BinanceTrader]:
        t = trader_box[0]
        if t is False:
            return None
        if t is not None:
            return t
        try:
            trader_box[0] = _resolve_trader(rec)
        except Exception as e:
            gaps.append(f"交易账号解析失败，交易所回补不可用：{e}")
            trader_box[0] = False
            return None
        return trader_box[0]

    # ── actual：实际成交与结算字段（fee/funding 缺失时查 income 回补）──
    actual_entry = _pick(open_detail.get("fill_price"), open_detail.get("entry_price"), rec.entry_price)
    if actual_entry is None:
        gaps.append("缺失实际成交价（无 OPEN 事件且记录 entry_price 为空）")
    slippage_pct = _f(open_detail.get("slippage_pct"))

    fee_val = _f(rec.fee_total)
    fund_val = _f(rec.funding_total)
    realized = _f(rec.realized_pnl)
    if fee_val is None or fund_val is None:
        t = _trader()
        if t is not None:
            try:
                inc_fee, inc_fund = _income_costs(rec, t)
                if fee_val is None:
                    fee_val = inc_fee
                if fund_val is None:
                    fund_val = inc_fund
            except Exception as e:
                gaps.append(f"income 明细拉取失败：{e}")

    # TP1 成交明细：TP1_FILL 事件（qty/pnl），无分档盈亏时用交易所成交明细拆分
    tp1_qty_actual = _pick(tp1_detail.get("qty"), qty_tp1)
    tp1_pnl = _f(tp1_detail.get("pnl"))
    tp1_time = tp1_ev.created_at.isoformat() if (tp1_ev is not None and tp1_ev.created_at) else None
    if tp1_pnl is None and plan_tp1 is not None and qty_tp1 is not None and qty_tp2 is not None:
        t = _trader()
        if t is not None:
            try:
                tp1_pnl, _pnl2 = t.fill_tranche_pnls(rec.symbol, _since_ms(rec), qty_tp1, qty_tp2)
            except Exception as e:
                gaps.append(f"交易所成交明细无法拆分 TP1/TP2 分档盈亏：{e}")
    if tp1_pnl is None:
        gaps.append("缺失 TP1 成交分档盈亏明细，TP1 滑点无法量化")

    # SL 成交明细：SL_FILL（触发价/成交价）——当前结算流程不写该事件，常缺
    sl_trigger = _pick(sl_detail.get("trigger_price"), sl_detail.get("trigger"), sl_detail.get("stop_price"))
    sl_fill = _pick(sl_detail.get("price"), sl_detail.get("fill_price"), sl_detail.get("avg_price"))
    sl_time = sl_ev.created_at.isoformat() if (sl_ev is not None and sl_ev.created_at) else None

    # ── 泄漏分解（成本口径：不利 = 正 / 有利 = 负）──
    # a) 入场滑点 = (实际成交 − 计划 entry) × 数量 × 方向符号
    entry_slip_usdt = None
    if actual_entry is not None and plan_entry is not None and qty_total:
        entry_slip_usdt = sign * (actual_entry - plan_entry) * qty_total

    # b) 手续费取绝对值（费用恒为成本）
    fee_usdt = abs(fee_val) if fee_val is not None else None
    # c) 资金费换算成本口径：交易所符号负 = 支付 → 正成本
    fund_usdt = -fund_val if fund_val is not None else None

    # d) SL 穿价 = (触发价 − 成交价) × 数量 × 方向符号（多单成交低于触发 = 正成本）
    sl_breach_usdt = None
    if sl_trigger is not None and sl_fill is not None:
        sl_qty = _pick(sl_detail.get("qty"))
        if sl_qty is None and qty_total is not None and qty_tp1 is not None:
            # 未记数量：TP1 出场后剩余量（TP2 档位按计划分批扣减）
            sl_qty = max(0.0, qty_total - qty_tp1 - (qty_tp2 or 0.0))
        if sl_qty:
            sl_breach_usdt = sign * (sl_trigger - sl_fill) * sl_qty
    else:
        gaps.append("缺失 SL_FILL 明细（触发价/成交价），SL 穿价成本无法量化")

    # e) TP1 滑点：分档盈亏反推实际成交均价（多 entry+pnl/qty / 空 entry−pnl/qty）。
    # 成本口径与 entry_slip/sl_breach 对齐（不利 = 正）：多单 TP1 是卖出，成交价
    # 低于计划 = 少收 = 正成本；空单 TP1 是买回，成交价高于计划 = 多付 = 正成本。
    # 统一公式 sign × (计划 TP1 − 实际成交) × 数量（多单 +1 / 空单 −1 恰好同向）
    implied_fill = None
    tp1_slip_usdt = None
    if tp1_pnl is not None and tp1_qty_actual and plan_tp1 is not None:
        if actual_entry is not None:
            implied_fill = actual_entry + sign * tp1_pnl / tp1_qty_actual
            tp1_slip_usdt = sign * (plan_tp1 - implied_fill) * tp1_qty_actual
        else:
            gaps.append("缺失实际成交价，无法由分档盈亏反推 TP1 成交均价")
    # TP1 实际成交数量 vs 计划分批不符（docs/24 A1 泄漏源之一）
    if tp1_detail.get("qty") is not None and qty_tp1 is not None:
        aq = _f(tp1_detail.get("qty"))
        if aq is not None and abs(aq - qty_tp1) > max(qty_tp1 * _QTY_TOL, 1e-9):
            gaps.append(f"TP1 实际成交数量与计划分批不符（{aq} vs {qty_tp1}）")

    items = {
        "entry_slip": _item(entry_slip_usdt, risk),
        "fee": _item(fee_usdt, risk),
        "funding": _item(fund_usdt, risk),
        "sl_breach": _item(sl_breach_usdt, risk),
        "tp1_slip": _item(tp1_slip_usdt, risk),
    }

    # ── f) 汇总：理论 R（tp1 档位占比 × RR1，余档保本 0）vs 实际 R ──
    rr1 = None
    if plan_tp1 is not None and plan_entry is not None and plan_sl is not None:
        stop_dist = abs(plan_entry - plan_sl)
        if stop_dist > 0:
            rr1 = abs(plan_tp1 - plan_entry) / stop_dist
    theoretical_usdt = None
    if plan_tp1 is not None and plan_entry is not None and qty_tp1 is not None:
        theoretical_usdt = qty_tp1 * abs(plan_tp1 - plan_entry)
    theoretical_r = _r_of(theoretical_usdt, risk)
    if theoretical_r is None and rr1 is not None and qty_total and qty_tp1 is not None:
        theoretical_r = round(qty_tp1 / qty_total * rr1, 4)

    leakage_usdt = None
    if theoretical_usdt is not None and realized is not None:
        leakage_usdt = round(theoretical_usdt - realized, 4)

    covered_vals = [it["usdt"] for it in items.values() if it is not None]
    covered_usdt = round(sum(covered_vals), 4) if covered_vals else None

    unexplained_usdt = None
    if leakage_usdt is not None and covered_usdt is not None:
        unexplained_usdt = round(leakage_usdt - covered_usdt, 4)
        if risk and risk > 0 and abs(unexplained_usdt) / risk > _UNEXPLAINED_R_HINT:
            gaps.append(
                f"未解释泄漏 {unexplained_usdt:+.2f} USDT（{unexplained_usdt / risk:+.2f}R），"
                "上述项之外可能存在止损市价穿价/部分成交/资金费时点等未入账成本"
            )

    return {
        "trade_id": str(rec.id),
        "symbol": rec.symbol,
        "direction": rec.direction,
        "status": rec.status,
        "exit_reason": rec.exit_reason,
        "account_key": rec.account_key,
        "testnet": bool(rec.testnet),
        "plan": {
            "source": "ai_snapshot" if snap else "record",
            "entry": plan_entry,
            "sl": plan_sl,
            "tp1": plan_tp1,
            "tp2": plan_tp2,
            "qty_total": qty_total,
            "qty_tp1": qty_tp1,
            "qty_tp2": qty_tp2,
            "risk_amount": risk,
            "rr1": round(rr1, 4) if rr1 is not None else None,
        },
        "actual": {
            "entry_price": actual_entry,
            "qty": qty_total,
            "slippage_pct": slippage_pct,
            "qty_tp1": tp1_qty_actual,
            "qty_tp2": _pick(raw.get("qty_tp2")),
            "tp1_fill": (
                {
                    "price": round(implied_fill, 8) if implied_fill is not None else None,
                    "qty": _pick(tp1_detail.get("qty")),
                    "pnl": tp1_pnl,
                    "time": tp1_time,
                }
                if (tp1_ev is not None or tp1_pnl is not None)
                else None
            ),
            "sl_fill": (
                {"trigger_price": sl_trigger, "price": sl_fill,
                 "qty": _pick(sl_detail.get("qty")), "time": sl_time}
                if sl_ev is not None
                else None
            ),
            "fee_total": fee_val,
            "funding_total": fund_val,
            "realized_pnl": realized,
        },
        "leakage": {
            # a)~e) 泄漏分解（成本口径：不利 = 正）+ f) 汇总
            "entry_slip": items["entry_slip"],
            "fee": items["fee"],
            "funding": items["funding"],
            "sl_breach": items["sl_breach"],
            "tp1_slip": items["tp1_slip"],
            "summary": {
                "theoretical_usdt": round(theoretical_usdt, 4) if theoretical_usdt is not None else None,
                "theoretical_r": theoretical_r,
                "actual_usdt": round(realized, 4) if realized is not None else None,
                "actual_r": _r_of(realized, risk),
                "leakage_usdt": leakage_usdt,
                "leakage_r": _r_of(leakage_usdt, risk),
                "covered_usdt": covered_usdt,
                "covered_r": _r_of(covered_usdt, risk),
                "unexplained_usdt": unexplained_usdt,
            },
        },
        "gaps": gaps,
    }
