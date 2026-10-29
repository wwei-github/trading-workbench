"""观察期任务模型（docs/23 §3，2026-10-29）

一条「改参数/阈值 → 观察 N 天 → 到期决策」的登记。到期由每日 08:10 的归因
任务扫描（不新增 beat），标 due 后推飞书卡片，前端任务记录页显示「待决策」。

只做提醒与留痕，不自动改配置；判据（decision_rule）在登记时写死。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ObservationTask(Base):
    """一条观察期任务（pending → due → decided / cancelled）"""

    __tablename__ = "observation_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # 展示名：走平门按 ATR 归一（影子）
    config_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 关联配置键/开关，便于回溯
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)  # 到期决策时点（UTC）
    metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 当前读数，可每日刷新
    decision_rule: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 登记时写死的判据
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False, index=True)
    # pending 观察中 / due 已到期待决策 / decided 已决策 / cancelled 取消
    decision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 决策内容（切换/保持/回滚 + 依据）
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_observation_tasks_status_due", "status", "due_at"),
    )
