"""AI 分析 Celery 任务（P0 管线，docs/04）

- 批量：Celery group 按币种并行分发（原串行 + sleep(1) 已废弃）
- 并发：Redis zset 信号量限制 LLM 全局并发（LLM_CONCURRENCY，默认 3）
- Stage 1 闸门：
    1) 1h 内同指纹 → 沿用旧结论（价格区仍有效时）
- 程序侧 skip 闸门已全部移除（2026-09-15 拍板）：选中的币种必须经过真实 AI 分析，
  跳过与否由 AI 自己决定；重复命中由扫描侧抑制（scanner，同币同信号窗口内不落库）
- Stage 2：analyze_with_guard（校验回炉）→ 落库
"""
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from celery import chord, group
from redis import Redis
from sqlalchemy import func, select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanRecord, ScanResult, AIAnalysis, TradeReview
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services import ai_progress
from app.services import market_data as md
from app.services.ai_analyzer import analyze_with_guard
from app.services.ai_pause import is_ai_paused, pause_window_label
from app.services.scan_filters import resolve_scan_filters
from app.services.llm_client import configure_from, current_concurrency, resolve_credentials
from app.services.strategy.technical import annotate_htf_phase, htf_context
from app.services.strategy.indicators import volume_ratio
from app.services.exchange_pool import ExchangePool
from app.services.risk_guard import is_pullback_wait
from app.services.strategy import recent_swings
from app.services.account_registry import auto_trade_accounts
from app.services.decision_version import current_decision_version
from app.services.trade_engine import _consecutive_losses_today

logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None
_LLM_SEM_KEY = "sem:llm"
# 持有者过期清理阈值（ms）：大于单次分析最长耗时；崩溃泄漏最多 10 分钟
_SEM_STALE_MS = 600_000


def _get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _sem_acquire(timeout_s: int = 900) -> str:
    """获取 LLM 并发信号量，返回持有者 id（空串表示超时降级放行）"""
    r = _get_redis()
    ident = uuid4().hex
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        now_ms = int(time.time() * 1000)
        r.zremrangebyscore(_LLM_SEM_KEY, 0, now_ms - _SEM_STALE_MS)
        r.zadd(_LLM_SEM_KEY, {ident: now_ms})
        rank = r.zrank(_LLM_SEM_KEY, ident)
        if rank is not None and rank < current_concurrency():
            return ident
        r.zrem(_LLM_SEM_KEY, ident)
        time.sleep(1.0)
    logger.warning("LLM 信号量等待超时（%ds），降级放行", timeout_s)
    return ""


def _sem_release(ident: str) -> None:
    if ident:
        try:
            _get_redis().zrem(_LLM_SEM_KEY, ident)
        except Exception:
            pass


def compute_fingerprint(signal: dict, context: Optional[dict] = None) -> str:
    """信号指纹：价格 0.5% 对数分桶 + 信号要素。同指纹在 TTL 内可复用结论"""
    close = float(signal.get("current_price") or 0)
    bucket = round(math.log(close) / 0.005) if close > 0 else 0
    raw = "|".join([
        signal.get("symbol", ""),
        signal.get("signal_type", ""),
        signal.get("position") or "",
        signal.get("pattern") or "",
        signal.get("touch_ref") or "",  # 触点进指纹（docs/22）：同价位桶不同触点不复用结论
        str(bucket),
        settings.RULE_VERSION,
        json.dumps(context or {}, sort_keys=True, separators=(",", ":"), default=str),
    ])
    return hashlib.sha1(raw.encode()).hexdigest()[:40]


def build_cache_context(cfg, klines: list, htf_klines: list,
                        tactics_block: str, consecutive_losses: Optional[int]) -> dict:
    """绑定已收盘事实与运行配置；未收盘根只通过现价有效性检查参与。"""
    def digest(value) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    return {
        "contract": 2,
        # 统一决策版本（T5）：补丁/配置/提示词变更递增，跨版本不命中缓存
        "decision_version": int(cfg.decision_version or 0),
        "config": digest({col.key: getattr(cfg, col.key)
                          for col in SystemConfig.__table__.columns}),
        "closed_1h": digest(klines[:-1]),
        "closed_4h": digest(htf_klines[:-1]),
        "tactics": digest(tactics_block),
        "losses": consecutive_losses,
        "scan_filters": digest(asdict(resolve_scan_filters(cfg))),
        "defaults": digest({key: value for key, value in settings.model_dump().items()
                            if key.startswith(("AI_", "TRADING_", "RISK_", "TP1_", "TP2_", "SL_"))
                            and not any(part in key for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}),
    }


def _filter_ai_targets(db, results):
    """批量 AI 分析前的两道过滤（docs/16 §5.2 P0-2、§5.3 P0-3）。

    1) 不可交易合约（P0-2）：正式网与测试网都不在 exchangeInfo 里 → 分析结果注定
       开不了单（14 天 82 次「下单环境未上架该合约」，占 suggest 8%）。任一环境有
       上架就保留（用户可能手动在另一环境开）。exchangeInfo 拉取失败返回空集合，
       此时不做任何过滤（放行不阻断扫描链）。
    2) 单币冷却（P0-3）：同币当日（UTC）已被 AI skip 达 N 次、且历史推荐分均值 < M
       → 本轮不再送 AI，把配额让给新命中。依据：MAGMAUSDT 14 天被分析 14 次、
       skip 10 次、均分 34。N=0 关闭。

    只作用于批量自动分析；手动单币重分析由调用方绕过本函数。
    返回 (kept, unlisted_n, cooldown_n)。
    """
    import datetime as _dt

    kept = list(results)

    # 1) 不可交易合约
    unlisted_ids: set = set()
    try:
        from app.services.binance_trader import BinanceTrader

        listed: set[str] = set()
        for is_test in (False, True):
            try:
                listed |= BinanceTrader(is_test).listed_symbols() or set()
            except Exception as e:  # 单环境失败不影响另一环境的上架判定
                logger.warning("listed_symbols(testnet=%s) 失败，跳过该环境判定: %s",
                               is_test, e)
        if listed:
            for r in kept:
                if r.symbol not in listed:
                    unlisted_ids.add(r.id)
            if unlisted_ids:
                names = sorted({r.symbol for r in kept if r.id in unlisted_ids})
                logger.info("未上架合约跳过 AI 分析 %d 个：%s",
                            len(unlisted_ids), ",".join(names))
                kept = [r for r in kept if r.id not in unlisted_ids]
    except Exception as e:
        logger.warning("不可交易合约过滤失败（跳过该过滤）: %s", e)

    # 2) 单币冷却（DB 判定，天然持久，不引 Redis）
    cooled_ids: set = set()
    skip_limit = int(getattr(settings, "AI_REANALYZE_SKIP_LIMIT", 0) or 0)
    min_rec = float(getattr(settings, "AI_REANALYZE_MIN_REC", 0.0) or 0.0)
    if skip_limit > 0 and kept:
        try:
            day_start = _dt.datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0)
            rows = db.execute(
                select(
                    AIAnalysis.symbol,
                    func.count().filter(AIAnalysis.trade_decision == "skip"),
                    func.avg(AIAnalysis.recommendation),
                ).where(
                    AIAnalysis.created_at >= day_start,
                    AIAnalysis.symbol.in_(sorted({r.symbol for r in kept})),
                ).group_by(AIAnalysis.symbol)
            ).all()
            stats = {sym: (int(sk or 0), float(avg or 0.0)) for sym, sk, avg in rows}
            for r in kept:
                sk, avg = stats.get(r.symbol, (0, 0.0))
                if sk >= skip_limit and avg < min_rec:
                    cooled_ids.add(r.id)
            if cooled_ids:
                names = sorted({r.symbol for r in kept if r.id in cooled_ids})
                logger.info("单币冷却跳过 AI 分析 %d 个（今日 skip≥%d 且均分<%.0f）：%s",
                            len(cooled_ids), skip_limit, min_rec, ",".join(names))
                kept = [r for r in kept if r.id not in cooled_ids]
        except Exception as e:
            logger.warning("单币冷却查询失败（跳过该过滤）: %s", e)

    return kept, len(unlisted_ids), len(cooled_ids)


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_task", bind=True, max_retries=3)
def run_ai_analysis_task(
    self, scan_record_id: str, only_scan_result_id: Optional[str] = None,
    user_input: Optional[str] = None,
):
    """对指定扫描记录的命中币种执行 AI 分析（并行分发）。

    scan_record_id: 扫描记录 ID
    only_scan_result_id: 若提供，只分析该单个 ScanResult（用于手动重新分析，强制跳过缓存短路）
    user_input: 用户补充说明（单币分析时随请求传入），附加到 AI 提示词
    """
    db = SessionLocal()
    # 任务记录只在分发层落（2026-10-05 补缺口 3）：单币子任务若逐个落条目，
    # 每轮扫描最多配额个 × 每小时一轮会刷爆任务列表——子任务失败已有系统日志
    # + 进度事件（ai_progress）双通道可见
    from app.services.task_log import close_task, open_task
    task_id = open_task(
        "ai", "AI 分析分发",
        trigger="manual" if only_scan_result_id else "system",
    )
    try:
        cfg = db.get(SystemConfig, 1)
        # cfg 缺失（老库未初始化）不得继续：下游大量读取 cfg 字段会 AttributeError。
        # 2026-10-26 凭证改判 resolve_credentials 后，env 有 Key 时此分支不再必然
        # 提前返回，故显式保留空配置守卫。
        if not cfg or not resolve_credentials(cfg)[0]:
            logger.warning("AI 分析未启用（系统配置缺失或未配置 API Key），跳过")
            close_task(task_id, "skipped", summary="AI 未启用（系统配置缺失或未配置 API Key）")
            return
        if not cfg.ai_analysis_enabled and only_scan_result_id is None:
            # 「筛选后自动 AI 分析」开关（2026-09-28 与交易开关解耦）：仅拦截
            # 扫描链路的批量自动分析；手动单币重分析（only_scan_result_id 非空）
            # 不受限。交易开关（trading_enabled）只管自动成交，不再拦分析
            logger.info("筛选后自动 AI 分析开关关闭，批量分析跳过 #%s", scan_record_id)
            close_task(task_id, "skipped", summary="「筛选后自动 AI 分析」开关关闭")
            return
        if only_scan_result_id is None and is_ai_paused(cfg):
            # AI 分析暂停时段（2026-10-26）：每天固定北京时间整点区间内不跑批量
            # 分析（省 LLM 成本）；手动单币重分析不受限（与上面的开关同语义）
            label = pause_window_label(cfg)
            logger.info(
                "处于 AI 分析暂停时段（北京时间 %s），批量分析跳过 #%s",
                label, scan_record_id,
            )
            close_task(
                task_id, "skipped",
                summary=f"AI 分析暂停时段（北京时间 {label}），跳过本轮",
            )
            return
        q = select(ScanResult).where(ScanResult.scan_record_id == UUID(scan_record_id))
        # 连损降速（docs/24 A3 软熔断）：启用自动交易的账号当日连损任一 ≥2 →
        # 批量分析配额减半。此最大值不代表每个交易账号的动态开仓门槛；仅作为
        # 提示词中的一般性说明，具体门槛仍由执行层按账号单独判断。
        # 手动单币分析不受批量配额上限限制。
        # 账号/连损解析失败不阻断扫描（按无连损处理）
        try:
            losses = 0
            for acc in auto_trade_accounts(db):
                losses = max(losses, _consecutive_losses_today(db, acc.key, acc.testnet))
        except Exception as e:
            logger.warning("连损查询失败，批量分析配额不降速: %s", e)
            losses = 0
        skipped_live = 0
        if only_scan_result_id:
            q = q.where(ScanResult.id == UUID(only_scan_result_id))
        else:
            # 正式网已有在跑单的币种直接跳过 AI 分析（2026-10-25）：同币在正式网
            # 已有持仓/管理中的单子（OPENED/TP1_HIT/TP2_HIT），新分析不可能再
            # 开一单（开仓闸门按账号同币拦截），只消耗 LLM 配额——在配额排序前
            # 剔除，把名额让给其他新命中。手动单币重分析（only_scan_result_id）
            # 由用户主动触发，不受此限。
            live_running = set(db.execute(
                select(TradeRecord.symbol).where(
                    TradeRecord.testnet.is_(False),
                    TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
                ).distinct()
            ).scalars().all())
            if live_running:
                skipped_live = db.execute(
                    select(func.count()).select_from(ScanResult).where(
                        ScanResult.scan_record_id == UUID(scan_record_id),
                        ScanResult.symbol.in_(live_running),
                    )
                ).scalar_one()
                q = q.where(ScanResult.symbol.notin_(live_running))
                if skipped_live:
                    logger.info(
                        "正式网在跑币种跳过 AI 分析 %d 个（%s） #%s",
                        skipped_live, ",".join(sorted(live_running)), scan_record_id,
                    )
            # 批量：按质量分降序（指标驱动筛选的排序键，旧数据无分排后）、
            # 同分按 24h 成交额——控制 LLM 成本；单币重分析不受限。
            # 配额走筛选参数组（scan_filters > env > 默认，2026-09-28）
            quota = resolve_scan_filters(cfg).ai_max_per_scan
            if losses >= 2:
                derated = max(1, quota // 2)
                logger.info(
                    "连损 %d 笔降速：批量分析配额 %d → %d（扫描 #%s）",
                    losses, quota, derated, scan_record_id,
                )
                quota = derated
            q = q.order_by(
                ScanResult.quality_score.desc().nulls_last(),
                ScanResult.volume_24h.desc(),
            ).limit(quota)
        results = db.execute(q).scalars().all()

        # 4.5 批量分析与开单前的两道过滤（2026-10-27，docs/16 §5.2/§5.3）：
        #     不可交易合约 + 单币冷却。手动单币重分析（only_scan_result_id）不受限——
        #     用户主动点的那一个必须被分析。过滤后为空则按「无可分析」收尾，
        #     与真实无命中区分（summary 写明被谁过滤），便于看板判读。
        skipped_unlisted = 0
        skipped_cooldown = 0
        if results and only_scan_result_id is None:
            results, skipped_unlisted, skipped_cooldown = _filter_ai_targets(db, results)
        if not results:
            if skipped_live:
                logger.info(
                    "新命中币种均已在正式网在跑，跳过 AI 分析 %d 个 #%s",
                    skipped_live, scan_record_id,
                )
                close_task(
                    task_id, "skipped",
                    summary=f"新命中 {skipped_live} 个均已在正式网在跑，跳过 AI 分析",
                )
            elif skipped_unlisted or skipped_cooldown:
                logger.info(
                    "命中均已过滤跳过 #%s（未上架 %d / 单币冷却 %d）",
                    scan_record_id, skipped_unlisted, skipped_cooldown,
                )
                close_task(
                    task_id, "skipped",
                    summary=f"命中均已过滤跳过（未上架 {skipped_unlisted} / 冷却 "
                            f"{skipped_cooldown}）",
                )
            else:
                logger.info("无可分析的扫描结果 #%s", scan_record_id)
                close_task(task_id, "skipped", summary="无可分析的扫描结果")
            return

        force = only_scan_result_id is not None
        jobs = group(
            run_ai_analysis_single.s(str(r.id), scan_record_id, user_input, force, losses)
            for r in results
        )
        if force:
            # 手动单币重分析：用户在场主动触发，不链式执行（docs/06 A1 回归口径）
            jobs.apply_async()
        else:
            # 批量分析完成后链式触发一次结算巡检（docs/06 A1/E3）：chord 回调在
            # 全部子任务结束后派发 run_auto_trade_task——本轮新开单立即获得首轮
            # 结算/巡检（SL 补挂/TP1 推进/保本移损），不再等下一个整点 :01。
            # 同一轮扫描只触发 1 次；子任务硬失败致 chord 未触发时由 30min 节拍
            # （AUTO_TRADE_BEAT_MINUTES，docs/06 A2）兜底，巡检幂等多触发无副作用
            chord(jobs)(
                celery_app.signature(
                    "app.tasks.trade_tasks.run_auto_trade_task", immutable=True,
                )
            )
        logger.info("已分发 %d 个 AI 分析子任务 #%s", len(results), scan_record_id)
        live_skip_note = f"，正式网在跑跳过 {skipped_live} 个" if skipped_live else ""
        if skipped_unlisted:
            live_skip_note += f"，未上架跳过 {skipped_unlisted} 个"
        if skipped_cooldown:
            live_skip_note += f"，单币冷却跳过 {skipped_cooldown} 个"
        close_task(
            task_id, "completed",
            summary=f"已分发 {len(results)} 个分析子任务" + live_skip_note
                    + ("（手动单币重分析）" if force else f"（扫描 #{scan_record_id[:8]}）"),
            detail={
                "scan_record_id": scan_record_id, "dispatched": len(results),
                "force": force, "live_running_skipped": skipped_live,
                "unlisted_skipped": skipped_unlisted,
                "cooldown_skipped": skipped_cooldown,
            },
        )
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("AI 分析任务分发失败: %s", e)
        # 瞬时故障（DB 断连等）自动重试：子任务幂等（闸门/指纹/沿用短路），重发无副作用
        raise self.retry(countdown=30)
    finally:
        db.close()


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_single", bind=True, max_retries=2)
def run_ai_analysis_single(
    self, scan_result_id: str, scan_record_id: str,
    user_input: Optional[str] = None, force: bool = False,
    consecutive_losses: Optional[int] = None,
):
    """单币种 AI 分析子任务：闸门 → 缓存 → LLM(带校验回炉) → 落库

    consecutive_losses（2026-10-21，T0-2）：跨账号最大连损值，仅用于提示动态门槛
    可能存在；不得当成所有账号统一门槛，真实门槛按账号在开仓时复核。
    """
    # 生效 AI 运行参数（模型/端点/超时/并发）每轮重读 DB：先短读 cfg 刷新再取
    # 信号量（避免持 DB 连接等信号量——原顺序为先取信号量后开 session）
    _boot = SessionLocal()
    try:
        configure_from(_boot.get(SystemConfig, 1))
    finally:
        _boot.close()
    sem = _sem_acquire()
    db = SessionLocal()
    try:
        r = db.get(ScanResult, UUID(scan_result_id))
        if r is None:
            return
        cfg = db.get(SystemConfig, 1)
        # 同父任务：cfg 缺失时下游直接读字段会 AttributeError（2026-10-26 守卫）
        if not cfg or not resolve_credentials(cfg)[0]:
            return
        if not cfg.ai_analysis_enabled and not force:
            # 「筛选后自动 AI 分析」开关只拦批量自动分析（与父任务闸门口径一致）；
            # 手动单币重分析（force = only_scan_result_id 非空）不受限。
            # 此前这里静默 return 且不写进度事件 → 前端「等待分析任务开始」卡死
            logger.info("筛选后自动 AI 分析开关关闭，批量分析子任务跳过 %s", r.symbol)
            return
        if not force and is_ai_paused(cfg):
            # AI 分析暂停时段（2026-10-26）：分发后跨入暂停时段的子任务同样跳过，
            # 与父任务闸门同口径；手动单币重分析（force）不受限
            logger.info("处于 AI 分析暂停时段，批量分析子任务跳过 %s", r.symbol)
            return

        strategy_prompt = (
            cfg.strategy_prompt
            if cfg.strategy_prompt_enabled and cfg.strategy_prompt
            else None
        )
        # TP2 分批止盈开关（2026-10-08）：关闭时两管线提示词剔除止盈二 + 输出硬清零；
        # 定义在指纹缓存沿用（_copy）之前——沿用路径同样需要清零 TP2
        _tp2_on = bool(cfg.tp2_enabled)
        signal = {
            "symbol": r.symbol,
            "signal_type": r.signal_type,
            "current_price": float(r.current_price),
            "breakout_pct": float(r.breakout_pct),
            "pattern": r.pattern,
            "signal_reason": r.signal_reason,
            "touch_ref": r.touch_ref,  # 回踩触点（docs/22），无触点信号为 None
            "position": r.position,
            "volume_type": r.volume_type,
            "volume": float(r.volume),
            "volume_24h": float(r.volume_24h),
            # 质量分（T0-3）：提示词多处要求「结合质量分」，此前事实包里没有
            "quality_score": (
                float(r.quality_score) if r.quality_score is not None else None
            ),
            "quality_parts": r.quality_parts,
        }
        # 战法库（2026-10-02）：装载活动战法 → 渲染判定块注入用户消息 + 白名单
        # 随 signal 进 risk_guard 闸门（防 LLM 编造战法名）。2026-10-03 删环境
        # 切换：传 None = 装载全部活动战法（同一分析双网各开一单，scope 取并集）；
        # 装载失败降级不阻断
        from app.services import tactics_match
        _tactics = tactics_match.load_active_tactics(None)
        tactics_block = tactics_match.render_tactics_block(_tactics)
        signal["tactic_names"] = tactics_match.allowed_names(_tactics)
        fp = compute_fingerprint(signal)  # 取数后再以完整上下文生成最终指纹
        logger.info(
            "AI 分析开始: %s strength=%s fp=%s", r.symbol, r.strength, fp[:8]
        )
        # 进度事件：start（前端流式展示分析过程）
        ai_progress.push_start(r.id, r.symbol)

        # ── 程序侧 skip 闸门已全部移除（2026-09-15 拍板）：强度/陈旧/振幅熔断不再拦截，
        #    选中的币种一律交真实 AI 分析，跳过与否由 AI 自己决定 ──
        pool = ExchangePool()
        # 基准时刻取一次（H4，2026-10-12）：强刷判定与幽灵尾根判定共用同一 now，
        # 避免重读时钟造成「判定为尾窗、screen 时已跨点」的错配
        tail_now = datetime.now(timezone.utc)
        in_tail = md.in_forming_tail(cfg.kline_interval, tail_now)
        # 扫描尾窗同口径 + 强刷（H5，2026-10-12）：K线缓存按小时桶且桶内不刷新，
        # 尾窗内未收盘根随时在变——不强刷会读到小时内早期快照（依赖扫描侧是否刚
        # 刷过桶的时序），与扫描看到的分属两根不同口径的「信号根」。尾窗内强刷，
        # 非尾窗沿用缓存（省请求）
        klines = pool.get_klines(r.symbol, cfg.kline_interval, 500, force_refresh=in_tail)
        if len(klines) < 500 and not in_tail:
            # K线不足500根（止盈锚定/回退的前提是查满500根窗口）：强刷重取一次；
            # 仍不足则为新上市合约的全部历史，用可用K线继续（尾窗内已强刷，不重复）
            klines = pool.get_klines(r.symbol, cfg.kline_interval, 500, force_refresh=True)
            logger.info("AI 分析 %s K线不足500根，强刷重取后 %d 根", r.symbol, len(klines))

        # 扫描同口径（2026-10-11-4；2026-10-13 扩整点竞态）：分析紧随扫描，事实包
        # 必须与扫描看到同一根信号K线——尾窗内近收盘根、或整点取数竞态下末根实为
        # 刚收盘根（新周期根尚未出现在响应里）→ 幽灵尾根视图（K线摘要/指标块/摆动
        # 结构/current_price 全部对齐扫描口径）；否则原样返回（信号根=上一根已收盘）
        klines = md.screen_klines(klines, cfg.kline_interval, tail_now)
        # 量比（T0-1）：量比口径 = 近 20 根已收盘 K 线中位量的倍数，与扫描侧
        # classify_volume / 质量分加分 / 门槛块同一函数；signal["volume"] 仍是
        # 原始成交量，仅作记录，不得再被渲染成「×20根均量」
        try:
            signal["volume_ratio"] = float(volume_ratio(klines))
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            signal["volume_ratio"] = None
        if in_tail:
            # 信号根 = 未走完的近收盘根（H1，2026-10-12）：其收盘/极值未定型，
            # 下游「信号K极值已定型」前提不成立——打标供 risk_guard 放宽近锚硬校验。
            # 整点竞态场景末根已收盘（走完），不打标
            signal["signal_bar_provisional"] = True

        # 分析时刻最新价（未收盘K线的现价）：信号里的 current_price 是扫描时价格，
        # 扫描到分析之间可能已明显移动——所有锚点换算、入场价校验都以最新价为基准
        if klines:
            signal["current_price"] = float(klines[-1][4])
        # ── Stage 2：LLM 分析（P0 单次调用）+ Risk Guard 校验 ──
        # LLM 长调用（数分钟）期间不持有 DB 连接——任务开头取出的连接可能中途被
        # 网络层掐断（pool_pre_ping 只在取出时探测），落库前换新会话并重取行
        db.close()
        db = SessionLocal()
        r = db.get(ScanResult, r.id)
        # 近期摆动结构（HH/LH/HL/LL，高低点各 20 个）随事实包给 AI，与图表标注同源；
        # 分析时刻现算（现价已更新），风控锚定使用同一结构
        signal["recent_swings"] = recent_swings(klines, order=cfg.swing_order, n=20)
        # 4h 高周期 K 线（大方向背景注入，600 根）；失败只降级为无 4h 行，不中断分析
        try:
            # 600 根：EMA144 的 SMA 种子残留从 300 根的 ~11% 降到 <0.2%，
            # 4h 斜率/走平判定才稳定（与 scanner 取数窗口同口径）
            htf_klines = pool.get_klines(r.symbol, "4h", 600)
            # 4h 末根已收盘竞态兜底（与扫描同口径）：整点取数时新 4h 根可能尚未
            # 出现在响应里，补幽灵尾根避免状态滞后一根（4 小时）
            htf_klines = md.screen_klines(htf_klines, "4h", tail_now)
        except Exception as e:
            htf_klines = None
            logger.warning("AI 分析 %s 获取4h K线失败，跳过4h背景: %s", r.symbol, e)
        # 4h 数据降级标记（2026-10-21 观测 B2）：4h K线缺失/拉取失败时
        # 仅记录降级（不做方向否决），结论在无大周期背景下产出；
        # 逐行落库供复盘按降级/正常分两组对比（B3）
        htf_degraded = not htf_klines
        # 4h 状态快照时刻（T1）：参与状态计算的最后一根已收盘 4h 收盘时间；
        # 开仓前按此刻复核陈旧度（跨周期禁开）。尾窗场景封顶到当前时刻。
        htf_as_of = md.htf_snapshot_time(htf_klines, tail_now)
        # 先取得当前大周期事实，再构造缓存上下文；缺数或尾窗不复用旧裁决。
        cache_context = build_cache_context(cfg, klines, htf_klines or [],
                                            tactics_block, consecutive_losses)
        fp = compute_fingerprint(signal, cache_context)
        if (not force and not user_input and not in_tail and not htf_degraded
                and settings.FINGERPRINT_TTL_MIN > 0):
            cached = _find_recent(db, r.symbol, fp, settings.FINGERPRINT_TTL_MIN)
            if cached and _conclusion_still_valid(cached, signal["current_price"]):
                try:
                    current_htf = htf_context(htf_klines)
                except Exception:
                    current_htf = None
                # 缓存有效性仍按当前 4h 状态做陈旧性校验（数据变化则重新分析），
                # 但不再对 AI 方向做强制改判。
                eligible = (bool(current_htf)
                            and not current_htf.get("reject_reason")
                            and not current_htf.get("block_both")
                            and (cached.trade_decision == "skip"
                                 or current_htf.get("direction") == cached.direction))
                if eligible:
                    note = "⏱️ 沿用近期同上下文结论"
                    ai_progress.push(r.id, "gate", note=note)
                    _copy(db, r, r.symbol, cached, fp, note, tp2_enabled=_tp2_on,
                          current_price=signal["current_price"],
                          htf_as_of=htf_as_of)
                    ai_progress.push_done(r.id, cached.trade_decision, note)
                    return
        # 市场环境（资金费率/大盘/恐贪）不再预取注入——保持数据契约干净
        # 止损金额口径（2026-09-27）：与开仓引擎同源的预算模式/金额/文案，
        # 传给提示词与仓位复算（risk_budget.resolve）
        from app.services.risk_budget import resolve_risk_budget
        _risk_mode, _risk_amount, _risk_label = resolve_risk_budget(cfg)
        # 止损宽度硬上限透传（2026-09-29）：max_stop_pct 补丁参数注入门槛块，
        # AI 定止损前自验不超限，避免"AI 建议开单→程序宽度闸弃单"的无效调用
        _stop_cap = float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None
        # 风控参数上页面（2026-10-02）：RR 铁律 / BTC 急跌阈值按 cfg 覆盖优先、
        # NULL 回落 env，透传进提示词与 Risk Guard（同源）；
        # 2026-10-13 起急跌仅作提示词警示，开仓引擎侧硬闸已移除
        # 风险比例单一来源（2026-10-02 复查）：pct 模式下 resolver 的 amount 即
        # 生效比例（cfg.risk_pct ?? RISK_BUDGET_PCT），直接派生保证提示词 label
        # 与 Risk Guard 仓位复算同口径（TRADING_RISK_PCT 仅开仓引擎 est_risk 用，
        # 两 env 默认同 3.0）；fixed 模式 amount 是 USDT 实额，不传 risk_pct
        _rr_min = (
            float(cfg.ai_rr_min) if cfg.ai_rr_min is not None
            else float(settings.AI_RR_MIN)
        )
        # RR/TP1 容差（2026-10-14）：同一条 ai_rr_tol 透传两管线的 Risk Guard
        # （RR 三价铁律 + TP1 上限）；None 传 None，校验侧回落 settings.AI_RR_TOL
        _rr_tol = (
            float(cfg.ai_rr_tol) if cfg.ai_rr_tol is not None
            else None
        )
        _risk_pct = _risk_amount if _risk_mode == "pct" else None
        # 评分门槛（2026-10-21）：注入提示词「评分纪律」段，让 AI 知道低于门槛
        # 等于程序不下单（此前的分发点门槛只存在于程序侧，AI 会输出 58 这类中间分
        # 表达"保守"，实际效果是隐性弃单）
        _min_rec = (
            float(cfg.min_recommendation) if cfg.min_recommendation is not None
            else float(settings.TRADING_MIN_RECOMMENDATION)
        )
        _crash_pct = (
            float(cfg.btc_crash_pause_pct) if cfg.btc_crash_pause_pct is not None
            else None
        )
        # 筛选参数组（2026-10-12）：门槛块阈值/触点启用列表读页面配置生效值，
        # 与扫描侧 resolve 同源——页面改筛选配置后 AI 提示词口径同步跟上
        _flt = resolve_scan_filters(cfg)
        # 管线：P0 单次调用 + 校验回炉（批量与手动单币重分析同一条路径）
        ai_progress.push(r.id, "round", round=0, tools=[], note="单次调用管线思考中…")
        ai_result = analyze_with_guard(
            signal, klines,
            strategy_prompt=strategy_prompt,
            user_input=user_input,
            htf_klines=htf_klines,
            risk_mode=_risk_mode,
            risk_amount=_risk_amount,
            risk_label=_risk_label,
            stop_cap_pct=_stop_cap,
            rr_min=_rr_min,
            risk_pct=_risk_pct,
            crash_pct=_crash_pct,
            rr_tol=_rr_tol,
            interval=cfg.kline_interval,
            progress_id=r.id,  # 方案 B 轻量进度：流式增量计数推 "llm" 事件（≥2s 节流）
            tactics_block=tactics_block,
            tp2_enabled=_tp2_on,
            flt=_flt,
            min_recommendation=_min_rec,
            consecutive_losses=consecutive_losses,
        )
        # 4h 状态快照落库（2026-10-21 用户拍板：方向限制只保留在筛选层，
        # AI 出结果后不再做双向禁开/锁逆势的程序改判）
        ai_result = annotate_htf_phase(ai_result, htf_klines)
        # Narrator 已下线（提速）：输出 reason 直接承载完整分点分析
        # （提示词强约束"1. 2. 3."每条一行），落库即 analysis，省一次 LLM 往返
        # 待回踩判定（2026-09-15）：入场在现价回踩侧的建议仅展示标识、不下单
        ai_result["pullback_wait"] = is_pullback_wait(ai_result, signal.get("current_price"))
        # 4h 数据降级标记（B2）：随结论落库（AIAnalysis.htf_degraded）
        ai_result["htf_degraded"] = htf_degraded
        # 4h 状态快照时刻（T1）：缺数时为 None，开仓前由 htf_degraded 拦截
        ai_result["htf_as_of"] = htf_as_of
        _finish(db, r, r.symbol, ai_result, fp)
        ai_progress.push_done(
            r.id, ai_result.get("trade_decision"),
            str(ai_result.get("analysis") or "")[:80],
        )
    except Exception as e:
        # 网关超时（延迟波动剧烈，实测 130s~>240s 随机）：延迟自动重试，不推 error 事件。
        # 重试额度用尽（self.retry 抛 MaxRetriesExceeded）必须推 error 收尾，
        # 否则前端进度流永远停在「重试中」（2026-09-28 排查网关超时卡死）
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            if self.request.retries >= self.max_retries:
                logger.error("AI 分析 %s 网关超时且重试额度用尽，标记失败", scan_result_id)
                ai_progress.push(scan_result_id, "error", note="⏳ 网关超时，重试 2 次后仍失败")
            else:
                logger.warning(
                    "AI 分析 %s 网关超时，%ds 后自动重试（第 %d/2 次）",
                    scan_result_id, 90, self.request.retries + 1,
                )
                ai_progress.push(scan_result_id, "gate", note="⏳ 网关超时，90s 后自动重试")
                raise self.retry(countdown=90)
        logger.warning("AI 分析 %s 失败: %s", scan_result_id, e)
        ai_progress.push(scan_result_id, "error", note=str(e)[:200])
        try:
            db.rollback()
        except Exception:
            pass  # 连接已死时 rollback 也会抛，吞掉以保住原始错误信息
    finally:
        db.close()
        _sem_release(sem)


def _conclusion_still_valid(a: AIAnalysis, current_price: float) -> bool:
    """沿用旧结论前校验价格区仍有效：现价须仍在该方向的 止损~止盈一 区间内。

    价格已越过原 TP1/SL 的旧结论没有参考意义（如价格已到止盈位还提示做多）——
    不再沿用，落入正常 LLM 重分析。skip 可复用，suggest 数据不全则拒绝复用。
    """
    if a.trade_decision == "skip":
        return True
    if a.trade_decision != "suggest" or a.direction not in ("long", "short"):
        return False
    try:
        price, sl, tp1 = (
            float(current_price), float(a.stop_loss or 0), float(a.take_profit_1 or 0),
        )
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(v) and v > 0 for v in (price, sl, tp1)):
        return False
    if a.direction == "long":
        return sl < price < tp1
    return sl > price > tp1


def _find_recent(db, symbol: str, fp: str, ttl_min: int) -> Optional[AIAnalysis]:
    """TTL 内同 symbol 同指纹的最近结论"""
    cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
    return db.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.symbol == symbol,
            AIAnalysis.fingerprint == fp,
            func.coalesce(AIAnalysis.cache_origin_at, AIAnalysis.ai_finished_at,
                          AIAnalysis.created_at) >= cutoff,
            AIAnalysis.trade_decision.isnot(None),
        )
        .order_by(AIAnalysis.created_at.desc())
    ).scalars().first()


def _copy(db, r: ScanResult, symbol: str, src: AIAnalysis, fp: str, note: str,
          tp2_enabled: bool = True, current_price: Optional[float] = None,
          htf_as_of: Optional[datetime] = None) -> None:
    """沿用旧结论：复制到新 scan_result_id（analysis 加沿用说明）。

    必须提交：前端行级轮询以"分析记录出现"为完成标志，不提交会导致轮询挂满超时。
    tp2_enabled（2026-10-08）：TP2 分批关闭时沿用记录同样清零 take_profit_2
    （旧结论可能带 TP2 价，落库口径须与当前出场规则一致）。
    """
    result = {
        "trade_decision": src.trade_decision,
        "skip_reason": src.skip_reason,
        "direction": src.direction,
        "trade_type": src.trade_type,
        "analysis": (note + "\n" + (src.analysis or "")).strip(),
        "entry_price": float(src.entry_price or 0),
        "stop_loss": float(src.stop_loss or 0),
        "take_profit_1": float(src.take_profit_1 or 0),
        "take_profit_2": float(src.take_profit_2 or 0) if tp2_enabled else 0,
        "risk_reward_ratio": float(src.risk_reward_ratio or 0),
        "position_pct": float(src.position_pct) if src.position_pct is not None else None,
        "stop_anchor": src.stop_anchor,
        "tactic_name": src.tactic_name,
        "tactic_reason": src.tactic_reason,
        "htf_phase": src.htf_phase,
        "veto_original": src.veto_original,
        "stage_trace": src.stage_trace,
        "cache_origin_at": src.cache_origin_at or src.ai_finished_at or src.created_at,
        "recommendation": float(src.recommendation or 0),
        # 评分原始值（T2）：随档位一起沿用，校准分析不因缓存断链
        "recommendation_raw": (
            float(src.recommendation_raw) if getattr(src, "recommendation_raw", None) is not None
            else float(src.recommendation or 0)
        ),
        "pullback_wait": bool(src.pullback_wait),
        # 4h 数据降级标记（B2）沿用源结论：缓存复用不重新拉 4h，降级态随结论走
        "htf_degraded": bool(getattr(src, "htf_degraded", False)),
        # 4h 快照时刻（T1）：缓存命中意味着当前 4h 已收盘上下文与源结论一致
        # （指纹含已收盘 4h 摘要），用本次取数时刻续期，避免复用单被时效闸误杀
        "htf_as_of": htf_as_of,
        # BTC 环境缺失标记（T0-5）沿用源结论：缓存复用不重新拉市场环境
        "btc_env_missing": bool(getattr(src, "btc_env_missing", False)),
    }
    result["pullback_wait"] = is_pullback_wait(
        result, float(r.current_price) if current_price is None else current_price)
    _finish(db, r, symbol, result, fp)
    logger.info("AI 结论沿用: %s ← %s", symbol, src.id)


def _finish(db, r: ScanResult, symbol: str, ai_result: dict, fp: str, commit: bool = True):
    row = _upsert_ai_analysis(db, r.id, symbol, ai_result, fingerprint=fp)
    if commit:
        db.commit()
        logger.info("AI 分析完成: %s (%s)", symbol, ai_result.get("trade_decision"))
        _dispatch_immediate_open(row)


def _dispatch_immediate_open(a: AIAnalysis) -> None:
    """唯一开仓通道：suggest 且过评分门槛的分析一落库即分发开仓任务（2026-09-14 起
    取消 :42 批量开仓，本分发失败即无兜底）。

    只做分发不做开仓（闸门全在 open_trade_for_analysis → try_open_for_analysis 里）；
    静默失败不重试——等该币下次信号重新分析再触发。
    """
    if a.trade_decision != "suggest":
        return
    # 待回踩单照常分发（2026-09-18）：回踩距离 ≤ pullback_market_pct 容差时
    # 市价入场、超容差在 _attempt_open 偏离闸门拦下——不再一刀切不下单
    # 开关以 DB 为运行时权威（2026-09-20 由 env 迁移进 system_config）
    # 评分门槛（2026-10-02 上页面）：cfg.min_recommendation 覆盖时优先，
    # NULL 回落 env——门槛检查需 cfg 行，故移入 db 块内
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, 1)
        # 双网独立开关（2026-10-03）：任一网开启即分发，具体扇出哪些账号
        # 由 open_trade_for_analysis 按 auto_trade_accounts 逐网判定；
        # 测试网总开关（2026-10-27）关闭时测试网分开关不生效
        auto_open_on = cfg is not None and (
            (cfg.trading_enabled_testnet and getattr(cfg, "testnet_enabled", True) is not False)
            or cfg.trading_enabled_live
        )
        if not auto_open_on:
            # 双网自动交易全关：没有可扇出的账号，不派发开仓任务；但达标信号
            # 仍要提醒——2026-10-27 之前这里直接 return，18:00 那批 9 条
            # suggest 一条飞书都没有，用户无从得知「AI 说了可以开单」。
            # 复用 notify_live_auto_open_off（自带三价齐/评分门槛/已成交去重），
            # 它自己吞异常只记日志，不阻断本任务。
            if cfg is not None:
                from app.services.trade_engine import notify_live_auto_open_off
                notify_live_auto_open_off(db, cfg, str(a.id), both_off=True)
            return
        min_rec = (
            float(cfg.min_recommendation) if cfg.min_recommendation is not None
            else float(settings.TRADING_MIN_RECOMMENDATION)
        )
        if float(a.recommendation or 0) < min_rec:
            return
    finally:
        db.close()
    celery_app.send_task(
        "app.tasks.trade_tasks.open_trade_for_analysis", args=[str(a.id)]
    )
    logger.info("已分发即时开仓任务: %s（评分 %s）", a.symbol, a.recommendation)


def _upsert_ai_analysis(
    db, scan_result_id: UUID, symbol: str, ai_result: dict,
    fingerprint: Optional[str] = None,
):
    """latest-wins：有则更新，无则新建；返回落库行（供即时开仓分发取 id）。

    保护性摘钩（2026-10-11，ALTUSDT 案例）：命中已存在且被 trade_records/
    trade_reviews 引用的行时不再原地覆盖（会把「已开单」的结论静默改写——
    交易记录 ai_snapshot 冻住开单时三价，AI 卡片却显示新结论，看卡片的人
    被误导），旧行摘钩（scan_result_id=None，与手动单币重分析同语义），
    新结论另起新行照常走开仓闸门。"""
    fields = {
        "trade_decision": ai_result.get("trade_decision"),
        "skip_reason": ai_result.get("skip_reason"),
        "direction": ai_result.get("direction"),
        "trade_type": ai_result.get("trade_type"),
        "analysis": ai_result.get("analysis"),
        "entry_price": ai_result.get("entry_price"),
        "stop_loss": ai_result.get("stop_loss"),
        "take_profit_1": ai_result.get("take_profit_1"),
        "take_profit_2": ai_result.get("take_profit_2"),
        "risk_reward_ratio": ai_result.get("risk_reward_ratio"),
        "position_pct": ai_result.get("position_pct"),
        "recommendation": ai_result.get("recommendation"),
        # 评分原始值（T2 方案 A）：档位归一的原始分独立落库，供校准与弃单复盘
        "recommendation_raw": ai_result.get("recommendation_raw"),
        # 止损锚类型（docs/23）：suggest 时由 risk_guard fixed dict 保证有值
        # （None→structure），skip 行为 None（旧行同样按 structure 理解）
        "stop_anchor": ai_result.get("stop_anchor"),
        # 规则版本快照（docs/24 A0）：复盘统计按版本分组，跨版本不混谈
        "rule_version": settings.RULE_VERSION,
        # 统一决策版本（T5）：配置/补丁/提示词变更的统一切点，复盘按版本分组
        "decision_version": current_decision_version(db),
        # 历史双评委 veto 原始建议快照（docs/24 A5a；该功能已下线，字段保留恒为 None）；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "veto_original": ai_result.get("veto_original"),
        # 战法命中标注（2026-10-02）：白名单闸门后的值（清单外已清空）；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "tactic_name": ai_result.get("tactic_name"),
        "tactic_reason": ai_result.get("tactic_reason"),
        # 4h 排列强度（2026-10-10 五次拍板）：enforce 阶段 stash，复盘维度分组用；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "htf_phase": ai_result.get("htf_phase"),
        "pullback_wait": bool(ai_result.get("pullback_wait")),
        # 待回踩结论在分发前就被跳过（_dispatch_immediate_open），不会进入开仓闸门，
        # 未开单原因在此预置；其余结论的原因由开仓闸门拦截时写入
        "open_block_reason": (
            "AI 建议等回踩至入场价，仅展示待回踩标识、不下单"
            if bool(ai_result.get("pullback_wait")) else None
        ),
        "fingerprint": fingerprint,
        "cache_origin_at": ai_result.get("cache_origin_at") or datetime.utcnow(),
        "stage_trace": ai_result.get("stage_trace"),
        # 4h 数据降级标记（2026-10-21 观测 B2）：分析时 4h K线缺失 → True，
        # 复盘据此分组（B3）；latest-wins 重分析按最新一次的事实覆盖
        "htf_degraded": bool(ai_result.get("htf_degraded")),
        # 4h 状态快照时刻（T1）：开仓前时效复核用；缺数时 None
        "htf_as_of": ai_result.get("htf_as_of"),
        # BTC 环境事实缺失（T0-5）：市场环境块缺失时提示词已标「未知」，
        # 本列供复盘分组（数据缺失场景的结论质量单独看）
        "btc_env_missing": bool(ai_result.get("btc_env_missing")),
        # 结论落库时刻（2026-10-21 观测 B1）：created_at 原地更新不刷新，
        # 不能代表完成时刻；本字段每次落库覆写，供复盘算 signal_age_s
        "ai_finished_at": datetime.utcnow(),
    }
    existing = db.execute(
        select(AIAnalysis).where(AIAnalysis.scan_result_id == scan_result_id)
    ).scalars().first()

    if existing:
        # 引用检查（轻查询各 limit 1）：开单/复盘引用任一存在即视为已消费
        referenced = (
            db.execute(
                select(TradeRecord.id)
                .where(TradeRecord.ai_analysis_id == existing.id).limit(1)
            ).scalar() is not None
            or db.execute(
                select(TradeReview.id)
                .where(TradeReview.ai_analysis_id == existing.id).limit(1)
            ).scalar() is not None
        )
        if referenced:
            # 摘钩保留（先 flush 保证 UPDATE 先于 INSERT 发出），新结论另起新行
            existing.scan_result_id = None
            db.flush()
            row = AIAnalysis(scan_result_id=scan_result_id, symbol=symbol, **fields)
            db.add(row)
            return row
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing
    row = AIAnalysis(scan_result_id=scan_result_id, symbol=symbol, **fields)
    db.add(row)
    return row
