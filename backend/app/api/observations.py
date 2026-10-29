"""观察期任务接口（docs/23 §3）：登记 / 列表 / 决策 / 更新读数

前端「任务记录」页顶部「待决策」区块消费本接口；到期推送由每日归因任务负责。
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.observation import ObservationTask
from app.schemas.observation import (
    ObservationCreate,
    ObservationDecide,
    ObservationListOut,
    ObservationOut,
    ObservationUpdate,
)
from app.services import observations as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/observations", tags=["observations"])


@router.get("", response_model=ObservationListOut)
def list_observations(
    status: Optional[str] = Query(None, description="逗号分隔：pending,due,decided,cancelled；缺省=未决策(pending+due)"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    wanted = [s.strip() for s in status.split(",") if s.strip()] if status else \
        [svc.STATUS_PENDING, svc.STATUS_DUE]
    stmt = select(ObservationTask).where(ObservationTask.status.in_(wanted))
    # 已到期（due）置顶，其余按到期日升序
    order = case((ObservationTask.status == svc.STATUS_DUE, 0), else_=1)
    items = db.execute(
        stmt.order_by(order, ObservationTask.due_at.asc()).limit(limit)
    ).scalars().all()
    return ObservationListOut(
        items=[ObservationOut.model_validate(t) for t in items],
        counts=svc.counts(db),
    )


@router.post("", response_model=ObservationOut, status_code=201)
def create_observation(body: ObservationCreate, db: Session = Depends(get_db)):
    task = svc.create(
        db, name=body.name, days=body.days, config_key=body.config_key,
        decision_rule=body.decision_rule, metrics=body.metrics,
        note=body.note, due_at=body.due_at,
    )
    return ObservationOut.model_validate(task)


def _get_task(db: Session, task_id: str) -> ObservationTask:
    try:
        key = UUID(task_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="观察任务不存在") from None
    task = db.get(ObservationTask, key)
    if task is None:
        raise HTTPException(status_code=404, detail="观察任务不存在")
    return task


@router.post("/{task_id}/decide", response_model=ObservationOut)
def decide_observation(task_id: str, body: ObservationDecide,
                       db: Session = Depends(get_db)):
    task = _get_task(db, task_id)
    if task.status == svc.STATUS_CANCELLED:
        raise HTTPException(status_code=409, detail="任务已取消，不能决策")
    return ObservationOut.model_validate(svc.decide(db, task, body.decision, body.note))


@router.patch("/{task_id}", response_model=ObservationOut)
def update_observation(task_id: str, body: ObservationUpdate,
                       db: Session = Depends(get_db)):
    task = _get_task(db, task_id)
    if body.metrics is not None:
        task.metrics = body.metrics
    if body.note is not None:
        task.note = body.note
    if body.due_at is not None:
        task.due_at = body.due_at
    if body.status is not None:
        task.status = body.status
        if body.status != svc.STATUS_DECIDED:
            task.decided_at = None
    db.commit()
    db.refresh(task)
    return ObservationOut.model_validate(task)
