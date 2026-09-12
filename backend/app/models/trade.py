"""自动交易数据模型（docs/06）

- TradeRecord：一笔自动交易的全生命周期（开仓信息/三价现值/状态/收益结算）
- TradeEvent：操作历史（每步迁移/撤单/改价/成交/异常各一行）
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, Numeric, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class TradeRecord(Base):
    """一笔自动交易。ai_analysis_id 唯一：同一 AI 结论最多开一次仓（DB 级防重）"""

    __tablename__ = "trade_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short

    # 溯源（scan_result 可空：防误删历史行导致交易记录断链）
    scan_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_results.id"), nullable=True
    )
    ai_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_analyses.id"), nullable=False, unique=True, index=True
    )
    recommendation: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)  # 开单时评分快照
    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 测试网/正式网标识
    ai_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 开单时 AI 分析结论快照（字段同前端 AIAnalysisCard 所需，交易记录自持不随 ai_analyses 断链）

    # 开仓
    entry_price: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)  # 实际成交均价
    qty: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)  # 开仓数量（基准量）
    notional: Mapped[Optional[float]] = mapped_column(Numeric(20, 2), nullable=True)  # 名义仓位 USDT
    leverage: Mapped[int] = mapped_column(Integer, default=20)
    margin_mode: Mapped[str] = mapped_column(String(12), default="isolated")
    margin_used: Mapped[Optional[float]] = mapped_column(Numeric(20, 4), nullable=True)
    risk_amount: Mapped[Optional[float]] = mapped_column(Numeric(20, 4), nullable=True)  # 止损金额（USDT）

    # 三价（现值；SL 跟进移动后更新，历史在 trade_events）
    stop_loss: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    tp1: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    tp2: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)

    # 状态与结算
    status: Mapped[str] = mapped_column(String(16), default="OPENED", index=True)
    # OPENED（运行中）/ TP1_HIT（部分止盈，止损已移至成本价保本）/ TP2_HIT（仅剩跟进仓）/ CLOSED / FAILED
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 正/负值 USDT
    pnl_pct: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)  # 相对止损金额 %
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # sl / tp1_then_sl / tp1_trail / trail_sl / breakeven_sl / manual / error

    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # {entry_order_id, sl_order_id, tp1_order_id, tp2_order_id, qty_tp1, qty_tp2, capital_base, wallet}

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    events: Mapped[list["TradeEvent"]] = relationship(
        back_populates="trade_record", cascade="all, delete-orphan", order_by="TradeEvent.created_at"
    )


class TradeEvent(Base):
    """交易操作历史事件"""

    __tablename__ = "trade_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_records.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # OPEN / TP1_FILL / TP2_FILL / SL_MOVE / SL_FILL / CANCEL / ERROR / SETTLE / SKIP
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    trade_record: Mapped[TradeRecord] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_trade_events_record_created", "trade_record_id", "created_at"),
    )
