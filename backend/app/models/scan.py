import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Numeric, Index, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ScanRecord(Base):
    __tablename__ = "scan_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_type: Mapped[str] = mapped_column(String(20), default="manual")  # scheduled / manual
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / completed / failed
    coin_count: Mapped[int] = mapped_column(Integer, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    results: Mapped[list["ScanResult"]] = relationship(back_populates="scan_record", cascade="all, delete-orphan")


class ScanResult(Base):
    __tablename__ = "scan_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_records.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    current_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    breakout_pct: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    trend_slope: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    r_squared: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    pattern: Mapped[str] = mapped_column(String(32), nullable=True)
    signal_reason: Mapped[str] = mapped_column(String(128), nullable=True)
    ema_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)  # 均线形态状态
    position: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 12金K出现的位置
    key_levels: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # 命中的关键位明细
    volume_24h: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False, default=0)
    volume: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    volume_type: Mapped[str] = mapped_column(String(16), nullable=False, default="平量", index=True)
    is_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    scan_record: Mapped[ScanRecord] = relationship(back_populates="results")
    ai_analysis: Mapped[Optional["AIAnalysis"]] = relationship(back_populates="scan_result", uselist=False)

    __table_args__ = (
        Index("ix_scan_results_symbol_created", "symbol", "created_at"),
        Index("ix_scan_results_scan_record_id", "scan_record_id"),
    )


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_results.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trade_decision: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)  # suggest / skip
    skip_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # long / short
    analysis: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit_1: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit_2: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    risk_reward_ratio: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    position_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    recommendation: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)  # 推荐程度 0-100
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    scan_result: Mapped["ScanResult"] = relationship(back_populates="ai_analysis")


class KlineCache(Base):
    """K 线小时级缓存表

    同一小时内相同 symbol+interval 多次请求直接命中缓存；
    跨小时自动拉取新数据覆盖。
    """
    __tablename__ = "kline_caches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1h")
    kline_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    klines: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "kline_hour", name="uq_kline_cache_key"),
    )


class Watchlist(Base):
    """关注列表：用户手动关注的币种，持久保存"""

    __tablename__ = "watchlist"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
