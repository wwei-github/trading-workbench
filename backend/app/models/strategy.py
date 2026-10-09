"""多策略可插拔架构（docs/20）数据模型。

StrategyShadowSignal：S1 影子期策略信号独立落库（只记录不开单）——
ema_trend 等非内置策略产出的信号在此积累样本，用于 docs/20 §4.2 的
重叠率/相关性判定与后续 S2 开单权限评审。golden_k_ai 内置管线的信号
不落此表（走 scan_results/ai_analyses 原路径，零行为变化）。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Numeric, DateTime, JSON, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StrategyShadowSignal(Base):
    """策略影子信号（S1，docs/20 §八）：策略插件产出、未接执行层的信号记录。

    与 TradeRecord（实盘单）严格隔离：本表任何行都不会触发开单——
    S2 只在影子期数据评审通过后，才让指定 strategy_id 的信号进执行层。
    """

    __tablename__ = "strategy_shadow_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short
    entry: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    stop: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    tp1: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    tp2: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    signal_type: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)  # pullback / cross
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 策略私有证据快照
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_shadow_sig_strategy_created", "strategy_id", "created_at"),
    )
