"""实盘归因统计（复盘重设计 2026-09-18）

复盘主轴从「模拟回放 AI 建议」转向「实盘交易 + 计划对照」：
- 总览：胜率/盈亏比/期望值/持仓时长/费用拆分
- 计划 vs 实际：计划风险（risk_amount 合计）对照实际盈亏，偏差归因到费用
- 分组：开单类型 / 方向 / 出场原因 / 错误标签 / 开仓时段（北京时间 4h 桶）
- 明细：逐笔带 AI 归因结论，联动交易复盘K线

存量平仓单的 fee/funding 在结算字段上线前未落库，此处惰性回补
（income 明细按 symbol+opened_at 从交易所拉取，回写后一次性生效）。
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trade import TradeRecord
from app.services.binance_trader import BinanceTrader

logger = logging.getLogger(__name__)

# 开仓时段桶（北京时间，4h 一档）：交易记录 opened_at 为 UTC
HOUR_BUCKETS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]

# 结算费用回补单轮上限（防一次性拉爆交易所限额；未补完的下轮统计继续）
_BACKFILL_LIMIT = 20


def backfill_costs(db: Session, trader: BinanceTrader, limit: int = _BACKFILL_LIMIT) -> int:
    """为 fee_total 为空的已平仓交易回补手续费/资金费（一次性，回写后不再查）。"""
    rows = db.execute(
        select(TradeRecord)
        .where(
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_reason.isnot(None),
            TradeRecord.fee_total.is_(None),
        )
        .order_by(TradeRecord.closed_at.desc())
        .limit(limit)
    ).scalars().all()
    n = 0
    for rec in rows:
        try:
            since_ms = int(
                rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            ) - 5_000 if rec.opened_at else 0
            income = trader.income_rows(rec.symbol, since_ms)
            rec.fee_total = round(sum(
                float(r.get("income") or 0) for r in income
                if r.get("incomeType") == "COMMISSION"), 6)
            rec.funding_total = round(sum(
                float(r.get("income") or 0) for r in income
                if r.get("incomeType") == "FUNDING_FEE"), 6)
            db.commit()
            n += 1
        except Exception as e:
            logger.warning("费用回补失败 %s: %s（按 0 处理，避免反复重试）", rec.symbol, e)
            rec.fee_total, rec.funding_total = 0.0, 0.0
            db.commit()
            n += 1
    return n


def _trade_type_of(rec: TradeRecord) -> str:
    return (rec.ai_snapshot or {}).get("trade_type") or "unknown"


def _hold_hours(rec: TradeRecord) -> float:
    if not rec.opened_at or not rec.closed_at:
        return 0.0
    return round((rec.closed_at - rec.opened_at).total_seconds() / 3600, 1)


def _bucket_label(rec: TradeRecord) -> str:
    """开仓时段（北京时间 4h 桶）"""
    if not rec.opened_at:
        return "未知"
    cst = rec.opened_at + timedelta(hours=8)
    for lo, hi in HOUR_BUCKETS:
        if lo <= cst.hour < hi:
            return f"北京 {lo:02d}-{hi:02d} 时"
    return "未知"


def _agg(trades: list) -> dict:
    wins = [t for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t for t in trades if (t.realized_pnl or 0) < 0]
    pnl = round(sum(t.realized_pnl or 0 for t in trades), 2)
    avg_win = round(sum(t.realized_pnl for t in wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(t.realized_pnl for t in losses) / len(losses), 2) if losses else 0.0
    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "pnl": pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": round(abs(avg_win / avg_loss), 2) if avg_loss else None,  # 盈亏比
        "ev_per_trade": round(pnl / len(trades), 2) if trades else 0.0,  # 期望值/笔 USDT
    }


def build_actual_review(db: Session, days: int) -> dict:
    """实盘归因统计主入口（API 层直接返回 dict）"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    trades = db.execute(
        select(TradeRecord)
        .where(
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_reason.isnot(None),
            TradeRecord.closed_at >= cutoff,
        )
        .order_by(TradeRecord.closed_at.asc())
    ).scalars().all()

    overview = _agg(trades)
    total_fee = round(sum(t.fee_total or 0 for t in trades), 2)
    total_funding = round(sum(t.funding_total or 0 for t in trades), 2)
    planned_risk = round(sum(t.risk_amount or 0 for t in trades), 2)

    overview.update({
        "days": days,
        "total_fee": total_fee,
        "total_funding": total_funding,
        "planned_risk_total": planned_risk,
        # 计划 vs 实际：净亏（计划内应亏 planned_risk，实际多亏部分主要是费用）
        "plan_gap": round((min(overview["pnl"], 0) + planned_risk), 2),  # ≥0 表示亏超计划
        "avg_hold_hours": round(
            sum(_hold_hours(t) for t in trades) / len(trades), 1) if trades else 0.0,
        "attributed": sum(1 for t in trades if t.attributed_at),  # 已归因笔数
    })

    # 分组：维度 × 桶
    groups: list[dict] = []

    def push(dim: str, key: str, bucket: list):
        if not bucket:
            return
        groups.append({"dim": dim, "key": key, **_agg(bucket)})

    for dim, keyfn in (
        ("开单类型", _trade_type_of),
        ("方向", lambda t: t.direction or "未知"),
        ("出场原因", lambda t: t.exit_reason or "未知"),
        ("开仓时段", _bucket_label),
    ):
        buckets: dict[str, list] = {}
        for t in trades:
            buckets.setdefault(keyfn(t), []).append(t)
        for key, bucket in sorted(buckets.items()):
            push(dim, key, bucket)

    # 错误标签维度：一笔多标签拆行（仅统计已归因的）
    tag_buckets: dict[str, list] = {}
    for t in trades:
        for tag in (t.error_tags or []):
            tag_buckets.setdefault(str(tag), []).append(t)
    for key, bucket in sorted(tag_buckets.items()):
        push("错误标签", key, bucket)

    items = [{
        "id": str(t.id),
        "symbol": t.symbol,
        "direction": t.direction,
        "trade_type": _trade_type_of(t),
        "exit_reason": t.exit_reason,
        "entry_plan": (t.ai_snapshot or {}).get("entry_price"),
        "sl_plan": (t.ai_snapshot or {}).get("stop_loss"),
        "tp1_plan": (t.ai_snapshot or {}).get("take_profit_1"),
        "entry_actual": float(t.entry_price) if t.entry_price else None,
        "realized_pnl": float(t.realized_pnl) if t.realized_pnl is not None else None,
        "pnl_pct": float(t.pnl_pct) if t.pnl_pct is not None else None,
        "risk_amount": float(t.risk_amount) if t.risk_amount is not None else None,
        "fee_total": float(t.fee_total) if t.fee_total is not None else None,
        "funding_total": float(t.funding_total) if t.funding_total is not None else None,
        "recommendation": (t.ai_snapshot or {}).get("recommendation"),
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "hold_hours": _hold_hours(t),
        "error_tags": t.error_tags or [],
        "error_lesson": t.error_lesson,
        "attributed": bool(t.attributed_at),
    } for t in trades]

    return {"overview": overview, "groups": groups, "items": items}
