"""虚拟账本 API（docs/16 P-07，只读统计 + 手动回填触发）。

口径（P-04 对齐）：
- decided = status=DONE（有 outcome/r）；OPEN/EXPIRED 不进任何百分比分母，单列计数；
- win_rate = (win_tp1+win_tp2)/decided；exp_r = decided 净 R 均值（含费）；
- 分桶维度：signal_type / direction / quality 分位（<60/60-69/70-79/80+，与
  CARD-09 门槛分析同切法）/ rule_version（跨版本不混算）。
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ledger import VirtualLedger

logger = logging.getLogger("app.ledger")

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

_WIN = ("win_tp1", "win_tp2")


def _cutoff(days: int) -> datetime:
    return datetime.utcnow() - timedelta(days=max(1, days))


def _bucket(rows: list, keyfn) -> list:
    """decided 行分桶统计（Python 聚合——月增千行级无需 SQL group）。"""
    groups: dict = {}
    for r in rows:
        groups.setdefault(keyfn(r), []).append(r)
    out = []
    for k, rs in groups.items():
        wins = sum(1 for r in rs if r.outcome in _WIN)
        sum_r = sum(float(r.r or 0) for r in rs)
        out.append({
            "key": k, "n": len(rs), "wins": wins,
            "win_rate": round(wins / len(rs), 4),
            "exp_r": round(sum_r / len(rs), 4),
            "sum_r": round(sum_r, 2),
        })
    out.sort(key=lambda x: -x["n"])
    return out


def _qbucket(score) -> str:
    if score is None:
        return "未知"
    if score < 60:
        return "<60"
    if score < 70:
        return "60-69"
    if score < 80:
        return "70-79"
    return "80+"


@router.get("/stats")
def ledger_stats(
    days: int = Query(30, ge=1, le=365),
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(VirtualLedger).filter(VirtualLedger.entry_bar_open >= _cutoff(days))
    if source:
        q = q.filter(VirtualLedger.source == source)
    rows = q.all()
    decided = [r for r in rows if r.status == "DONE"]
    wins = [r for r in decided if r.outcome in _WIN]
    losses = [r for r in decided if r.outcome == "loss"]
    sum_r = sum(float(r.r or 0) for r in decided)
    overall = {
        "decided": len(decided),
        "wins": len(wins),
        "losses": len(losses),
        "be_exits": sum(1 for r in decided if r.outcome == "be_exit"),
        "win_rate": round(len(wins) / len(decided), 4) if decided else None,
        "exp_r": round(sum_r / len(decided), 4) if decided else None,
        "sum_r": round(sum_r, 2),
        "open": sum(1 for r in rows if r.status == "OPEN"),
        "expired": sum(1 for r in rows if r.status == "EXPIRED"),
    }
    return {
        "overall": overall,
        "by_signal_type": _bucket(decided, lambda r: r.signal_type),
        "by_direction": _bucket(decided, lambda r: r.direction),
        "by_quality": _bucket(decided, lambda r: _qbucket(r.quality_score)),
        "by_rule_version": _bucket(decided, lambda r: r.rule_version or "legacy"),
    }


def _serialize(r: VirtualLedger) -> dict:
    return {
        "id": str(r.id),
        "scan_result_id": str(r.scan_result_id) if r.scan_result_id else None,
        "symbol": r.symbol,
        "direction": r.direction,
        "source": r.source,
        "signal_type": r.signal_type,
        "touch_ref": r.touch_ref,
        "pattern": r.pattern,
        "position": r.position,
        "ema_state": r.ema_state,
        "quality_score": r.quality_score,
        "votes_net": (r.votes_json or {}).get("net") if r.votes_json else None,
        "entry": float(r.entry_price),
        "stop": float(r.stop_price),
        "tp1": float(r.tp1_price),
        "tp2": float(r.tp2_price) if r.tp2_price else None,
        "r_unit": float(r.r_unit),
        "entry_bar_open": r.entry_bar_open.isoformat() if r.entry_bar_open else None,
        "status": r.status,
        "outcome": r.outcome,
        "r": float(r.r) if r.r is not None else None,
        "bars": r.bars,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "ai_decision": r.ai_decision,
        "ai_recommendation": float(r.ai_recommendation) if r.ai_recommendation is not None else None,
        "rule_version": r.rule_version,
    }


@router.get("/entries")
def ledger_entries(
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    status: Optional[str] = None,
    outcome: Optional[str] = None,
    source: Optional[str] = None,
    signal_type: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(VirtualLedger).filter(VirtualLedger.entry_bar_open >= _cutoff(days))
    if symbol:
        q = q.filter(VirtualLedger.symbol == symbol.upper())
    if direction:
        q = q.filter(VirtualLedger.direction == direction)
    if status:
        q = q.filter(VirtualLedger.status == status)
    if outcome:
        q = q.filter(VirtualLedger.outcome == outcome)
    if source:
        q = q.filter(VirtualLedger.source == source)
    if signal_type:
        q = q.filter(VirtualLedger.signal_type == signal_type)
    total = q.count()
    rows = (
        q.order_by(VirtualLedger.entry_bar_open.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"items": [_serialize(r) for r in rows],
            "total": total, "page": page, "page_size": page_size}


class LedgerBackfillIn(BaseModel):
    days: int = Field(30, ge=1, le=90)
    symbols: Optional[List[str]] = None


@router.post("/backfill")
def ledger_backfill(body: LedgerBackfillIn, db: Session = Depends(get_db)):
    """手动触发源B回填（可重入，唯一键去重）；2h 内已有同名运行中任务则 409。"""
    from app.models.ops import TaskRecord

    recent = datetime.utcnow() - timedelta(hours=2)
    running = (
        db.query(TaskRecord)
        .filter(TaskRecord.task_type == "ledger_backfill",
                TaskRecord.status == "running",
                TaskRecord.created_at >= recent)
        .count()
    )
    if running:
        raise HTTPException(status_code=409, detail="已有回填任务在运行（2h 内），请稍后再试")
    from app.tasks.ledger_tasks import run_ledger_backfill_task
    run_ledger_backfill_task.delay(body.days, body.symbols)
    return {"ok": True, "days": body.days,
            "symbols": body.symbols or "(全部，volume 前缀)"}
