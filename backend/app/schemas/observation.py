"""观察期任务接口 Schema（docs/23 §3）"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ObservationOut(BaseModel):
    id: UUID
    name: str
    config_key: Optional[str] = None
    started_at: datetime
    due_at: datetime
    metrics: Optional[dict] = None
    decision_rule: Optional[str] = None
    status: str
    decision: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ObservationListOut(BaseModel):
    items: list[ObservationOut]
    counts: dict[str, int]  # pending / due / decided / cancelled


class ObservationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    days: int = Field(default=7, ge=1, le=90, description="观察期天数；与 due_at 二选一")
    due_at: Optional[datetime] = Field(default=None, description="显式到期时间（UTC），优先于 days")
    config_key: Optional[str] = Field(default=None, max_length=64)
    decision_rule: Optional[str] = None
    metrics: Optional[dict] = None
    note: Optional[str] = None


class ObservationDecide(BaseModel):
    decision: str = Field(min_length=2, description="决策结论：切换 / 保持影子 / 回滚 + 依据")
    note: Optional[str] = None


class ObservationUpdate(BaseModel):
    metrics: Optional[dict] = None
    note: Optional[str] = None
    due_at: Optional[datetime] = None
    status: Optional[str] = Field(default=None, pattern="^(pending|due|cancelled)$")
