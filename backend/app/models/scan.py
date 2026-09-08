import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Numeric, Index
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
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="downtrend_breakout", index=True)
    current_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    breakout_pct: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    trend_slope: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    r_squared: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    is_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    scan_record: Mapped[ScanRecord] = relationship(back_populates="results")

    __table_args__ = (
        Index("ix_scan_results_symbol_created", "symbol", "created_at"),
        Index("ix_scan_results_scan_record_id", "scan_record_id"),
    )
