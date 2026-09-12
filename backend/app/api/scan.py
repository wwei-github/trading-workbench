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
    ReviewGroupStats,
    ReviewStatsResponse,
)
from app.services.exchange_pool import ExchangePool, AllExchangesFailed
from app.services.scanner import classify_volume
from app.services.ai_analyzer import analyze_coin
from app.services.skill_library import list_skills
from app.services.strategy import compute_signal_key_levels, recent_swings
from app.api.watchlist import normalize_symbol
from app.tasks.scan_tasks import run_scan_task
from app.tasks.ai_tasks import run_ai_analysis_task
from app.models.scan import ScanRecord, ScanResult, AIAnalysis, TradeReview
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
    # 仅保留最近 24 小时内的扫描记录（关注列表手动刷新不进历史）
    cutoff = datetime.utcnow() - timedelta(hours=24)
    total = db.execute(
        select(func.count())
        .select_from(ScanRecord)
        .where(ScanRecord.started_at >= cutoff, ScanRecord.scan_type != "watchlist")
    ).scalar_one()
    items = (
        db.execute(
            select(ScanRecord)
            .where(ScanRecord.started_at >= cutoff, ScanRecord.scan_type != "watchlist")
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
    signal_type: str = Query(None),
    position: str = Query(None),
    pattern: str = Query(None),
    ema_state: str = Query(None),
    db: Session = Depends(get_db),
):
    latest = db.execute(
        select(ScanRecord)
        .where(
            ScanRecord.status == "completed",
            ScanRecord.scan_type != "watchlist",  # 关注列表手动刷新不算常规扫描
        )
        .order_by(desc(ScanRecord.finished_at))
        .limit(1)
    ).scalars().first()
    if not latest:
        return ScanResultListResponse(items=[], total=0)
    return _get_results(
        latest.id, page, page_size, sort_by, order, db,
        signal_type=signal_type, position=position, pattern=pattern,
        ema_state=ema_state,
    )


@router.get("/{scan_id}/results", response_model=ScanResultListResponse)
def scan_results(
    scan_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("breakout_pct"),
    order: str = Query("desc"),
    signal_type: str = Query(None),
    position: str = Query(None),
    pattern: str = Query(None),
    ema_state: str = Query(None),
    db: Session = Depends(get_db),
):
    record = db.get(ScanRecord, scan_id)
    if not record:
        raise HTTPException(status_code=404, detail="扫描记录不存在")
    return _get_results(
        scan_id, page, page_size, sort_by, order, db,
        signal_type=signal_type, position=position, pattern=pattern,
        ema_state=ema_state,
    )


@router.get("/status", response_model=ScanStatusResponse)
def scan_status(db: Session = Depends(get_db)):
    last_scan = db.execute(
        select(ScanRecord)
        .where(ScanRecord.scan_type != "watchlist")  # 关注列表手动刷新不算常规扫描
        .order_by(desc(ScanRecord.started_at))
        .limit(1)
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


def _get_results(
    scan_id: UUID, page: int, page_size: int, sort_by: str, order: str, db: Session,
    signal_type: str = None, position: str = None, pattern: str = None,
    ema_state: str = None,
):
    allowed_sort = {"breakout_pct", "created_at", "symbol", "r_squared", "volume_24h", "volume", "volume_type"}
    if sort_by not in allowed_sort:
        sort_by = "breakout_pct"
    column = getattr(ScanResult, sort_by)
    order_col = desc(column) if order == "desc" else column

    conds = [ScanResult.scan_record_id == scan_id]
    if signal_type:
        conds.append(ScanResult.signal_type == signal_type)
    if position:
        conds.append(ScanResult.position == position)
    if pattern:
        conds.append(ScanResult.pattern == pattern)
    if ema_state:
        conds.append(ScanResult.ema_state == ema_state)

    total = db.execute(
        select(func.count()).select_from(ScanResult).where(*conds)
    ).scalar_one()
    items = (
        db.execute(
            select(ScanResult)
            .where(*conds)
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
        # 清空旧进度事件，保证事件流只属于本次分析（前端流式展示依赖）
        from app.services import ai_progress
        ai_progress.clear(body.scan_result_id)

    run_ai_analysis_task.delay(
        str(scan_id),
        str(body.scan_result_id) if body.scan_result_id else None,
        (body.user_input or "").strip() or None,
    )
    return ScanTriggerResponse(scan_id=scan_id, status="analyzing")


# ===== 系统配置（AI 开关 + 扫描策略） =====

@router.get("/config", response_model=SystemConfigOut)
def get_system_config(db: Session = Depends(get_db)):
    """获取系统配置（AI 开关 + 策略提示词 + 扫描策略）"""
    cfg = _get_system_config(db)
    return SystemConfigOut(
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        ai_pipeline_enabled=cfg.ai_pipeline_enabled,
        dual_judge_enabled=cfg.dual_judge_enabled,
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
        key_level_tolerance=float(cfg.key_level_tolerance),
        level_merge_threshold=float(cfg.level_merge_threshold),
        fib_enabled=cfg.fib_enabled,
        max_open_trades=cfg.max_open_trades,
        strategy_trend_follow_enabled=cfg.strategy_trend_follow_enabled,
        strategy_structure_break_enabled=cfg.strategy_structure_break_enabled,
        strategy_range_edge_enabled=cfg.strategy_range_edge_enabled,
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
    if body.ai_pipeline_enabled is not None:
        cfg.ai_pipeline_enabled = body.ai_pipeline_enabled
    if body.dual_judge_enabled is not None:
        cfg.dual_judge_enabled = body.dual_judge_enabled

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
    if body.key_level_tolerance is not None:
        cfg.key_level_tolerance = body.key_level_tolerance
    if body.level_merge_threshold is not None:
        cfg.level_merge_threshold = body.level_merge_threshold
    if body.fib_enabled is not None:
        cfg.fib_enabled = body.fib_enabled
    if body.max_open_trades is not None:
        if not (1 <= body.max_open_trades <= 50):
            raise HTTPException(status_code=400, detail="max_open_trades 需在 1~50 之间")
        cfg.max_open_trades = body.max_open_trades
    # 开单策略开关
    if body.strategy_trend_follow_enabled is not None:
        cfg.strategy_trend_follow_enabled = body.strategy_trend_follow_enabled
    if body.strategy_structure_break_enabled is not None:
        cfg.strategy_structure_break_enabled = body.strategy_structure_break_enabled
    if body.strategy_range_edge_enabled is not None:
        cfg.strategy_range_edge_enabled = body.strategy_range_edge_enabled

    db.commit()
    db.refresh(cfg)
    return SystemConfigOut(
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        ai_pipeline_enabled=cfg.ai_pipeline_enabled,
        dual_judge_enabled=cfg.dual_judge_enabled,
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
        key_level_tolerance=float(cfg.key_level_tolerance),
        level_merge_threshold=float(cfg.level_merge_threshold),
        fib_enabled=bool(cfg.fib_enabled),
        max_open_trades=cfg.max_open_trades,
        strategy_trend_follow_enabled=bool(cfg.strategy_trend_follow_enabled),
        strategy_structure_break_enabled=bool(cfg.strategy_structure_break_enabled),
        strategy_range_edge_enabled=bool(cfg.strategy_range_edge_enabled),
    )


# ===== 复盘统计（docs/04 §10 P2） =====

def _agg_stats(rows: list[TradeReview]) -> dict:
    wins = sum(1 for r in rows if r.outcome in ("win_tp1", "win_tp2"))
    losses = sum(1 for r in rows if r.outcome == "loss")
    return {
        "win_tp1": sum(1 for r in rows if r.outcome == "win_tp1"),
        "win_tp2": sum(1 for r in rows if r.outcome == "win_tp2"),
        "loss": losses,
        "expired": sum(1 for r in rows if r.outcome == "expired"),
        "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else 0.0,
    }


@router.get("/review/stats", response_model=ReviewStatsResponse)
def review_stats(days: int = Query(30, ge=1, le=180), db: Session = Depends(get_db)):
    """AI 建议复盘统计：整体 + 按 signal_type/position/ema_state 分组胜率"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(TradeReview).where(TradeReview.created_at >= cutoff)
    ).scalars().all()

    resp = ReviewStatsResponse(days=days, **_agg_stats(rows), groups=[])

    for dim in ("signal_type", "position", "ema_state"):
        buckets: dict[str, list[TradeReview]] = {}
        for r in rows:
            key = getattr(r, dim)
            if key:
                buckets.setdefault(key, []).append(r)
        for key, items in sorted(buckets.items()):
            agg = _agg_stats(items)
            resp.groups.append(ReviewGroupStats(**{dim: key, **agg}))
    return resp


# ===== Agent trace 查询（docs/04 §5.9） =====

@router.get("/ai-trace/{analysis_id}")
def get_ai_trace(analysis_id: UUID, db: Session = Depends(get_db)):
    """查询某次 AI 分析的 Agent 工具循环 trace（P0 管线无 trace，返回 null）"""
    a = db.get(AIAnalysis, analysis_id)
    if not a:
        raise HTTPException(status_code=404, detail="分析记录不存在")
    return {"analysis_id": a.id, "stage_trace": a.stage_trace}


# ===== AI 分析进度（流式展示，前端 2s 轮询 Redis 事件流） =====

@router.get("/ai-progress/{scan_result_id}")
def get_ai_progress(scan_result_id: UUID, db: Session = Depends(get_db)):
    """单个扫描结果的 AI 分析进度事件。
    status: done=分析已落库 / running=进行中 / idle=无事件（未开始或已过期）
    """
    from app.services import ai_progress

    done = db.execute(
        select(AIAnalysis.id).where(AIAnalysis.scan_result_id == scan_result_id)
    ).first() is not None
    events = ai_progress.get_events(scan_result_id)
    # 终态感知：落库=done，末事件为 error=error（否则 error 后仍显示 running，像卡住）
    if done:
        status = "done"
    elif events and events[-1].get("t") == "error":
        status = "error"
    else:
        status = "running" if events else "idle"
    return {"scan_result_id": str(scan_result_id), "status": status, "events": events}


# ===== 技能库（只读，docs/04 §6.5） =====

@router.get("/skills")
def skills():
    return [{"name": s["name"], "description": s["description"],
             "use_when": s["use_when"], "version": s["version"]}
            for s in list_skills()]


@router.get("/skills/{name}")
def skill_detail(name: str):
    for s in list_skills():
        if s["name"] == name:
            return s
    raise HTTPException(status_code=404, detail="技能不存在")


# ===== K 线数据 =====

@router.get("/klines/{symbol}")
def get_klines(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
    force_refresh: bool = Query(False, description="跳过缓存直接拉取交易所最新数据并重置缓存"),
    db: Session = Depends(get_db),
):
    """获取指定币种的最新 K 线数据（用于前端展示）

    多交易所故障转移（币安→欧易→Bitget）；全部失败时降级返回旧缓存，
    仍失败则 503，detail 含各交易所失败原因与解禁时长。
    force_refresh=True 时跳过缓存直连交易所（图表"更新"按钮），结果回写缓存。
    """
    cfg = _get_system_config(db)
    pool = ExchangePool()
    try:
        klines = pool.get_klines(
            symbol, cfg.kline_interval, limit, allow_stale=True, force_refresh=force_refresh
        )
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线获取失败（{e.summary}）")
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
    # 近期摆动点（图表标注用，各 5 个；bars_ago 换算回时间戳）
    sw = recent_swings(klines, order=cfg.swing_order, n=5)
    n_bars = len(klines)
    swings = {
        kind: [
            {
                "time": int(klines[n_bars - 1 - it["bars_ago"]][0]),
                "price": it["price"],
                "label": it["label"],
            }
            for it in sw[kind]
        ]
        for kind in ("highs", "lows")
    }
    # 关键位（图表区域色块用，与扫描/AI 同源：支撑位/压力位 + ATR 自适应区域）
    levels = compute_signal_key_levels(
        klines,
        {
            "min_klines": 30,
            "swing_order": cfg.swing_order,
            "r_squared_threshold": float(cfg.r_squared_threshold),
            "pullback_tolerance": float(cfg.pullback_tolerance),
            "key_level_tolerance": float(cfg.key_level_tolerance),
            "level_merge_threshold": float(cfg.level_merge_threshold),
            "max_trend_slope": 0.005,
        },
    )
    return {
        "symbol": symbol,
        "interval": cfg.kline_interval,
        "klines": result,
        "swings": swings,
        "key_levels": levels,
    }


# ===== 手动搜索 AI 分析 =====

@router.post("/analyze", response_model=ManualAnalysisOut)
def analyze_symbol(body: ManualAnalyzeRequest, db: Session = Depends(get_db)):
    """手动分析任意币种（搜索币种页签）：拉取 K 线后同步调用 AI，返回结构化建议"""
    cfg = _get_system_config(db)
    if not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
        raise HTTPException(status_code=403, detail="AI 分析未启用或未配置 API Key")

    symbol = normalize_symbol(body.symbol)
    pool = ExchangePool()
    try:
        klines = pool.get_klines(symbol, cfg.kline_interval, 500)
    except AllExchangesFailed as e:
        raise HTTPException(
            status_code=503,
            detail=f"K线获取失败，无法分析（{e.summary}）",
        )

    if len(klines) < 2:
        raise HTTPException(status_code=400, detail=f"币种 {symbol} K线数据不足")

    vol, volume_type = classify_volume(klines)
    # 24h 成交额（USDT）：最近 24 根已收盘 K 线的计价成交量之和
    volume_24h = sum(float(k[7]) for k in klines[-25:-1] if len(k) > 7)
    signal = {
        "symbol": symbol,
        "signal_type": "manual_search",
        # 分析时刻最新价（未收盘K线的现价），入场价锚定基准
        "current_price": float(klines[-1][4]),
        "breakout_pct": 0.0,
        "pattern": None,
        "signal_reason": "手动搜索，无预设信号，请根据K线结构自行判断",
        "volume_type": volume_type,
        "volume": vol,
        "volume_24h": volume_24h,
        # 近期摆动结构（HH/LH/HL/LL，高低点各 20 个）：足够 AI 锚定止盈档位与
        # 评估关键位的历史触及密度
        "recent_swings": recent_swings(klines, order=cfg.swing_order, n=20),
        # 分析时刻计算的关键位（前高前低/支撑压力/区间边界，含时间加权与 ATR 自适应区域）
        "key_levels": compute_signal_key_levels(klines, {
            "swing_order": cfg.swing_order,
            "key_level_tolerance": float(cfg.key_level_tolerance),
            "level_merge_threshold": float(cfg.level_merge_threshold),
        }),
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
        trade_type=ai_result.get("trade_type"),
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
