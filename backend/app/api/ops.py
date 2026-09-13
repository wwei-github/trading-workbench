"""运维观测接口：任务记录（/api/tasks）与系统日志（/api/logs）"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ops import AppLog, TaskRecord
from app.schemas.ops import AppLogListResponse, AppLogOut, TaskListResponse, TaskRecordOut

router = APIRouter(prefix="/api", tags=["ops"])


@router.get("/tasks", response_model=TaskListResponse)
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    task_type: Optional[str] = Query(None, description="scan/ai/trade/review"),
    status: Optional[str] = Query(None, description="running/completed/failed/skipped"),
    db: Session = Depends(get_db),
):
    stmt = select(TaskRecord)
    if task_type:
        stmt = stmt.where(TaskRecord.task_type == task_type)
    if status:
        stmt = stmt.where(TaskRecord.status == status)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = (
        db.execute(
            stmt.order_by(desc(TaskRecord.started_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return TaskListResponse(
        items=[TaskRecordOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/tasks/{task_id}", response_model=TaskRecordOut)
def get_task(task_id: UUID, db: Session = Depends(get_db)):
    return db.get(TaskRecord, task_id)


@router.get("/logs", response_model=AppLogListResponse)
def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    level: Optional[str] = Query(None, description="WARNING/ERROR/CRITICAL"),
    q: Optional[str] = Query(None, max_length=100, description="消息关键字"),
    db: Session = Depends(get_db),
):
    stmt = select(AppLog)
    if level:
        stmt = stmt.where(AppLog.level == level)
    if q:
        stmt = stmt.where(AppLog.message.ilike(f"%{q}%"))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = (
        db.execute(
            stmt.order_by(desc(AppLog.id))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return AppLogListResponse(
        items=[AppLogOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )
