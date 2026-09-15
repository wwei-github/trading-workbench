"""交易记录 API 模型（docs/06）"""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TradeEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    detail: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TradeOut(BaseModel):
    id: uuid.UUID
    symbol: str
    direction: str
    recommendation: Optional[float] = None
    entry_price: Optional[float] = None
    qty: Optional[float] = None
    notional: Optional[float] = None
    leverage: int
    margin_used: Optional[float] = None
    risk_amount: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    status: str
    order_type: Optional[str] = None  # 委托方式：market 市价 / limit 限价委托（docs/10）
    expires_at: Optional[datetime] = None  # 限价挂单到期时间（PENDING 时有值）
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    testnet: bool = True
    ai_snapshot: Optional[dict] = None
    ai_analysis_id: Optional[uuid.UUID] = None

    class Config:
        from_attributes = True
