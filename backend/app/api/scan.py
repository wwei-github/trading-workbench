from datetime import datetime, timedelta
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.scan import ScanRecord, ScanResult, AIAnalysis
from app.schemas.scan import (
    ScanListResponse,
    ScanRecordOut,
    ScanResultListResponse,
    ScanResultOut,
    ScanStatusResponse,
    ScanTriggerResponse,
    ScanConfig,
    AIAnalysisOut,
    AIAnalysisListResponse,
    AIAnalysisTriggerRequest,
)
from app.services.binance_client import BinanceClient
from app.tasks.scan_tasks import run_scan_task
from app.tasks.ai_tasks import run_ai_analysis_task

router = APIRouter(prefix="/api/scans", tags=["scans"])


@router.post("", response_model=ScanTriggerResponse)
def trigger_scan(db: Session = Depends(get_db)):
    """手动触发一次扫描"""
    # 检查是否有正在运行的扫描
    running = db.execute(
        select(ScanRecord).where(ScanRecord.status == "running")
    ).scalars().first()
    if running:
        raise HTTPException(status_code=409, detail="已有扫描任务正在运行")

    record = ScanRecord(scan_type="manual", status="running")
    db.add(record)
    db.commit()
    db.refresh(record)

    run_scan_task.delay(str(record.id))
    return ScanTriggerResponse(scan_id=record.id, status="running")


@router.get("", response_model=ScanListResponse)
def list_scans(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    total = db.execute(select(func.count()).select_from(ScanRecord)).scalar_one()
    items = (
        db.execute(
            select(ScanRecord)
            .order_by(desc(ScanRecord.started_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return ScanListResponse(
        items=[ScanRecordOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/latest/results", response_model=ScanResultListResponse)
def latest_scan_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("breakout_pct"),
    order: str = Query("desc"),
    db: Session = Depends(get_db),
):
    latest = db.execute(
        select(ScanRecord)
        .where(ScanRecord.status == "completed")
        .order_by(desc(ScanRecord.finished_at))
        .limit(1)
    ).scalars().first()
    if not latest:
        return ScanResultListResponse(items=[], total=0)
    return _get_results(latest.id, page, page_size, sort_by, order, db)


@router.get("/{scan_id}/results", response_model=ScanResultListResponse)
def scan_results(
    scan_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("breakout_pct"),
    order: str = Query("desc"),
    db: Session = Depends(get_db),
):
    record = db.get(ScanRecord, scan_id)
    if not record:
        raise HTTPException(status_code=404, detail="扫描记录不存在")
    return _get_results(scan_id, page, page_size, sort_by, order, db)


@router.get("/status", response_model=ScanStatusResponse)
def scan_status(db: Session = Depends(get_db)):
    last_scan = db.execute(
        select(ScanRecord).order_by(desc(ScanRecord.started_at)).limit(1)
    ).scalars().first()
    is_scanning = (
        db.execute(
            select(func.count())
            .select_from(ScanRecord)
            .where(ScanRecord.status == "running")
        ).scalar_one()
        > 0
    )
    config = ScanConfig(
        interval_hours=settings.SCAN_INTERVAL_HOURS,
        kline_interval=settings.KLINE_INTERVAL,
        window=settings.KLINE_WINDOW,
        breakout_threshold=settings.BREAKOUT_THRESHOLD,
        r_squared_threshold=settings.R_SQUARED_THRESHOLD,
        repeat_window_hours=settings.REPEAT_WINDOW_HOURS,
    )
    return ScanStatusResponse(
        last_scan=ScanRecordOut.model_validate(last_scan) if last_scan else None,
        is_scanning=is_scanning,
        config=config,
    )


def _get_results(scan_id: UUID, page: int, page_size: int, sort_by: str, order: str, db: Session):
    allowed_sort = {"breakout_pct", "created_at", "symbol", "r_squared", "volume_24h", "volume", "volume_type"}
    if sort_by not in allowed_sort:
        sort_by = "breakout_pct"
    column = getattr(ScanResult, sort_by)
    order_col = desc(column) if order == "desc" else column

    total = db.execute(
        select(func.count()).select_from(ScanResult).where(ScanResult.scan_record_id == scan_id)
    ).scalar_one()
    items = (
        db.execute(
            select(ScanResult)
            .where(ScanResult.scan_record_id == scan_id)
            .order_by(order_col)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return ScanResultListResponse(
        items=[ScanResultOut.model_validate(i) for i in items],
        total=total,
    )


# ===== AI 分析端点 =====

@router.get("/{scan_id}/ai-analyses", response_model=AIAnalysisListResponse)
def list_ai_analyses(scan_id: UUID, db: Session = Depends(get_db)):
    """列出指定扫描记录的所有 AI 分析结果"""
    items = db.execute(
        select(AIAnalysis).where(
            AIAnalysis.scan_result_id.in_(
                select(ScanResult.id).where(ScanResult.scan_record_id == scan_id)
            )
        )
    ).scalars().all()
    return AIAnalysisListResponse(
        items=[AIAnalysisOut.model_validate(i) for i in items],
        total=len(items),
    )


@router.post("/{scan_id}/ai-analyses", response_model=ScanTriggerResponse)
def trigger_ai_analysis(
    scan_id: UUID,
    body: AIAnalysisTriggerRequest,
    db: Session = Depends(get_db),
):
    """触发 AI 分析（全量或单币）"""
    if not settings.AI_ENABLED or not settings.AI_API_KEY:
        raise HTTPException(status_code=403, detail="AI 分析未启用")

    record = db.get(ScanRecord, scan_id)
    if not record:
        raise HTTPException(status_code=404, detail="扫描记录不存在")

    run_ai_analysis_task.delay(
        str(scan_id),
        str(body.scan_result_id) if body.scan_result_id else None,
    )
    return ScanTriggerResponse(scan_id=scan_id, status="analyzing")


# ===== K 线数据 =====

@router.get("/klines/{symbol}")
def get_klines(symbol: str, limit: int = Query(100, ge=1, le=500)):
    """获取指定币种的最新 K 线数据（用于前端展示）"""
    client = BinanceClient()
    try:
        klines = client.get_klines(symbol, settings.KLINE_INTERVAL, limit)
        # 返回精简格式: [{time, open, high, low, close, volume}, ...]
        result = []
        for k in klines:
            result.append({
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return {"symbol": symbol, "interval": settings.KLINE_INTERVAL, "klines": result}
    finally:
        client.close()
