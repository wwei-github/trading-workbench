"""影子模式评估报告接口（docs/14 §六，2026-10-04）：只读，无写路径。

前端「影子实验」面板数据源；实时调用 build_report 与每日评估任务同口径。
评估只统计不决策——升格必须走 docs/14 §八 人工流程，本接口不提供任何开关。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.shadow import build_report

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


@router.get("/report")
def get_shadow_report(
    window_days: int = Query(30, ge=1, le=180),
    db: Session = Depends(get_db),
):
    """影子特征分桶评估报告（基线 + 分桶胜率，n<30 的桶标 insufficient）。"""
    return build_report(db, window_days=window_days)
