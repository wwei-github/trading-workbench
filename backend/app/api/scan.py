from datetime import datetime, timedelta
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, desc, select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
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
    ManualAnalyzeRequest,
    ManualAnalysisOut,
    SystemConfigOut,
    SystemConfigUpdate,
)
from app.services.binance_client import BinanceClient
from app.services.scanner import classify_volume
from app.services.ai_analyzer import analyze_coin
from app.api.watchlist import normalize_symbol
from app.tasks.scan_tasks import run_scan_task
from app.tasks.ai_tasks import run_ai_analysis_task
from app.models.scan import ScanRecord, ScanResult, AIAnalysis
from app.models.system_config import SystemConfig

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
    # 仅保留最近 24 小时内的扫描记录
    cutoff = datetime.utcnow() - timedelta(hours=24)
    total = db.execute(
        select(func.count())
        .select_from(ScanRecord)
        .where(ScanRecord.started_at >= cutoff)
    ).scalar_one()
    items = (
        db.execute(
            select(ScanRecord)
            .where(ScanRecord.started_at >= cutoff)
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
    # 从数据库读取策略配置
    cfg = _get_system_config(db)
    config = ScanConfig(
        interval_hours=settings.SCAN_INTERVAL_HOURS,
        kline_interval=cfg.kline_interval,
        window=cfg.kline_window,
        breakout_threshold=float(cfg.breakout_threshold),
        r_squared_threshold=float(cfg.r_squared_threshold),
        repeat_window_hours=cfg.repeat_window_hours,
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


def _get_system_config(db: Session) -> SystemConfig:
    """获取系统配置（保证存在 id=1 的行）"""
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        cfg = SystemConfig(id=1, ai_analysis_enabled=False)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


@router.post("/{scan_id}/ai-analyses", response_model=ScanTriggerResponse)
def trigger_ai_analysis(
    scan_id: UUID,
    body: AIAnalysisTriggerRequest,
    db: Session = Depends(get_db),
):
    """触发 AI 分析（全量或单币）"""
    cfg = _get_system_config(db)
    # 检查：数据库开关打开 + 后端已配置 API_KEY
    if not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
        raise HTTPException(status_code=403, detail="AI 分析未启用或未配置 API Key")

    record = db.get(ScanRecord, scan_id)
    if not record:
        raise HTTPException(status_code=404, detail="扫描记录不存在")

    # 单币重新分析：先删除旧结果，前端轮询以"新记录出现"为完成标志
    if body.scan_result_id:
        db.execute(
            delete(AIAnalysis).where(
                AIAnalysis.scan_result_id == body.scan_result_id
            )
        )
        db.commit()

    run_ai_analysis_task.delay(
        str(scan_id),
        str(body.scan_result_id) if body.scan_result_id else None,
    )
    return ScanTriggerResponse(scan_id=scan_id, status="analyzing")


# ===== 系统配置（AI 开关 + 扫描策略） =====

@router.get("/config", response_model=SystemConfigOut)
def get_system_config(db: Session = Depends(get_db)):
    """获取系统配置（AI 开关 + 策略提示词 + 扫描策略）"""
    cfg = _get_system_config(db)
    return SystemConfigOut(
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        ai_configured=bool(settings.AI_API_KEY),
        strategy_prompt_enabled=cfg.strategy_prompt_enabled,
        strategy_prompt=cfg.strategy_prompt or "",
        kline_interval=cfg.kline_interval,
        kline_window=cfg.kline_window,
        breakout_threshold=float(cfg.breakout_threshold),
        r_squared_threshold=float(cfg.r_squared_threshold),
        repeat_window_hours=cfg.repeat_window_hours,
        swing_order=cfg.swing_order,
        pullback_tolerance=float(cfg.pullback_tolerance),
    )


@router.put("/config", response_model=SystemConfigOut)
def update_system_config(
    body: SystemConfigUpdate,
    db: Session = Depends(get_db),
):
    """更新系统配置（AI 开关 + 扫描策略）"""
    cfg = _get_system_config(db)

    # AI 开关
    if body.ai_analysis_enabled is not None:
        if body.ai_analysis_enabled and not settings.AI_API_KEY:
            raise HTTPException(
                status_code=400,
                detail="后端未配置 AI_API_KEY，无法开启 AI 分析",
            )
        cfg.ai_analysis_enabled = body.ai_analysis_enabled

    # 策略提示词
    if body.strategy_prompt_enabled is not None:
        cfg.strategy_prompt_enabled = body.strategy_prompt_enabled
    if body.strategy_prompt is not None:
        cfg.strategy_prompt = body.strategy_prompt

    # 扫描策略字段
    if body.kline_interval is not None:
        cfg.kline_interval = body.kline_interval
    if body.kline_window is not None:
        cfg.kline_window = body.kline_window
    if body.breakout_threshold is not None:
        cfg.breakout_threshold = body.breakout_threshold
    if body.r_squared_threshold is not None:
        cfg.r_squared_threshold = body.r_squared_threshold
    if body.repeat_window_hours is not None:
        cfg.repeat_window_hours = body.repeat_window_hours
    if body.swing_order is not None:
        cfg.swing_order = body.swing_order
    if body.pullback_tolerance is not None:
        cfg.pullback_tolerance = body.pullback_tolerance

    db.commit()
    db.refresh(cfg)
    return SystemConfigOut(
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        ai_configured=bool(settings.AI_API_KEY),
        strategy_prompt_enabled=cfg.strategy_prompt_enabled,
        strategy_prompt=cfg.strategy_prompt or "",
        kline_interval=cfg.kline_interval,
        kline_window=cfg.kline_window,
        breakout_threshold=float(cfg.breakout_threshold),
        r_squared_threshold=float(cfg.r_squared_threshold),
        repeat_window_hours=cfg.repeat_window_hours,
        swing_order=cfg.swing_order,
        pullback_tolerance=float(cfg.pullback_tolerance),
    )


# ===== K 线数据 =====

@router.get("/klines/{symbol}")
def get_klines(symbol: str, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    """获取指定币种的最新 K 线数据（用于前端展示）"""
    cfg = _get_system_config(db)
    client = BinanceClient()
    try:
        klines = client.get_klines(symbol, cfg.kline_interval, limit)
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
        return {"symbol": symbol, "interval": cfg.kline_interval, "klines": result}
    finally:
        client.close()


# ===== 手动搜索 AI 分析 =====

@router.post("/analyze", response_model=ManualAnalysisOut)
def analyze_symbol(body: ManualAnalyzeRequest, db: Session = Depends(get_db)):
    """手动分析任意币种（搜索币种页签）：拉取 K 线后同步调用 AI，返回结构化建议"""
    cfg = _get_system_config(db)
    if not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
        raise HTTPException(status_code=403, detail="AI 分析未启用或未配置 API Key")

    symbol = normalize_symbol(body.symbol)
    client = BinanceClient()
    try:
        try:
            klines = client.get_klines(symbol, cfg.kline_interval, 250)
        except Exception:
            raise HTTPException(status_code=400, detail=f"币种 {symbol} 不存在或不可交易")
    finally:
        client.close()

    if len(klines) < 2:
        raise HTTPException(status_code=400, detail=f"币种 {symbol} K线数据不足")

    vol, volume_type = classify_volume(klines)
    # 24h 成交额（USDT）：最近 24 根已收盘 K 线的计价成交量之和
    volume_24h = sum(float(k[7]) for k in klines[-25:-1] if len(k) > 7)
    signal = {
        "symbol": symbol,
        "signal_type": "manual_search",
        "current_price": float(klines[-2][4]),
        "breakout_pct": 0.0,
        "pattern": None,
        "signal_reason": "手动搜索，无预设信号，请根据K线结构自行判断",
        "volume_type": volume_type,
        "volume": vol,
        "volume_24h": volume_24h,
    }

    strategy_prompt = (
        cfg.strategy_prompt
        if cfg.strategy_prompt_enabled and cfg.strategy_prompt
        else None
    )
    try:
        ai_result = analyze_coin(signal, klines, strategy_prompt=strategy_prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 分析失败: {e}")

    return ManualAnalysisOut(
        id=uuid4(),
        symbol=symbol,
        trade_decision=ai_result.get("trade_decision"),
        skip_reason=ai_result.get("skip_reason"),
        direction=ai_result.get("direction"),
        analysis=ai_result.get("analysis"),
        entry_price=ai_result.get("entry_price"),
        stop_loss=ai_result.get("stop_loss"),
        take_profit_1=ai_result.get("take_profit_1"),
        take_profit_2=ai_result.get("take_profit_2"),
        risk_reward_ratio=ai_result.get("risk_reward_ratio"),
        position_pct=ai_result.get("position_pct"),
        recommendation=ai_result.get("recommendation"),
        created_at=datetime.utcnow(),
    )
