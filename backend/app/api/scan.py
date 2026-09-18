from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, desc, select, update, func
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
    ActualReviewResponse,
    AttributionTriggerResponse,
    PatchListResponse,
    PatchDetailResponse,
    PatchOut,
)
from app.services.exchange_pool import ExchangePool, AllExchangesFailed
from app.services.scanner import classify_volume
from app.services.ai_analyzer import analyze_coin
from app.services.risk_guard import is_pullback_wait
from app.services.skill_library import list_skills
from app.services.strategy import recent_swings
from app.api.watchlist import normalize_symbol
from app.tasks.scan_tasks import run_scan_task
from app.tasks.ai_tasks import run_ai_analysis_task
from app.models.scan import ScanRecord, ScanResult, AIAnalysis, TradeReview
from app.models.trade import TradeRecord
from app.models.patch import PromptPatch
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
    allowed_sort = {"breakout_pct", "created_at", "symbol", "r_squared", "volume_24h", "volume", "volume_type", "quality_score"}
    if sort_by not in allowed_sort:
        sort_by = "breakout_pct"
    column = getattr(ScanResult, sort_by)
    # desc 时 NULL 排最后（PG 默认 NULLS FIRST）：旧管线历史行 quality_score 为空，不插队
    order_col = desc(column).nulls_last() if order == "desc" else column

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

    # 单币重新分析：先删除旧结果，前端轮询以"新记录出现"为完成标志。
    # 已被交易记录/复盘引用的旧结论不能删（FK 约束，直接删会 500）——置空
    # scan_result_id 摘钩保留（交易记录自持 ai_snapshot 快照，复盘统计不受影响）；
    # 无引用的照常删除。新结论落库时按 scan_result_id 另起新行（latest-wins upsert）
    if body.scan_result_id:
        old_ids = db.execute(
            select(AIAnalysis.id).where(AIAnalysis.scan_result_id == body.scan_result_id)
        ).scalars().all()
        if old_ids:
            referenced = set(db.execute(
                select(TradeRecord.ai_analysis_id).where(
                    TradeRecord.ai_analysis_id.in_(old_ids))
            ).scalars().all()) | set(db.execute(
                select(TradeReview.ai_analysis_id).where(
                    TradeReview.ai_analysis_id.in_(old_ids))
            ).scalars().all())
            if referenced:
                db.execute(
                    update(AIAnalysis)
                    .where(AIAnalysis.id.in_(referenced))
                    .values(scan_result_id=None)
                )
                db.execute(
                    delete(AIAnalysis).where(
                        AIAnalysis.scan_result_id == body.scan_result_id,
                        ~AIAnalysis.id.in_(referenced),
                    )
                )
            else:
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
        fib_enabled=cfg.fib_enabled,
        max_open_trades=cfg.max_open_trades,
        trading_min_free_pct=float(cfg.trading_min_free_pct),
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
    if body.fib_enabled is not None:
        cfg.fib_enabled = body.fib_enabled
    if body.max_open_trades is not None:
        if not (1 <= body.max_open_trades <= 50):
            raise HTTPException(status_code=400, detail="max_open_trades 需在 1~50 之间")
        cfg.max_open_trades = body.max_open_trades
    if body.trading_min_free_pct is not None:
        if not (0 <= body.trading_min_free_pct <= 1):
            raise HTTPException(status_code=400, detail="trading_min_free_pct 需在 0~1 之间")
        cfg.trading_min_free_pct = body.trading_min_free_pct
    # 规则补丁参数（docs/12 §10）：显式传 null = 恢复默认（用 model_fields_set 区分未传）
    if "max_stop_pct" in body.model_fields_set:
        v = body.max_stop_pct
        if v is not None and not (0 < v < 1):
            raise HTTPException(status_code=400, detail="max_stop_pct 需为比例值（0.04=4%）")
        cfg.max_stop_pct = v
    if "liq_gate_buffer" in body.model_fields_set:
        v = body.liq_gate_buffer
        if v is not None and not (0 < v < 0.05):
            raise HTTPException(status_code=400, detail="liq_gate_buffer 需为比例值（0.005=0.5%，<5%）")
        cfg.liq_gate_buffer = v
    if "min_adapt_leverage" in body.model_fields_set:
        v = body.min_adapt_leverage
        if v is not None and not (1 <= v <= settings.TRADING_LEVERAGE):
            raise HTTPException(
                status_code=400,
                detail=f"min_adapt_leverage 需在 1~{settings.TRADING_LEVERAGE} 之间（动态杠杆下限，倍数）",
            )
        cfg.min_adapt_leverage = v
    if "pullback_market_pct" in body.model_fields_set:
        v = body.pullback_market_pct
        if v is not None and not (0 < v < 0.05):
            raise HTTPException(status_code=400, detail="pullback_market_pct 需为比例值（0.01=1%，<5%）")
        cfg.pullback_market_pct = v
    if "max_slippage_pct" in body.model_fields_set:
        v = body.max_slippage_pct
        if v is not None and not (0 < v < 0.05):
            raise HTTPException(status_code=400, detail="max_slippage_pct 需为比例值（0.003=0.3%，<5%）")
        cfg.max_slippage_pct = v

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
        fib_enabled=bool(cfg.fib_enabled),
        max_open_trades=cfg.max_open_trades,
        trading_min_free_pct=float(cfg.trading_min_free_pct),
        max_stop_pct=float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None,
        liq_gate_buffer=(float(cfg.liq_gate_buffer)
                         if cfg.liq_gate_buffer is not None else None),
        min_adapt_leverage=cfg.min_adapt_leverage,
        pullback_market_pct=(float(cfg.pullback_market_pct)
                             if cfg.pullback_market_pct is not None else None),
        max_slippage_pct=(float(cfg.max_slippage_pct)
                          if cfg.max_slippage_pct is not None else None),
    )


# ===== 复盘统计（docs/04 §10 P2） =====

def _agg_stats(rows: list[TradeReview]) -> dict:
    wins = sum(1 for r in rows if r.outcome in ("win_tp1", "win_tp2"))
    losses = sum(1 for r in rows if r.outcome == "loss")
    return {
        "total": len(rows),
        "win_tp1": sum(1 for r in rows if r.outcome == "win_tp1"),
        "win_tp2": sum(1 for r in rows if r.outcome == "win_tp2"),
        "loss": losses,
        "expired": sum(1 for r in rows if r.outcome == "expired"),
        "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else 0.0,
    }


@router.get("/review/stats", response_model=ReviewStatsResponse)
def review_stats(days: int = Query(30, ge=1, le=180), db: Session = Depends(get_db)):
    """AI 建议复盘统计：整体 + 按 signal_type/position/ema_state 分组胜率 + 期望值(R)"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(TradeReview).where(TradeReview.created_at >= cutoff)
    ).scalars().all()

    # 期望值口径（R 倍数）：loss=-1R、win_tp1=RR1、win_tp2=RR2（简化：不分批），
    # 减去费用假设——taker 往返 0.1% 名义 + 资金费 0.03%/8h（按持仓 K 线数折算），
    # 费用换算成 R：cost_pct / stop_pct。无对应 AI 三价（手动分析/数据缺失）的行不计
    ai_map: dict = {}
    ai_ids = [r.ai_analysis_id for r in rows if r.ai_analysis_id]
    if ai_ids:
        for a in db.execute(
            select(AIAnalysis).where(AIAnalysis.id.in_(ai_ids))
        ).scalars().all():
            ai_map[str(a.id)] = a

    def _ev_r(rev: TradeReview) -> Optional[float]:
        a = ai_map.get(str(rev.ai_analysis_id)) if rev.ai_analysis_id else None
        if not a or not a.entry_price or not a.stop_loss or not a.take_profit_1:
            return None
        entry, stop, tp1 = float(a.entry_price), float(a.stop_loss), float(a.take_profit_1)
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or entry <= 0:
            return None
        if rev.outcome == "loss":
            r_mult = -1.0
        elif rev.outcome == "win_tp1":
            r_mult = abs(tp1 - entry) / stop_dist
        elif rev.outcome == "win_tp2" and a.take_profit_2:
            r_mult = abs(float(a.take_profit_2) - entry) / stop_dist
        else:
            return None
        cost_pct = 0.001 + 0.0003 * max(1, (rev.bars_to_exit or 1) // 8)  # 往返手续费 + 资金费(0.03%/8h)
        return round(r_mult - cost_pct / (stop_dist / entry), 3)

    for rev in rows:
        rev.ev = _ev_r(rev)  # 非映射属性，分组聚合用

    def _agg_ev(items: list) -> Optional[float]:
        vals = [r.ev for r in items if getattr(r, "ev", None) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    resp = ReviewStatsResponse(
        days=days, **_agg_stats(rows), ev_r=_agg_ev(rows), groups=[],
    )

    for dim in ("signal_type", "position", "ema_state"):
        buckets: dict[str, list[TradeReview]] = {}
        for r in rows:
            key = getattr(r, dim)
            if key:
                buckets.setdefault(key, []).append(r)
        for key, items in sorted(buckets.items()):
            agg = _agg_stats(items)
            resp.groups.append(ReviewGroupStats(**{dim: key, **agg, "ev_r": _agg_ev(items)}))
    return resp


@router.get("/review/actual", response_model=ActualReviewResponse)
def review_actual(days: int = Query(30, ge=1, le=180), db: Session = Depends(get_db)):
    """实盘归因统计（复盘主轴）：trade_records 总览 + 分组 + 逐笔计划vs实际 + AI 错误标签"""
    from app.services.actual_review import backfill_costs, build_actual_review
    from app.services.binance_trader import BinanceTrader

    try:
        backfill_costs(db, BinanceTrader(), limit=20)  # 存量平仓单费用一次性回补
    except Exception as e:
        # 回补失败不阻塞统计（费用列显示 None）
        import logging
        logging.getLogger(__name__).warning("费用回补失败: %s", e)

    return build_actual_review(db, days)


@router.post("/review/attribute", response_model=AttributionTriggerResponse)
def review_attribute(body: dict, db: Session = Depends(get_db)):
    """手动触发 AI 错误归因：body.trade_id 指定单笔，缺省处理全部待归因（≤5 笔）"""
    from app.services.actual_review import backfill_costs
    from app.services.binance_trader import BinanceTrader
    from app.services.exchange_pool import AllExchangesFailed
    from app.tasks.review_tasks import run_ai_attribution

    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="AI 未配置（AI_API_KEY 为空）")

    trade_id = body.get("trade_id")
    q = (
        select(TradeRecord)
        .where(
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_reason.isnot(None),
            TradeRecord.testnet == settings.TRADING_TESTNET,
        )
        .order_by(TradeRecord.closed_at.desc())
    )
    if trade_id:
        try:
            tid = UUID(str(trade_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="trade_id 格式非法")
        q = q.where(TradeRecord.id == tid)
    else:
        q = q.where(TradeRecord.attributed_at.is_(None))
    trades = db.execute(q.limit(5)).scalars().all()
    if not trades:
        raise HTTPException(status_code=404, detail="没有待归因的交易")

    trader = BinanceTrader()
    try:
        backfill_costs(db, trader, limit=5)
    except Exception:
        pass

    pool = ExchangePool()
    try:
        results = run_ai_attribution(db, pool, trader, trades)
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线获取失败（{e.summary}）")
    processed = sum(1 for r in results if r.get("ok"))
    return {"processed": processed, "results": results}


# ===== 规则补丁闭环（docs/12 §10） =====

@router.get("/review/patches", response_model=PatchListResponse)
def review_patches(db: Session = Depends(get_db)):
    """补丁列表（告警置顶）：含合入前后对照指标（评估任务写入）"""
    from app.services.patch_service import RULE_TAGS, window_stats

    rows = db.execute(
        select(PromptPatch).order_by(PromptPatch.created_at.desc())
    ).scalars().all()
    stats = window_stats(db, days=7)  # 当前实时热度（列表底部展示）
    items = [PatchOut.model_validate(p) for p in rows]
    # 恶化告警置顶，其余按创建时间倒序
    items.sort(key=lambda x: (x.verdict != "worse",))
    return PatchListResponse(items=items, stats=stats, rule_tags=list(RULE_TAGS))


@router.get("/review/patches/{patch_id}", response_model=PatchDetailResponse)
def review_patch_detail(patch_id: UUID, db: Session = Depends(get_db)):
    """补丁对照明细：前后指标 + 证据案例交易 ID"""
    p = db.get(PromptPatch, patch_id)
    if not p:
        raise HTTPException(status_code=404, detail="补丁不存在")
    return PatchDetailResponse.model_validate(p)


@router.post("/review/patches/{patch_id}/apply", response_model=PatchOut)
def review_patch_apply(patch_id: UUID, db: Session = Depends(get_db)):
    """合入草稿：提示词追加覆盖段 / 参数写 system_config 列（即时生效）"""
    from app.services.patch_service import apply_patch
    try:
        return PatchOut.model_validate(apply_patch(db, str(patch_id)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/review/patches/{patch_id}/rollback", response_model=PatchOut)
def review_patch_rollback(patch_id: UUID, db: Session = Depends(get_db)):
    """回滚：提示词删标记段 / 参数恢复默认。页面一键操作，即时生效"""
    from app.services.patch_service import rollback_patch
    try:
        return PatchOut.model_validate(rollback_patch(db, str(patch_id)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/review/patches/{patch_id}")
def review_patch_delete(patch_id: UUID, db: Session = Depends(get_db)):
    """删除草稿（仅 draft 可删；已合入的走回滚保留历史）"""
    p = db.get(PromptPatch, patch_id)
    if not p:
        raise HTTPException(status_code=404, detail="补丁不存在")
    if p.status != "draft":
        raise HTTPException(status_code=400, detail="仅草稿可删除，已合入的请走回滚")
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.get("/review/prompt-view")
def review_prompt_view(db: Session = Depends(get_db)):
    """当前生效提示词分层视图：系统自带（代码常量）/用户自定义/补丁覆盖段 + 参数"""
    from app.services.patch_service import build_prompt_view
    return build_prompt_view(db)


@router.post("/review/patches/trigger")
def review_patch_trigger(db: Session = Depends(get_db)):
    """手动触发一次补丁草稿生成（立即调 LLM，不等每日定时）"""
    from app.services.patch_service import run_trigger
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="AI 未配置（AI_API_KEY 为空）")
    drafts = run_trigger(db)
    return {"created": len(drafts), "drafts": [
        {"id": d.id, "tag": d.target_tag, "kind": d.kind} for d in drafts
    ]}


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


# ===== 币种黑名单（2026-09-18，GRIFFAIN/4USDT 批次教训） =====

@router.get("/blacklist")
def blacklist_list(db: Session = Depends(get_db)):
    """黑名单列表（含已过期/停用行，前端按状态区分展示）"""
    from app.models.blacklist import SymbolBlacklist
    from app.services.blacklist_service import active_symbols
    rows = db.execute(select(SymbolBlacklist).order_by(desc(SymbolBlacklist.created_at))).scalars().all()
    act = active_symbols(db)
    now = datetime.utcnow()
    return [{
        "id": str(r.id),
        "symbol": r.symbol,
        "origin": r.origin,  # manual / auto
        "reason": r.reason,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "expired": bool(r.origin == "auto" and r.expires_at and r.expires_at <= now),
        "active": r.symbol in act,
        "disabled": r.disabled,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/blacklist")
def blacklist_add(body: dict, db: Session = Depends(get_db)):
    """手动添加（永久）。body: {symbol, reason?}"""
    from app.models.blacklist import SymbolBlacklist
    from app.services.blacklist_service import add_manual
    symbol = normalize_symbol(str(body.get("symbol") or "").strip())
    if not symbol or not symbol.endswith("USDT"):
        raise HTTPException(status_code=400, detail="请输入有效的 USDT 合约币种（如 4USDT）")
    row = add_manual(db, symbol, str(body.get("reason") or "").strip())
    db.commit()
    return {"id": str(row.id), "symbol": row.symbol, "origin": "manual"}


@router.delete("/blacklist/{symbol}")
def blacklist_remove(symbol: str, db: Session = Depends(get_db)):
    """手动移除（删行，manual/auto 都可删）"""
    from app.services.blacklist_service import remove
    ok = remove(db, normalize_symbol(symbol.strip().upper()))
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail="该币种不在黑名单")
    return {"ok": True}


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
    # 关键位功能已移除（docs/09）：图表不再返回关键位线，仅保留摆动点标记
    return {
        "symbol": symbol,
        "interval": cfg.kline_interval,
        "klines": result,
        "swings": swings,
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

    # 4h 高周期 K 线（大方向背景注入）；失败只降级为无 4h 行，不中断分析
    try:
        htf_klines = pool.get_klines(symbol, "4h", 200)
    except Exception:
        htf_klines = None

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
        # 评估结构位的历史密度
        "recent_swings": recent_swings(klines, order=cfg.swing_order, n=20),
    }

    strategy_prompt = (
        cfg.strategy_prompt
        if cfg.strategy_prompt_enabled and cfg.strategy_prompt
        else None
    )
    try:
        ai_result = analyze_coin(
            signal, klines, strategy_prompt=strategy_prompt, htf_klines=htf_klines,
        )
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
        pullback_wait=is_pullback_wait(ai_result, signal.get("current_price")),
        created_at=datetime.utcnow(),
    )
