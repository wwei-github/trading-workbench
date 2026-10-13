from datetime import datetime, timedelta, timezone
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
    AccountBrief,
    ReviewGroupStats,
    ReviewStatsResponse,
    ReviewFunnelResponse,
    FunnelStage,
    ActualReviewResponse,
    AttributionTriggerResponse,
    PatchListResponse,
    PatchDetailResponse,
    PatchOut,
)
from app.services.exchange_pool import ExchangePool, AllExchangesFailed
from app.services import market_data as md
from app.services.scanner import classify_volume
from app.services.trade_engine import breaker_status_by_net
from app.services.ai_analyzer import analyze_coin
from app.services.risk_guard import is_pullback_wait
from app.services.skill_library import list_skills
from app.services.strategy import recent_swings, swing_annotations
from app.services.strategy.technical import enforce_htf_constraint
from app.api.watchlist import normalize_symbol
from app.tasks.scan_tasks import run_scan_task
from app.tasks.ai_tasks import run_ai_analysis_task
from app.models.scan import PullbackPending, ScanRecord, ScanResult, AIAnalysis, TradeReview
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


@router.get("/{scan_id}/funnel")
def scan_funnel(scan_id: UUID, db: Session = Depends(get_db)):
    """筛选漏斗明细（2026-10-11）：该轮各关卡被拒计数 + symbol→关卡全量映射。

    前端「筛选漏斗」Tab 展示每轮币种卡在哪一步；funnel_json 为 NULL
    （旧行/异常轮次）返回 404 语义由前端空态处理，这里返回 {funnel: None}。
    关卡代码→中文文案映射在前端（FUNNEL_STAGES），后端只回结构化数据。
    """
    record = db.get(ScanRecord, scan_id)
    if not record:
        raise HTTPException(status_code=404, detail="扫描记录不存在")
    return {"scan_id": str(scan_id), "funnel": record.funnel_json}


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
        trading_enabled_testnet=cfg.trading_enabled_testnet,
        trading_enabled_live=cfg.trading_enabled_live,
        # 2026-10-03 删环境切换：在跑单统计为双网合计（明细按记录自带 testnet 区分）
        open_trades=db.execute(
            select(func.count()).select_from(TradeRecord).where(
                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
            )
        ).scalar_one(),
        open_tp_hit=db.execute(
            select(func.count()).select_from(TradeRecord).where(
                TradeRecord.status.in_(("TP1_HIT", "TP2_HIT")),
            )
        ).scalar_one(),
        # 在跑 OPENED 按网计数（2026-10-04 拆分显示）：满仓跳过判定 = 任一网达上限
        open_trades_testnet=db.execute(
            select(func.count()).select_from(TradeRecord).where(
                TradeRecord.status == "OPENED", TradeRecord.testnet == True,  # noqa: E712
            )
        ).scalar_one(),
        open_trades_live=db.execute(
            select(func.count()).select_from(TradeRecord).where(
                TradeRecord.status == "OPENED", TradeRecord.testnet == False,  # noqa: E712
            )
        ).scalar_one(),
        max_open_trades=cfg.max_open_trades,
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        # 各网当日熔断状态（顶部卡片；钱包 5min 缓存，展示口径非拦截口径）
        loss_breaker=breaker_status_by_net(db, cfg),
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
    # 手动单币重分析（scan_result_id 非空）不受「筛选后自动 AI 分析」开关限制，
    # 仅要求已配置 API_KEY；无 scan_result_id 的全量触发仍受开关约束（批量自动分析语义）
    if not settings.AI_API_KEY or (
        not body.scan_result_id and not cfg.ai_analysis_enabled
    ):
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

def _config_response(cfg: SystemConfig) -> SystemConfigOut:
    """SystemConfigOut 统一构造（GET/PUT 共用；含账号注册表只读展示，docs/17）"""
    from app.services.account_registry import (
        live_accounts,
        parse_live_keys,
        registry,
        testnet_accounts,
    )
    from app.services.scan_filters import resolve_scan_filters
    flt = resolve_scan_filters(cfg)
    return SystemConfigOut(
        ai_analysis_enabled=cfg.ai_analysis_enabled,
        ai_pipeline_enabled=cfg.ai_pipeline_enabled,
        dual_judge_enabled=cfg.dual_judge_enabled,
        tp2_enabled=cfg.tp2_enabled,
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
        risk_budget_mode=cfg.risk_budget_mode or "pct",
        risk_fixed_amount=float(cfg.risk_fixed_amount if cfg.risk_fixed_amount is not None else 3.0),
        trading_enabled_testnet=cfg.trading_enabled_testnet,
        trading_enabled_live=cfg.trading_enabled_live,
        current_account_key=cfg.current_account_key,
        live_trading_keys=parse_live_keys(cfg.live_trading_keys),
        accounts=[AccountBrief(key=a.key, name=a.name, testnet=a.testnet) for a in registry()],
        binance_key_live=bool(live_accounts()),
        binance_key_testnet=bool(testnet_accounts()) or bool(live_accounts()),
        max_stop_pct=float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None,
        liq_gate_buffer=(float(cfg.liq_gate_buffer)
                         if cfg.liq_gate_buffer is not None else None),
        min_adapt_leverage=cfg.min_adapt_leverage,
        pullback_market_pct=(float(cfg.pullback_market_pct)
                             if cfg.pullback_market_pct is not None else None),
        max_slippage_pct=(float(cfg.max_slippage_pct)
                          if cfg.max_slippage_pct is not None else None),
        min_recommendation=cfg.min_recommendation,
        daily_loss_stop_pct=(float(cfg.daily_loss_stop_pct)
                             if cfg.daily_loss_stop_pct is not None else None),
        daily_loss_stop_enabled=cfg.daily_loss_stop_enabled,
        ai_rr_min=(float(cfg.ai_rr_min) if cfg.ai_rr_min is not None else None),
        ai_rr_tol=(float(cfg.ai_rr_tol) if cfg.ai_rr_tol is not None else None),
        risk_pct=(float(cfg.risk_pct) if cfg.risk_pct is not None else None),
        leverage_max=cfg.leverage_max,
        taker_fee_rate=(float(cfg.taker_fee_rate)
                        if cfg.taker_fee_rate is not None else None),
        near_anchor_factor=(float(cfg.near_anchor_factor)
                            if cfg.near_anchor_factor is not None else None),
        portfolio_heat_cap=(float(cfg.portfolio_heat_cap)
                            if cfg.portfolio_heat_cap is not None else None),
        same_direction_cap=cfg.same_direction_cap,
        btc_crash_pause_pct=(float(cfg.btc_crash_pause_pct)
                             if cfg.btc_crash_pause_pct is not None else None),
        weekly_dd_stop_pct=(float(cfg.weekly_dd_stop_pct)
                            if cfg.weekly_dd_stop_pct is not None else None),
        monthly_dd_stop_pct=(float(cfg.monthly_dd_stop_pct)
                             if cfg.monthly_dd_stop_pct is not None else None),
        monthly_breaker_ack_at=cfg.monthly_breaker_ack_at,
        weekly_breaker_ack_at=cfg.weekly_breaker_ack_at,
        atr_trail_k=(float(cfg.atr_trail_k)
                     if cfg.atr_trail_k is not None else None),
        tp1_qty_pct_with_tp2=(float(cfg.tp1_qty_pct_with_tp2)
                              if cfg.tp1_qty_pct_with_tp2 is not None else None),
        tp2_qty_pct=(float(cfg.tp2_qty_pct)
                     if cfg.tp2_qty_pct is not None else None),
        tp1_qty_pct_no_tp2=(float(cfg.tp1_qty_pct_no_tp2)
                            if cfg.tp1_qty_pct_no_tp2 is not None else None),
        live_initial_capital=float(cfg.live_initial_capital or 0),
        scan_filters=(cfg.scan_filters if isinstance(cfg.scan_filters, dict) else None),
        scan_filters_effective={
            "min_volume_24h": flt.min_volume_24h,
            "new_coin_cooldown_days": flt.new_coin_cooldown_days,
            "breakout_channel_bars": flt.breakout_channel_bars,
            "breakout_threshold": flt.breakout_threshold,
            "breakout_vol_ratio": flt.breakout_vol_ratio,
            "breakout_max_pct": flt.breakout_max_pct,
            "candle_max_ema21_dist_pct": flt.candle_max_ema21_dist_pct,
            "ai_max_per_scan": flt.ai_max_per_scan,
            "allowed_triggers": list(flt.allowed_triggers),
            # 回踩触点体系（docs/22）：启用触点白名单 + 触点参数
            "pullback_touch_refs": list(flt.pullback_touch_refs),
            "retest_window_bars": flt.retest_window_bars,
            "pending_confirm_window_bars": flt.pending_confirm_window_bars,
            "structure_touch_band_pct": flt.structure_touch_band_pct,
            "ema_touch_min_bars": flt.ema_touch_min_bars,
            "structure_1h_gate": flt.structure_1h_gate,
        },
    )


# 筛选参数组校验表（2026-09-28）：键 → (最小值, 最大值, 说明)
_SCAN_FILTER_RANGES: dict[str, tuple[float, float, str]] = {
    "min_volume_24h": (0, 1e9, "24h 成交额门槛（USDT），0=不过滤"),
    "new_coin_cooldown_days": (0, 90, "新币冷静期（天，上市未满 N 天不进扫描，0=关闭）"),
    "breakout_channel_bars": (5, 200, "Donchian 通道回看根数"),
    "breakout_threshold": (0, 0.05, "最小突破幅度（比例，0.002=0.2%）"),
    "breakout_vol_ratio": (0, 10, "突破量比加分门槛（信号K线量/近20根中位量，达标+5分不卡死）"),
    "breakout_max_pct": (0, 20, "突破幅度上限（%，防追高，0=关闭）"),
    "candle_max_ema21_dist_pct": (0, 20, "悬空金K距 EMA21 上限（%，0=关闭）"),
    "ai_max_per_scan": (1, 50, "单轮扫描 AI 分析上限（个）"),
    # 回踩触点体系（docs/22）
    "retest_window_bars": (0, 500, "突破回测触点有效窗口（已收盘根数，0=关闭 retest 触点）"),
    "pending_confirm_window_bars": (1, 10, "回踩待确认窗口（已收盘根数，金K须出现在触碰根后 1~N 根内）"),
    "structure_touch_band_pct": (0, 5, "retest/swing 触碰带宽（%，影线可入带收盘须守住）"),
    "ema_touch_min_bars": (50, 1000, "EMA 触点最小已收盘根数（数据门）"),
}


def _validate_scan_filters(raw: dict) -> dict:
    """筛选参数组校验：未知键拒绝（防拼错静默失效）、范围越界 400、数值化清洗。
    合法但缺省的键不补齐——由调用方 `_merge_scan_filters` 与库内旧值做键级合并
    （合并后仍缺的键回落 env）。非数值键：allowed_triggers / pullback_touch_refs
    （非空合法类型列表）、structure_1h_gate（布尔开关，2026-10-11）。"""
    from app.services.scan_filters import ALL_TRIGGERS, ALL_TOUCH_REFS

    out: dict = {}
    for k, v in raw.items():
        if k == "structure_1h_gate":
            if not isinstance(v, bool):
                raise HTTPException(status_code=400,
                                    detail="scan_filters.structure_1h_gate 需为布尔值")
            out[k] = v
        elif k == "allowed_triggers":
            if (not isinstance(v, list) or not v
                    or not all(t in ALL_TRIGGERS for t in v)):
                raise HTTPException(
                    status_code=400,
                    detail="scan_filters.allowed_triggers 需为非空触发类型列表"
                           f"（合法值: {', '.join(ALL_TRIGGERS)}）",
                )
            out[k] = v
        elif k == "pullback_touch_refs":
            # 空列表拒绝（触点全空会让回踩管线停摆）；非法项拒绝（防拼错静默失效）
            if (not isinstance(v, list) or not v
                    or not all(t in ALL_TOUCH_REFS for t in v)):
                raise HTTPException(
                    status_code=400,
                    detail="scan_filters.pullback_touch_refs 需为非空触点类型列表"
                           f"（合法值: {', '.join(ALL_TOUCH_REFS)}）",
                )
            out[k] = v
        elif k in _SCAN_FILTER_RANGES:
            lo, hi, label = _SCAN_FILTER_RANGES[k]
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"scan_filters.{k} 需为数值")
            if not (lo <= v <= hi):
                raise HTTPException(status_code=400, detail=f"scan_filters.{k}（{label}）需在 {lo}~{hi} 之间")
            out[k] = v
        else:
            raise HTTPException(status_code=400, detail=f"scan_filters 未知参数: {k}")
    return out


def _merge_scan_filters(existing, submitted: dict) -> dict:
    """键级合并（2026-10-13 修复）：提交键校验后覆盖，未提交键保留库内旧值。

    修复「前端只提交改动键 + PUT 整组覆盖」的静默回退——此前保过 A 键定制后，
    再改 B 键保存会整组替换、丢掉 A 键，A 回落 env（24h 成交额门槛 100万→300万
    个案）。existing 非 dict（None/脏值）按空处理。"""
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(_validate_scan_filters(submitted))
    return merged


@router.get("/config", response_model=SystemConfigOut)
def get_system_config(db: Session = Depends(get_db)):
    """获取系统配置（AI 开关 + 策略提示词 + 扫描策略）"""
    cfg = _get_system_config(db)
    return _config_response(cfg)


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
    if body.tp2_enabled is not None:
        cfg.tp2_enabled = body.tp2_enabled

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
        if not (0 <= body.max_open_trades <= 50):
            raise HTTPException(status_code=400, detail="max_open_trades 需在 0~50 之间（0=不限单数）")
        cfg.max_open_trades = body.max_open_trades
    if body.trading_min_free_pct is not None:
        if not (0 <= body.trading_min_free_pct <= 1):
            raise HTTPException(status_code=400, detail="trading_min_free_pct 需在 0~1 之间")
        cfg.trading_min_free_pct = body.trading_min_free_pct
    # 运行时开关（任务级即时生效：celery 任务每轮读 DB）。
    # 2026-10-03 删环境切换：不再有 trading_testnet 写入——自动开单由
    # 双网独立开关控制，手动开单由前端显式选账号
    # 多账号（docs/17）：current_account_key 为正式网主账号（仅前端展示/
    # 默认 scope 用），选中 live 账号即更新
    if body.current_account_key is not None:
        from app.services.account_registry import get_account
        acc = get_account(body.current_account_key)
        if acc is None:
            raise HTTPException(status_code=400, detail=f"账号 {body.current_account_key} 不在注册表中（检查服务器 .env）")
        if acc.testnet:
            raise HTTPException(status_code=400, detail="current_account_key 仅接受正式网账号")
        if cfg.current_account_key != acc.key:
            cfg.current_account_key = acc.key
    # P1 扇出（docs/17 §3）：启用自动交易的正式网账号集合（多账号并行开单）。
    # 正式网模式内加减启用账号不联动总闸（总闸是显式安全开关）；主账号不在
    # 启用集合时自动改为集合第一个
    if body.live_trading_keys is not None:
        from app.services.account_registry import get_account
        seen: list = []
        for k in body.live_trading_keys:
            acc = get_account(k)
            if acc is None:
                raise HTTPException(status_code=400, detail=f"账号 {k} 不在注册表中（检查服务器 .env）")
            if acc.testnet:
                raise HTTPException(status_code=400, detail="live_trading_keys 仅接受正式网账号")
            if acc not in seen:
                seen.append(acc)
        cfg.live_trading_keys = ",".join(a.key for a in seen)
        if seen and cfg.current_account_key not in {a.key for a in seen}:
            cfg.current_account_key = seen[0].key
    # 双网自动交易开关（2026-10-03 拆分）：独立判定、可同时开启
    if body.trading_enabled_testnet is not None:
        cfg.trading_enabled_testnet = body.trading_enabled_testnet
    if body.trading_enabled_live is not None:
        cfg.trading_enabled_live = body.trading_enabled_live
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
        # 上限按本次请求生效后的杠杆上限（2026-10-02 上页面联动）：
        # body 同传 leverage_max 时以本次新值为准（显式 null=恢复 env 默认），
        # 未传时才读 cfg 现值
        if "leverage_max" in body.model_fields_set:
            lev_cap = (
                int(body.leverage_max) if body.leverage_max is not None
                else int(settings.TRADING_LEVERAGE)
            )
        else:
            lev_cap = (
                int(cfg.leverage_max) if cfg.leverage_max is not None
                else int(settings.TRADING_LEVERAGE)
            )
        if v is not None and not (1 <= v <= lev_cap):
            raise HTTPException(
                status_code=400,
                detail=f"min_adapt_leverage 需在 1~{lev_cap} 之间（动态杠杆下限，倍数）",
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
    # 风控参数（2026-10-02 上页面）：显式传 null = 恢复 env 默认；范围越界 400
    if "min_recommendation" in body.model_fields_set:
        v = body.min_recommendation
        if v is not None and not (0 <= v <= 100):
            raise HTTPException(status_code=400, detail="min_recommendation 需在 0~100 之间（开单评分门槛）")
        cfg.min_recommendation = v
    if "daily_loss_stop_pct" in body.model_fields_set:
        v = body.daily_loss_stop_pct
        # 0=关闭（与 env 语义一致：消费侧 loss_pct>0 判定，关闭也可用开关）
        if v is not None and not (0 <= v <= 50):
            raise HTTPException(status_code=400, detail="daily_loss_stop_pct 需在 0~50 % 之间（当日亏损熔断阈值，0=关闭）")
        cfg.daily_loss_stop_pct = v
    if "daily_loss_stop_enabled" in body.model_fields_set:
        cfg.daily_loss_stop_enabled = body.daily_loss_stop_enabled
    if "ai_rr_min" in body.model_fields_set:
        v = body.ai_rr_min
        if v is not None and not (0.5 <= v <= 10):
            raise HTTPException(status_code=400, detail="ai_rr_min 需在 0.5~10 之间（盈亏比下限）")
        cfg.ai_rr_min = v
    if "ai_rr_tol" in body.model_fields_set:
        v = body.ai_rr_tol
        if v is not None and not (0 <= v <= 0.2):
            raise HTTPException(status_code=400, detail="ai_rr_tol 需在 0~0.2 之间（现价复算 RR 容差，0=严格）")
        cfg.ai_rr_tol = v
    if "risk_pct" in body.model_fields_set:
        v = body.risk_pct
        if v is not None and not (0.1 <= v <= 20):
            raise HTTPException(status_code=400, detail="risk_pct 需在 0.1~20 % 之间（单笔风险比例）")
        cfg.risk_pct = v
    if "leverage_max" in body.model_fields_set:
        v = body.leverage_max
        if v is not None and not (1 <= v <= 125):
            raise HTTPException(status_code=400, detail="leverage_max 需在 1~125 之间（杠杆上限，倍数）")
        cfg.leverage_max = v
    if "taker_fee_rate" in body.model_fields_set:
        v = body.taker_fee_rate
        if v is not None and not (0 <= v <= 0.005):
            raise HTTPException(status_code=400, detail="taker_fee_rate 需为比例值（0.0005=0.05%，≤0.5%）")
        cfg.taker_fee_rate = v
    if "near_anchor_factor" in body.model_fields_set:
        v = body.near_anchor_factor
        if v is not None and not (0 < v <= 1):
            raise HTTPException(status_code=400, detail="near_anchor_factor 需 >0 且 ≤1（近锚折价系数，1=关闭折价）")
        cfg.near_anchor_factor = v
    if "portfolio_heat_cap" in body.model_fields_set:
        v = body.portfolio_heat_cap
        if v is not None and not (0 <= v <= 1):
            raise HTTPException(status_code=400, detail="portfolio_heat_cap 需为比例值（0.3=30% 钱包，0=关闭）")
        cfg.portfolio_heat_cap = v
    if "same_direction_cap" in body.model_fields_set:
        v = body.same_direction_cap
        if v is not None and not (0 <= v <= 20):
            raise HTTPException(status_code=400, detail="same_direction_cap 需在 0~20 之间（同向上限，0=不限）")
        cfg.same_direction_cap = v
    if "btc_crash_pause_pct" in body.model_fields_set:
        v = body.btc_crash_pause_pct
        if v is not None and not (0 <= v <= 20):
            raise HTTPException(status_code=400, detail="btc_crash_pause_pct 需在 0~20 % 之间（BTC 急跌警示，0=关闭）")
        cfg.btc_crash_pause_pct = v
    # 周/月回撤分层熔断 + ATR 跟进（2026-10-04，docs/13 #2/#5；传 null 恢复 env 默认）
    if "weekly_dd_stop_pct" in body.model_fields_set:
        v = body.weekly_dd_stop_pct
        if v is not None and not (0 <= v <= 100):
            raise HTTPException(status_code=400, detail="weekly_dd_stop_pct 需在 0~100 % 之间（周回撤减速阈值，0=关闭）")
        cfg.weekly_dd_stop_pct = v
    if "monthly_dd_stop_pct" in body.model_fields_set:
        v = body.monthly_dd_stop_pct
        if v is not None and not (0 <= v <= 100):
            raise HTTPException(status_code=400, detail="monthly_dd_stop_pct 需在 0~100 % 之间（月回撤熔断阈值，0=关闭）")
        cfg.monthly_dd_stop_pct = v
    if "atr_trail_k" in body.model_fields_set:
        v = body.atr_trail_k
        if v is not None and not (0 <= v <= 20):
            raise HTTPException(status_code=400, detail="atr_trail_k 需在 0~20 之间（ATR 跟进倍数，0=关闭 ATR 腿）")
        cfg.atr_trail_k = v
    # TP 分批比例（2026-10-09；% 值，传 null 恢复 env 默认；合计 ≤95 保证跟进腿 ≥5%）
    if "tp1_qty_pct_with_tp2" in body.model_fields_set:
        v = body.tp1_qty_pct_with_tp2
        if v is not None and not (0 < v < 100):
            raise HTTPException(status_code=400, detail="tp1_qty_pct_with_tp2 需在 0~100 % 之间（有 TP2 时 TP1 分批比例）")
        cfg.tp1_qty_pct_with_tp2 = v
    if "tp2_qty_pct" in body.model_fields_set:
        v = body.tp2_qty_pct
        if v is not None and not (0 < v < 100):
            raise HTTPException(status_code=400, detail="tp2_qty_pct 需在 0~100 % 之间（TP2 分批比例）")
        cfg.tp2_qty_pct = v
    if "tp1_qty_pct_no_tp2" in body.model_fields_set:
        v = body.tp1_qty_pct_no_tp2
        if v is not None and not (0 < v < 100):
            raise HTTPException(status_code=400, detail="tp1_qty_pct_no_tp2 需在 0~100 % 之间（无 TP2 时 TP1 一次止盈比例）")
        cfg.tp1_qty_pct_no_tp2 = v
    _eff_tp1 = float(cfg.tp1_qty_pct_with_tp2) if cfg.tp1_qty_pct_with_tp2 is not None \
        else float(settings.TP1_QTY_PCT_WITH_TP2)
    _eff_tp2 = float(cfg.tp2_qty_pct) if cfg.tp2_qty_pct is not None \
        else float(settings.TP2_QTY_PCT)
    _eff_tp1s = float(cfg.tp1_qty_pct_no_tp2) if cfg.tp1_qty_pct_no_tp2 is not None \
        else float(settings.TP1_QTY_PCT_NO_TP2)
    if _eff_tp1 + _eff_tp2 > 95:
        raise HTTPException(status_code=400, detail="有 TP2 时 TP1+TP2 分批合计需 ≤95%（剩余 ≥5% 由跟进止损退出）")
    if _eff_tp1s > 95:
        raise HTTPException(status_code=400, detail="无 TP2 时 TP1 比例需 ≤95%（剩余 ≥5% 由跟进止损退出）")
    # 止损金额模式（2026-09-27）：pct=风险基数分档×3%（默认）/ fixed=固定 USDT 金额
    if "risk_budget_mode" in body.model_fields_set:
        v = body.risk_budget_mode
        if v not in ("pct", "fixed"):
            raise HTTPException(status_code=400, detail="risk_budget_mode 仅支持 pct / fixed")
        cfg.risk_budget_mode = v
    if "risk_fixed_amount" in body.model_fields_set:
        v = body.risk_fixed_amount
        if v is not None and not (0.1 <= v <= 1000):
            raise HTTPException(status_code=400, detail="risk_fixed_amount 需在 0.1~1000 USDT 之间")
        cfg.risk_fixed_amount = v if v is not None else 3.0
    # 初始资金（2026-09-26）：≥0 USDT，资金划转面板展示口径
    if "live_initial_capital" in body.model_fields_set:
        v = body.live_initial_capital
        if v is not None and v < 0:
            raise HTTPException(status_code=400, detail="live_initial_capital 需 ≥ 0")
        cfg.live_initial_capital = v if v is not None else 0

    # 筛选参数组（2026-09-28，2026-10-13 改键级合并）：提交键校验后覆盖库内旧值、
    # 未提交键保留（前端只提交改动键，整组覆盖会静默抹掉先前定制键、回落 env）；
    # 显式传 null=清除全部定制恢复 env/代码默认
    if "scan_filters" in body.model_fields_set:
        cfg.scan_filters = (
            _merge_scan_filters(cfg.scan_filters, body.scan_filters)
            if body.scan_filters is not None else None
        )

    db.commit()
    db.refresh(cfg)
    return _config_response(cfg)


@router.post("/config/monthly-breaker-ack")
def ack_monthly_breaker(db: Session = Depends(get_db)):
    """月度回撤熔断人工复盘确认（docs/13 #2）：写确认时刻，恢复开仓。

    确认语义 = 「人工已复盘本月回撤成因，知悉并接受继续交易」——确认时刻在
    当前 UTC 月内即视为本月已确认（放行至月底），跨月自动重新武装。
    恶性场景下重复确认只是延迟干预，不构成额外风险敞口（预算闸门仍在）。
    """
    cfg = _get_system_config(db)
    cfg.monthly_breaker_ack_at = datetime.utcnow()
    db.commit()
    db.refresh(cfg)
    return {"ok": True, "ack_at": cfg.monthly_breaker_ack_at}


@router.post("/config/weekly-breaker-ack")
def ack_weekly_breaker(db: Session = Depends(get_db)):
    """周度回撤减速人工复盘确认（2026-10-07，对称月熔断）：写确认时刻，
    本周剩余时间在跑上限/同向上限恢复常规（软减速不拦开仓，确认只恢复上限）。

    确认时刻在当前 UTC 周内即视为本周已确认，下周一 0 点自动重新武装。
    """
    cfg = _get_system_config(db)
    cfg.weekly_breaker_ack_at = datetime.utcnow()
    db.commit()
    db.refresh(cfg)
    return {"ok": True, "ack_at": cfg.weekly_breaker_ack_at}


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
    """AI 建议复盘统计：整体 + 按触发器（原始 signal_type）分组胜率 + 期望值(R)

    分组口径（2026-09-25 拆细）：
    - 按原始 signal_type 分组，不再合并为家族：trigger_pullback_confirm（回踩金K确认，
      两段式）单独成组，与旧一段式 trigger_pullback（同向回踩）、旧管线 uptrend_pullback
      （上涨回调）隔离，便于评估新触发器真实胜率
    - TRIGGER_FAMILY 键集（与实盘归因共用同源常量）作为触发器白名单；旧管线结构分类
      （uptrend/downtrend/trend_reversal/range_bound 等）非触发器，与 NULL/未识别统一归「未记录」
    - 位置/均线维度移除：位置仅镜像方向（支撑=多/压力=空），均线碎片化样本过小
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    all_rows = db.execute(
        select(TradeReview).where(TradeReview.created_at >= cutoff)
    ).scalars().all()
    # veto 假想回放行（docs/24 A5a）不混入主统计——"建议质量口径"只反映
    # 通过单；被否决建议的假想表现单列 veto_stats 对比
    rows = [r for r in all_rows if not getattr(r, "is_veto", False)]

    # 期望值口径（R 倍数）：loss=-1R、win_tp1=RR1、win_tp2=RR2（简化：不分批），
    # 减去费用假设——taker 往返 0.1% 名义 + 资金费 0.03%/8h（按持仓 K 线数折算），
    # 费用换算成 R：cost_pct / stop_pct。无对应 AI 三价（手动分析/数据缺失）的行不计
    # （ai_map 按 all_rows 构建：veto 行的分析行也要能查到 veto_original）
    ai_map: dict = {}
    ai_ids = [r.ai_analysis_id for r in all_rows if r.ai_analysis_id]
    if ai_ids:
        for a in db.execute(
            select(AIAnalysis).where(AIAnalysis.id.in_(ai_ids))
        ).scalars().all():
            ai_map[str(a.id)] = a

    def _prices_of(a: AIAnalysis, rev: TradeReview) -> Optional[tuple]:
        """回放三价来源：veto 否决单从 veto_original 快照取（自身三价已被
        forced skip 清空），suggest 单取分析行自身字段（docs/24 A5a）"""
        if getattr(rev, "is_veto", False) and a.veto_original:
            v = a.veto_original
            return (
                v.get("direction"),
                float(v.get("entry_price") or 0),
                float(v.get("stop_loss") or 0),
                float(v.get("take_profit_1") or 0),
                float(v.get("take_profit_2") or 0),
            )
        if not a.entry_price or not a.stop_loss or not a.take_profit_1:
            return None
        return (
            a.direction,
            float(a.entry_price),
            float(a.stop_loss),
            float(a.take_profit_1),
            float(a.take_profit_2) if a.take_profit_2 else 0.0,
        )

    def _ev_r(rev: TradeReview) -> Optional[float]:
        a = ai_map.get(str(rev.ai_analysis_id)) if rev.ai_analysis_id else None
        if not a:
            return None
        prices = _prices_of(a, rev)
        if not prices:
            return None
        direction, entry, stop, tp1, tp2 = prices
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or entry <= 0:
            return None
        if rev.outcome == "loss":
            r_mult = -1.0
        elif rev.outcome == "win_tp1":
            r_mult = abs(tp1 - entry) / stop_dist
        elif rev.outcome == "win_tp2" and tp2:
            r_mult = abs(tp2 - entry) / stop_dist
        else:
            return None
        cost_pct = 0.001 + 0.0003 * max(1, (rev.bars_to_exit or 1) // 8)  # 往返手续费 + 资金费(0.03%/8h)
        return round(r_mult - cost_pct / (stop_dist / entry), 3)

    for rev in all_rows:
        rev.ev = _ev_r(rev)  # 非映射属性，分组聚合用（veto 行也要算，供 veto 组聚合）

    def _agg_ev(items: list) -> Optional[float]:
        vals = [r.ev for r in items if getattr(r, "ev", None) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    resp = ReviewStatsResponse(
        days=days, **_agg_stats(rows), ev_r=_agg_ev(rows), groups=[],
        # 口径标注（docs/24 A2）：四态口径含"触 TP1 后又止损仍计胜"的美化项，
        # 仅作建议质量参考；考核主指标 = 实盘净胜率（/review/actual，pnl>0/闭仓数）
        win_rate_note=(
            "建议质量口径：win_tp1（触TP1后又止损仍计胜）计入胜率、expired 不计分母；"
            "考核以实盘净胜率为准（/review/actual）"
        ),
    )

    # 触发器分组：按原始 signal_type 细分（2026-09-25 拆细，见 docstring）。
    # TRIGGER_FAMILY 键集与实盘归因共用同源常量，作为触发器白名单防两处漂移；
    # 2026-09-28 docs/22 触点体系：组键扩为 (signal_type, touch_ref)——同触发类型
    # 不同触点位的回放行为分开统计（无触点行 touch_ref=None 单独成组）
    from app.services.actual_review import TRIGGER_FAMILY

    buckets: dict[tuple[str, Optional[str]], list[TradeReview]] = {}
    for r in rows:
        raw = r.signal_type or ""
        key = (raw if raw in TRIGGER_FAMILY else "未记录", getattr(r, "touch_ref", None))
        buckets.setdefault(key, []).append(r)
    # 组内样本多的排前面，便于聚焦有效分组（touch_ref 可能为 None，排序转字符串）
    for (sig, tref), items in sorted(
        buckets.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1] or ""),
    ):
        agg = _agg_stats(items)
        resp.groups.append(
            ReviewGroupStats(signal_type=sig, touch_ref=tref, **agg, ev_r=_agg_ev(items))
        )

    # 规则版本分组（docs/24 A0）：同版本内对比，legacy=标记上线前旧行；
    # 历史跨版本混合成绩不具证明力（docs/24 修订 v2），前端按版本隔离展示
    rv_buckets: dict[str, list[TradeReview]] = {}
    for r in rows:
        a = ai_map.get(str(r.ai_analysis_id)) if r.ai_analysis_id else None
        rv_buckets.setdefault((a.rule_version if a else None) or "legacy", []).append(r)
    for rv, items in sorted(rv_buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        resp.rule_version_groups.append(
            ReviewGroupStats(rule_version=rv, **_agg_stats(items), ev_r=_agg_ev(items))
        )

    # 双评委 veto 闭环统计（docs/24 A5a）：is_veto=True 的回放行是"被否决建议的
    # 假想表现"，与整体统计（=通过单，veto 行不混入）对比——若 veto 组假想
    # 胜率/ev_r 反而更高，说明评委在错杀；一致或更低说明 veto 有正贡献
    veto_rows = [r for r in all_rows if getattr(r, "is_veto", False)]
    if veto_rows:
        resp.veto_stats = ReviewGroupStats(
            is_veto=True, **_agg_stats(veto_rows), ev_r=_agg_ev(veto_rows)
        )
        passed_rows = [r for r in rows if not getattr(r, "is_veto", False)]
        passed_agg = _agg_stats(passed_rows)
        v_agg = resp.veto_stats
        resp.veto_note = (
            f"veto 假想回放 {v_agg.total} 笔（win_rate {v_agg.win_rate:.0%}）vs "
            f"通过单 {passed_agg['total']} 笔（win_rate {passed_agg['win_rate']:.0%}）；"
            "veto 组假想胜率明显更高 = 评委在错杀好单，需审视裁决质量"
        )
    return resp


@router.get("/review/funnel", response_model=ReviewFunnelResponse)
def review_funnel(
    days: int = Query(30, ge=1, le=180),
    scope: str = Query("all", description="live/testnet/all（docs/17）"),
    db: Session = Depends(get_db),
):
    """全漏斗转化率（docs/24 A5c）：信号命中 → AI 分析 → suggest → 过评委 →
    开仓 → 盈利，逐级转化率定位"价值在哪一级流失"。

    口径：
    - 信号命中 = 窗口内 scan_results（扫描落库的信号行）
    - AI 分析 = 命中信号产生 ai_analyses（latest-wins，1:1 挂信号）
    - suggest = trade_decision=suggest（veto 否决单已转 skip，不计入后续各级）
    - 过评委 = suggest 数（veto 在 suggest 之后发生，veto 单同级流失另列 note）
    - 开仓 = 有非 FAILED trade_records 的 suggest；被闸门拦 = open_block_reason 非空
    - 盈利 = 已闭仓且 realized_pnl > 0
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    sig_cnt = db.execute(
        select(func.count()).select_from(ScanResult).where(ScanResult.created_at >= cutoff)
    ).scalar() or 0
    ana_cnt = db.execute(
        select(func.count()).select_from(AIAnalysis).where(AIAnalysis.created_at >= cutoff)
    ).scalar() or 0
    suggest_rows = db.execute(
        select(AIAnalysis).where(
            AIAnalysis.created_at >= cutoff,
            AIAnalysis.trade_decision == "suggest",
        )
    ).scalars().all()
    suggest_cnt = len(suggest_rows)
    veto_cnt = db.execute(
        select(func.count()).select_from(AIAnalysis).where(
            AIAnalysis.created_at >= cutoff,
            AIAnalysis.trade_decision == "skip",
            AIAnalysis.veto_original.isnot(None),
        )
    ).scalar() or 0
    blocked_cnt = db.execute(
        select(func.count()).select_from(AIAnalysis).where(
            AIAnalysis.created_at >= cutoff,
            AIAnalysis.trade_decision == "suggest",
            AIAnalysis.open_block_reason.isnot(None),
        )
    ).scalar() or 0

    # 开仓：suggest 关联的 trade_records（排除 FAILED 下单失败行）；
    # scope 过滤（docs/17）只作用于有账号维度的开仓/盈利两级——信号/AI/suggest
    # 是全局扫描产物，无账号归属，强过滤会让前四级在单账号视图下恒为全局
    opened_cnt = 0
    profit_cnt = 0
    if suggest_rows:
        ana_ids = [a.id for a in suggest_rows]
        tfilter = [TradeRecord.ai_analysis_id.in_(ana_ids), TradeRecord.status != "FAILED"]
        from app.api.trades import _account_scope_filter

        extra = _account_scope_filter(TradeRecord, scope)
        if extra is not None:
            tfilter.append(extra)
        trows = db.execute(
            select(TradeRecord.status, TradeRecord.realized_pnl).where(*tfilter)
        ).all()
        opened_cnt = len(trows)
        profit_cnt = sum(
            1 for s, p in trows if s == "CLOSED" and (p or 0) > 0
        )

    def _stage(name: str, cnt: int, prev: Optional[int], note: str = "") -> FunnelStage:
        conv = round(cnt / prev, 4) if prev else None
        return FunnelStage(stage=name, count=cnt, conversion=conv, note=note)

    stages = [
        _stage("信号命中", sig_cnt, None, "扫描落库的信号行"),
        _stage("AI 分析", ana_cnt, sig_cnt, "命中信号产生 AI 分析结论"),
        _stage("suggest", suggest_cnt, ana_cnt, "AI 建议开单"),
        _stage("过评委", suggest_cnt, suggest_cnt,
               f"双评委 veto {veto_cnt} 笔（在 suggest 之后否决，假想表现见 /review/stats）"),
        _stage("开仓", opened_cnt, suggest_cnt,
               f"闸门拦截 {blocked_cnt} 笔，其余为锁等待/执行异常/待回踩"),
        _stage("盈利", profit_cnt, opened_cnt, "已闭仓且 realized_pnl > 0"),
    ]

    # 闸门拦截原因 top3（只统计窗口内 suggest 的 open_block_reason）
    reason_map: dict[str, int] = {}
    for a in suggest_rows:
        if a.open_block_reason:
            reason_map[a.open_block_reason] = reason_map.get(a.open_block_reason, 0) + 1
    block_top = [
        {"reason": k, "count": v}
        for k, v in sorted(reason_map.items(), key=lambda kv: -kv[1])[:3]
    ]

    return ReviewFunnelResponse(
        days=days, scope=scope, stages=stages, block_reasons_top=block_top,
    )


@router.get("/review/actual", response_model=ActualReviewResponse)
def review_actual(
    days: int = Query(30, ge=1, le=180),
    scope: str = Query("all", description="live/testnet/all 或账号 key（docs/17）"),
    db: Session = Depends(get_db),
):
    """实盘归因统计（复盘主轴）：trade_records 总览 + 分组 + 逐笔计划vs实际 + AI 错误标签"""
    from app.api.trades import _account_scope_filter
    from app.services.actual_review import backfill_costs, build_actual_review
    from app.services.binance_trader import BinanceTrader

    try:
        # 存量平仓单费用一次性回补：双网各补各的（2026-10-03 删环境切换，
        # 与定时归因任务同口径）；失败不阻塞统计（费用列显示 None）
        for testnet in (True, False):
            bt = BinanceTrader(testnet)
            if bt.configured:
                backfill_costs(db, bt, limit=20)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("费用回补失败: %s", e)

    return build_actual_review(db, days, extra_filter=_account_scope_filter(TradeRecord, scope))


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
    # 不按当前环境过滤（2026-10-03 双网同开对齐定时任务：归因队列跨双网），
    # 防重由 attributed_at IS NULL（批量）+ 同源镜像单去重保证
    q = (
        select(TradeRecord)
        .where(
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_reason.isnot(None),
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

    try:
        # 双网费用回补（2026-10-03 删环境切换，与定时归因任务同口径）
        for testnet in (True, False):
            bt = BinanceTrader(testnet)
            if bt.configured:
                backfill_costs(db, bt, limit=5)
    except Exception:
        pass

    pool = ExchangePool()
    try:
        results = run_ai_attribution(db, pool, None, trades)
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
    """手动触发一次补丁草稿生成（立即调 LLM，不等每日定时）；
    未生成时返回 skips 明细（卡在样本/热度/已有草稿/冷却哪一关）"""
    from app.services.patch_service import run_trigger
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="AI 未配置（AI_API_KEY 为空）")
    result = run_trigger(db)
    return {"created": len(result["drafts"]), "skips": result["skips"],
            "attributed_total_7d": result["attributed_total"],
            "drafts": [
                {"id": d.id, "tag": d.target_tag, "kind": d.kind} for d in result["drafts"]
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


# ===== 回踩两段式待确认池（docs/19）=====

@router.get("/pullback-pendings")
def pullback_pendings_list(
    status: str = Query("all", pattern="^(all|pending|confirmed|expired|superseded)$"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """回踩两段式待确认池（docs/19）：Stage-1 触碰后等金K确认的 pending 及近期终态行。

    status=pending 只看活跃等待中；其余值按状态过滤；all 全量按 updated_at 倒序。
    """
    q = select(PullbackPending).order_by(desc(PullbackPending.updated_at)).limit(limit)
    if status != "all":
        q = q.where(PullbackPending.status == status)
    rows = db.execute(q).scalars().all()
    return [{
        "id": str(r.id),
        "symbol": r.symbol,
        "direction": r.direction,  # 1 多 / -1 空
        "touch_ref": r.touch_ref,  # Stage-1 命中触点（docs/22，前端「等待确认」列展示；旧行 NULL）
        "stage1_candle_open": r.stage1_candle_open.isoformat() if r.stage1_candle_open else None,
        "ema21": float(r.ema21) if r.ema21 is not None else None,
        "quality_score": r.quality_score,
        "votes_json": r.votes_json,
        "status": r.status,  # pending / confirmed / expired / superseded
        # 卡住环节（2026-10-10）：仅 pending/confirmed 之外的终态算被卡住，值=死因
        # （ranging/gate_4h/structure_1h/range_score/window_expired/direction_flip/breakout/same_candle_confirm）
        "blocked_by": r.resolve_reason if r.status not in ("pending", "confirmed") else None,
        "resolve_reason": r.resolve_reason,
        "confirm_pattern": r.confirm_pattern,
        "confirmed_scan_result_id": str(r.confirmed_scan_result_id) if r.confirmed_scan_result_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
    } for r in rows]


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
        "origin": r.origin,  # manual / auto（冷却档）/ stop_hard（≥1.5R 永久档）
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
    interval: str = Query(
        None, pattern="^(1h|4h)$",
        description="K线周期（1h 默认 / 4h）；缺省用系统配置周期",
    ),
    force_refresh: bool = Query(False, description="跳过缓存直接拉取交易所最新数据并重置缓存"),
    db: Session = Depends(get_db),
):
    """获取指定币种的最新 K 线数据（用于前端展示）

    多交易所故障转移（币安→欧易→Bitget）；全部失败时降级返回旧缓存，
    仍失败则 503，detail 含各交易所失败原因与解禁时长。
    force_refresh=True 时跳过缓存直连交易所（图表"更新"按钮），结果回写缓存。
    interval 支持图表周期切换（2026-09-28：1h 默认，可切 4h）。
    """
    cfg = _get_system_config(db)
    eff_interval = interval or cfg.kline_interval
    pool = ExchangePool()
    # 扫描尾窗同口径 + 强刷（H5，2026-10-12）：K线缓存按小时桶且桶内不刷新，
    # 尾窗内未收盘根随时在变——不刷会停在小时内首次取数的快照上（最坏差近 1h），
    # 与扫描/AI 看到的最新快照不一致。尾窗内强制强刷（客户端「更新」按钮参数同级），
    # 非尾窗沿用缓存（省请求）
    eff_force = force_refresh or md.in_forming_tail(
        eff_interval, datetime.now(timezone.utc)
    )
    try:
        klines = pool.get_klines(
            symbol, eff_interval, limit, allow_stale=True, force_refresh=eff_force
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
    # 近期摆动点（图表标注，高低各 20）——与 AI 事实包同参对齐（2026-10-11
    # 拍板由 5 放宽 20：看图可核对 AI 报的结构位；同源 helper 单一事实源）
    swings = swing_annotations(klines, order=cfg.swing_order, n=20)
    return {
        "symbol": symbol,
        "interval": eff_interval,
        "klines": result,
        "swings": swings,
    }


# ===== 手动搜索 AI 分析 =====

@router.post("/analyze", response_model=ManualAnalysisOut)
def analyze_symbol(body: ManualAnalyzeRequest, db: Session = Depends(get_db)):
    """手动分析任意币种（搜索币种页签）：拉取 K 线后同步调用 AI，返回结构化建议"""
    cfg = _get_system_config(db)
    # 手动分析不受「筛选后自动 AI 分析」开关限制，仅要求已配置 API_KEY
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=403, detail="未配置 AI API Key，无法分析")

    symbol = normalize_symbol(body.symbol)
    pool = ExchangePool()
    # 扫描尾窗/竞态同口径（2026-10-11-4；2026-10-13 扩整点竞态）：手动分析的事实包
    # 与定时扫描一致——尾窗内强刷（缓存可能是小时内早期快照）+ 幽灵尾根视图
    # （尾窗内近收盘根，或整点竞态下末根实为已收盘根，均视作信号根）
    # 基准时刻取一次（2026-10-12）：判定与 screen_klines 同一 now——强刷是网络
    # 请求（秒级），重读时钟会让 :59:5x 的判定在跨点后失效、基准不一致
    _tail_now = datetime.now(timezone.utc)
    _tail = md.in_forming_tail(cfg.kline_interval, _tail_now)
    try:
        klines = pool.get_klines(
            symbol, cfg.kline_interval, 500, force_refresh=_tail,
        )
        klines = md.screen_klines(klines, cfg.kline_interval, _tail_now)
    except AllExchangesFailed as e:
        raise HTTPException(
            status_code=503,
            detail=f"K线获取失败，无法分析（{e.summary}）",
        )

    if len(klines) < 2:
        raise HTTPException(status_code=400, detail=f"币种 {symbol} K线数据不足")

    # 4h 高周期 K 线（大方向背景注入）；失败只降级为无 4h 行，不中断分析
    try:
        htf_klines = pool.get_klines(symbol, "4h", 300)  # 300 根：EMA144 收敛残留 ~12%（docs/17 §6）
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
    # 战法库（2026-10-02）：装载活动战法注入判定块 + 白名单闸门。
    # 2026-10-03 删环境切换：None = 全部活动战法（同一分析双网各开一单）
    from app.services import tactics_match
    _tactics = tactics_match.load_active_tactics(None)
    tactics_block = tactics_match.render_tactics_block(_tactics)
    signal["tactic_names"] = tactics_match.allowed_names(_tactics)
    try:
        from app.services.risk_budget import resolve_risk_budget
        from app.services.scan_filters import resolve_scan_filters
        _rm, _ra, _rl = resolve_risk_budget(cfg)
        _stop_cap = float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None
        ai_result = analyze_coin(
            signal, klines, strategy_prompt=strategy_prompt, htf_klines=htf_klines,
            risk_mode=_rm, risk_amount=_ra, risk_label=_rl, stop_cap_pct=_stop_cap,
            interval=cfg.kline_interval, tactics_block=tactics_block,
            tp2_enabled=bool(cfg.tp2_enabled),
            # 筛选参数组（2026-10-12）：门槛块阈值/触点启用列表读页面配置生效值
            flt=resolve_scan_filters(cfg),
        )
        # 4h 状态机硬约束兜底（2026-10-11 再重构 EMA 状态机 11 态：缠绕/拐头双向禁开，宏观交叉/恢复/排列锁逆势，过渡放行）
        ai_result = enforce_htf_constraint(ai_result, htf_klines)
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
        tactic_name=ai_result.get("tactic_name"),
        tactic_reason=ai_result.get("tactic_reason"),
        pullback_wait=is_pullback_wait(ai_result, signal.get("current_price")),
        created_at=datetime.utcnow(),
    )
