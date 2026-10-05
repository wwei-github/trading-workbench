"""战法 CRUD 接口（2026-10-02）：自建战术剧本的增删改查 + 启停。

战法仅作借鉴参考（AI 判定命中后落库标注，不拦截开单），复盘按战法
分组对比实盘表现——哪个战法真能打，数据说话。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.scan import AIAnalysis
from app.models.tactic import TACTIC_DIRECTIONS, TACTIC_SCOPES, Tactic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tactics", tags=["tactics"])

NAME_MAX = 64
DESC_MAX = 2000


class TacticIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    description: str = Field(min_length=1, max_length=DESC_MAX)
    direction: str = "all"
    scope: str = "both"
    enabled: bool = False

    @field_validator("direction")
    @classmethod
    def _direction_ok(cls, v: str) -> str:
        if v not in TACTIC_DIRECTIONS:
            raise ValueError(f"direction 非法: {v!r}（可选 {TACTIC_DIRECTIONS}）")
        return v

    @field_validator("scope")
    @classmethod
    def _scope_ok(cls, v: str) -> str:
        if v not in TACTIC_SCOPES:
            raise ValueError(f"scope 非法: {v!r}（可选 {TACTIC_SCOPES}）")
        return v


class TacticToggle(BaseModel):
    enabled: bool


def _serialize(t: Tactic, trade_count: int) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "description": t.description,
        "direction": t.direction,
        "scope": t.scope,
        "enabled": t.enabled,
        "trade_count": trade_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("")
def list_tactics(db: Session = Depends(get_db)):
    """战法列表（trade_count=该战法累计被 AI 命中的分析数，含 skip）"""
    rows = db.execute(select(Tactic).order_by(Tactic.created_at.asc())).scalars().all()
    counts = dict(db.execute(
        select(AIAnalysis.tactic_name, func.count(AIAnalysis.id))
        .where(AIAnalysis.tactic_name.isnot(None))
        .group_by(AIAnalysis.tactic_name)
    ).all())
    return [_serialize(t, counts.get(t.name, 0)) for t in rows]


@router.post("")
def create_tactic(payload: TacticIn, db: Session = Depends(get_db)):
    dup = db.execute(
        select(Tactic).where(Tactic.name == payload.name)
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(400, f"战法名已存在: {payload.name}")
    t = Tactic(**payload.model_dump())
    db.add(t)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("战法创建落库失败: %s", payload.name)
        raise
    db.refresh(t)
    return _serialize(t, 0)


@router.put("/{tactic_id}")
def update_tactic(tactic_id: uuid.UUID, payload: TacticIn, db: Session = Depends(get_db)):
    t = db.get(Tactic, tactic_id)
    if not t:
        raise HTTPException(404, "战法不存在")
    dup = db.execute(
        select(Tactic).where(Tactic.name == payload.name, Tactic.id != tactic_id)
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(400, f"战法名已存在: {payload.name}")
    for k, v in payload.model_dump().items():
        setattr(t, k, v)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("战法更新落库失败: %s", tactic_id)
        raise
    db.refresh(t)
    counts = db.execute(
        select(func.count(AIAnalysis.id)).where(AIAnalysis.tactic_name == t.name)
    ).scalar_one()
    return _serialize(t, counts)


@router.patch("/{tactic_id}/enabled")
def toggle_tactic(tactic_id: uuid.UUID, payload: TacticToggle, db: Session = Depends(get_db)):
    """启停开关：停用后不再注入 AI 判定，历史命中标注保留"""
    t = db.get(Tactic, tactic_id)
    if not t:
        raise HTTPException(404, "战法不存在")
    t.enabled = payload.enabled
    db.commit()
    return {"id": str(t.id), "enabled": t.enabled}


@router.delete("/{tactic_id}")
def delete_tactic(tactic_id: uuid.UUID, db: Session = Depends(get_db)):
    """删除：已有分析引用时拒绝硬删（历史归因失锚风险），提示改停用"""
    t = db.get(Tactic, tactic_id)
    if not t:
        raise HTTPException(404, "战法不存在")
    cited = db.execute(
        select(func.count(AIAnalysis.id)).where(AIAnalysis.tactic_name == t.name)
    ).scalar_one()
    if cited > 0:
        raise HTTPException(
            409, f"已有 {cited} 条分析引用该战法，不可删除（请改为停用）")
    db.delete(t)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("战法删除落库失败: %s", tactic_id)
        raise
    return {"deleted": str(tactic_id)}
