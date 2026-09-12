"""交易记录 API（docs/06）：列表 + 操作历史"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.trade import TradeEvent, TradeRecord
from app.schemas.trade import TradeEventOut, TradeOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("", response_model=list[TradeOut])
def list_trades(
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """自动交易记录（新→旧）。status 可选过滤：OPENED/TP1_HIT/TP2_HIT/CLOSED/FAILED"""
    q = select(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(TradeRecord.status == status.upper())
    rows = db.execute(q).scalars().all()
    return [TradeOut.model_validate(r) for r in rows]


@router.get("/{trade_id}/events", response_model=list[TradeEventOut])
def trade_events(trade_id: str, db: Session = Depends(get_db)):
    """一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    rows = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()
    return [TradeEventOut.model_validate(e) for e in rows]
