"""运维观测模型

- TaskRecord：任务记录（扫描/AI/交易/复盘等任务级运行历史，替代仅扫描的"历史记录"）
- AppLog：系统日志（应用 WARNING 以上日志落库，前端可查报错）
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TaskRecord(Base):
    """一次任务运行（任务粒度，非币种粒度）"""

    __tablename__ = "task_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # scan / ai / trade / review
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)  # 展示名：定时扫描 / 手动扫描 / 自动交易 / 建议复盘
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="system")  # scheduled / manual / system
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    # running / completed / failed / skipped
    summary: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # 一句话结果：命中 12 个 / 结算 5 笔新开 1 笔
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 任务明细（计数、分布等）
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 失败原因（status=failed 时）
    scan_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # scan 任务关联的扫描记录（前端点击加载该次扫描结果）

    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppLog(Base):
    """应用日志（WARNING 及以上），由 log_sink 从 logging 异步写入"""

    __tablename__ = "app_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # WARNING / ERROR / CRITICAL
    logger_name: Mapped[str] = mapped_column(String(128), nullable=False)  # 来源：app.services.trade_engine 等
    message: Mapped[str] = mapped_column(Text, nullable=False)  # 消息 + 异常堆栈（截断）

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_app_logs_created_level", "created_at", "level"),
    )
