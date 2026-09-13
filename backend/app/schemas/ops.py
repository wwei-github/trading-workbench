"""运维观测接口 Schema（任务记录 / 系统日志）"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class TaskRecordOut(BaseModel):
    id: UUID
    task_type: str
    task_name: str
    trigger: str
    status: str
    summary: Optional[str] = None
    detail: Optional[dict] = None
    error: Optional[str] = None
    scan_record_id: Optional[UUID] = None
    started_at: datetime
    finished_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    items: list[TaskRecordOut]
    total: int
    page: int
    page_size: int


class AppLogOut(BaseModel):
    id: int
    level: str
    logger_name: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True


class AppLogListResponse(BaseModel):
    items: list[AppLogOut]
    total: int
    page: int
    page_size: int
