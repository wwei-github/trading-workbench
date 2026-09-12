"""AI 分析 Celery 任务（P0 管线，docs/04）

- 批量：Celery group 按币种并行分发（原串行 + sleep(1) 已废弃）
- 并发：Redis zset 信号量限制 LLM 全局并发（LLM_CONCURRENCY，默认 3）
- Stage 1 闸门（不调 LLM 直接出结论）：
    1) strength < AI_MIN_STRENGTH → 程序 skip
    2) 当前K线振幅 > ATR_SPIKE_MULT × ATR → 熔断 skip
    3) 1h 内同指纹 → 沿用旧结论
    4) 24h 内同币种已有 suggest 结论且本信号为重复命中 → 沿用旧结论
- Stage 2：analyze_with_guard（校验回炉）→ 落库
"""
import hashlib
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from celery import group
from redis import Redis
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanResult, AIAnalysis
from app.models.system_config import SystemConfig
from app.services import ai_progress
from app.services.ai_agent import analyze_coin_agent
from app.services.ai_analyzer import analyze_with_guard
from app.services.dual_judge import run_dual_judge
from app.services.exchange_pool import ExchangePool
from app.services.risk_guard import build_forced_skip, calc_atr
from app.services.strategy import compute_signal_key_levels, recent_swings

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


def _sem_acquire(timeout_s: int = 300) -> str:
    """获取 LLM 并发信号量，返回持有者 id（空串表示超时降级放行）"""
    r = _get_redis()
    ident = uuid4().hex
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        now_ms = int(time.time() * 1000)
        r.zremrangebyscore(_LLM_SEM_KEY, 0, now_ms - _SEM_STALE_MS)
        r.zadd(_LLM_SEM_KEY, {ident: now_ms})
        rank = r.zrank(_LLM_SEM_KEY, ident)
        if rank is not None and rank < settings.LLM_CONCURRENCY:
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


def compute_fingerprint(signal: dict) -> str:
    """信号指纹：价格 0.5% 对数分桶 + 信号要素。同指纹在 TTL 内可复用结论"""
    close = float(signal.get("current_price") or 0)
    bucket = round(math.log(close) / 0.005) if close > 0 else 0
    nearest_level = ""
    levels = signal.get("key_levels") or []
    if levels and close > 0:
        nearest = min(
            levels, key=lambda lv: abs(float(lv.get("price", 0)) - close)
        )
        nearest_level = f"{float(nearest['price']):.4f}"
    raw = "|".join([
        signal.get("symbol", ""),
        signal.get("signal_type", ""),
        signal.get("position") or "",
        signal.get("pattern") or "",
        str(bucket),
        nearest_level,
    ])
    return hashlib.sha1(raw.encode()).hexdigest()[:40]


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_task", bind=True, max_retries=0)
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
    try:
        cfg = db.get(SystemConfig, 1)
        if not cfg or not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
            logger.warning("AI 分析未启用，跳过")
            return
        q = select(ScanResult).where(ScanResult.scan_record_id == UUID(scan_record_id))
        if only_scan_result_id:
            q = q.where(ScanResult.id == UUID(only_scan_result_id))
        else:
            # 批量：按 24h 成交额降序取前 N（控制 LLM 成本；单币重分析不受限）
            q = q.order_by(ScanResult.volume_24h.desc()).limit(settings.AI_MAX_PER_SCAN)
        results = db.execute(q).scalars().all()
        if not results:
            logger.info("无可分析的扫描结果 #%s", scan_record_id)
            return

        force = only_scan_result_id is not None
        jobs = group(
            run_ai_analysis_single.s(str(r.id), scan_record_id, user_input, force)
            for r in results
        )
        jobs.apply_async()
        logger.info("已分发 %d 个 AI 分析子任务 #%s", len(results), scan_record_id)
    except Exception as e:
        logger.exception("AI 分析任务分发失败: %s", e)
    finally:
        db.close()


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_single", bind=True, max_retries=2)
def run_ai_analysis_single(
    self, scan_result_id: str, scan_record_id: str,
    user_input: Optional[str] = None, force: bool = False,
):
    """单币种 AI 分析子任务：闸门 → 缓存 → LLM(带校验回炉) → 落库"""
    sem = _sem_acquire()
    db = SessionLocal()
    try:
        r = db.get(ScanResult, UUID(scan_result_id))
        if r is None:
            return
        cfg = db.get(SystemConfig, 1)
        if not cfg or not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
            return

        strategy_prompt = (
            cfg.strategy_prompt
            if cfg.strategy_prompt_enabled and cfg.strategy_prompt
            else None
        )
        signal = {
            "symbol": r.symbol,
            "signal_type": r.signal_type,
            "current_price": float(r.current_price),
            "breakout_pct": float(r.breakout_pct),
            "pattern": r.pattern,
            "signal_reason": r.signal_reason,
            "position": r.position,
            "key_levels": r.key_levels,
            "volume_type": r.volume_type,
            "volume": float(r.volume),
            "volume_24h": float(r.volume_24h),
        }
        fp = compute_fingerprint(signal)
        logger.info(
            "AI 分析开始: %s strength=%s fp=%s", r.symbol, r.strength, fp[:8]
        )
        # 进度事件：start（前端流式展示分析过程）
        ai_progress.push_start(r.id, r.symbol, "agent")

        # ── Stage 1 闸门（无 LLM 成本）──
        # 1) 强度不足
        if r.strength is not None and float(r.strength) < settings.AI_MIN_STRENGTH:
            reason = f"信号强度 {float(r.strength):.2f} 低于阈值 {settings.AI_MIN_STRENGTH}，程序判定跳过"
            ai_progress.push(r.id, "gate", note=reason)
            _finish(db, r, r.symbol, build_forced_skip(reason), fp)
            ai_progress.push_done(r.id, "skip", reason)
            return
        # 2) 振幅熔断（当前已收盘K线振幅 > N×ATR，插针/异常行情不出建议）
        pool = ExchangePool()
        klines = pool.get_klines(r.symbol, cfg.kline_interval, 500)
        if len(klines) < 500:
            # K线不足500根（止盈锚定/回退的前提是查满500根窗口）：强刷重取一次；
            # 仍不足则为新上市合约的全部历史，用可用K线继续
            klines = pool.get_klines(r.symbol, cfg.kline_interval, 500, force_refresh=True)
            logger.info("AI 分析 %s K线不足500根，强刷重取后 %d 根", r.symbol, len(klines))
        atr = calc_atr(klines)
        if atr and len(klines) >= 2:
            closed = klines[-2]
            kline_range = float(closed[2]) - float(closed[3])  # high - low
            if kline_range > settings.ATR_SPIKE_MULT * atr:
                reason = f"当前K线振幅超过 {settings.ATR_SPIKE_MULT:g}×ATR，行情异常熔断"
                ai_progress.push(r.id, "gate", note=reason)
                _finish(db, r, r.symbol, build_forced_skip(reason), fp)
                ai_progress.push_done(r.id, "skip", reason)
                return

        # 分析时刻最新价（未收盘K线的现价）：信号里的 current_price 是扫描时价格，
        # 扫描到分析之间可能已明显移动——所有锚点换算、入场价校验都以最新价为基准
        if klines:
            signal["current_price"] = float(klines[-1][4])
        # 3) 指纹缓存：TTL 内同指纹沿用旧结论（手动重分析不短路）
        if not force and settings.FINGERPRINT_TTL_MIN > 0:
            cached = _find_recent(db, r.symbol, fp, settings.FINGERPRINT_TTL_MIN)
            if cached:
                note = "⏱️ 沿用近期同信号结论"
                ai_progress.push(r.id, "gate", note=note)
                _copy(db, r, r.symbol, cached, fp, note)
                ai_progress.push_done(r.id, cached.trade_decision, note)
                return
        # 4) 重复信号沿用：24h 内同币种已有 suggest 且本行为重复命中
        if not force and r.is_repeat:
            repeat = _find_recent_suggest(db, r.symbol, 24 * 60)
            if repeat:
                note = "🔁 24h 内重复信号，沿用已有结论"
                ai_progress.push(r.id, "gate", note=note)
                _copy(db, r, r.symbol, repeat, fp, note)
                ai_progress.push_done(r.id, repeat.trade_decision, note)
                return

        # ── Stage 2：LLM 分析（P1 Agent 工具循环 / P0 单次调用）+ Risk Guard 校验 ──
        # LLM 长调用（数分钟）期间不持有 DB 连接——任务开头取出的连接可能中途被
        # 网络层掐断（pool_pre_ping 只在取出时探测），落库前换新会话并重取行
        db.close()
        db = SessionLocal()
        r = db.get(ScanResult, r.id)
        # 近期摆动结构（HH/LH/HL/LL）随事实包给 AI，与图表标注同源
        signal["recent_swings"] = recent_swings(klines, order=cfg.swing_order, n=2)
        # 关键位在分析时刻重算（含 ATR 自适应区域与时间加权）：扫描落库的 key_levels
        # 是扫描时快照，分析时结构可能已变化。指纹已按扫描快照算完，缓存判定不受影响
        fresh_levels = compute_signal_key_levels(klines, {
            "swing_order": cfg.swing_order,
            "key_level_tolerance": float(cfg.key_level_tolerance),
            "level_merge_threshold": float(cfg.level_merge_threshold),
        })
        if fresh_levels:
            signal["key_levels"] = fresh_levels
        # 市场环境（资金费率/大盘/恐贪）不再预取注入——保持数据契约干净，
        # Agent 管线由模型按需调用工具自行获取
        # 管线选择：批量（每小时扫描自动触发/手动全量）固定走 P0 单次调用管线，
        # 不受「AI 管线」开关影响；仅手动单币重分析（force）跟随开关走 Agent 管线
        if cfg.ai_pipeline_enabled and force:
            def _cb(ev: dict) -> None:
                ai_progress.push(r.id, ev.pop("t"), **ev)

            ai_result = analyze_coin_agent(
                signal, klines,
                strategy_prompt=strategy_prompt,
                user_input=user_input,
                pool=pool,
                progress_cb=_cb,
            )
        else:
            ai_progress.push(r.id, "round", round=0, tools=[], note="单次调用管线思考中…")
            ai_result = analyze_with_guard(
                signal, klines,
                strategy_prompt=strategy_prompt,
                user_input=user_input,
            )
        # 双评委辩论复核（P2，默认关）：仅对 suggest 决策，只能 keep / veto
        if cfg.dual_judge_enabled:
            if ai_result.get("trade_decision") == "suggest":
                ai_progress.push(r.id, "gate", note="⚖️ 双评委辩论复核中…")
            ai_result = run_dual_judge(ai_result, signal)
        # Narrator 已下线（提速）：submit_decision 的 reason 直接承载完整分点分析
        # （工具 schema 强约束"1. 2. 3."每条一行），落库即 analysis，省一次 LLM 往返
        _finish(db, r, r.symbol, ai_result, fp)
        ai_progress.push_done(
            r.id, ai_result.get("trade_decision"),
            str(ai_result.get("analysis") or "")[:80],
        )
    except Exception as e:
        # 网关超时（延迟波动剧烈，实测 130s~>240s 随机）：延迟自动重试，不推 error 事件
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
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


def _find_recent(db, symbol: str, fp: str, ttl_min: int) -> Optional[AIAnalysis]:
    """TTL 内同 symbol 同指纹的最近结论"""
    cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
    return db.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.symbol == symbol,
            AIAnalysis.fingerprint == fp,
            AIAnalysis.created_at >= cutoff,
            AIAnalysis.trade_decision.isnot(None),
        )
        .order_by(AIAnalysis.created_at.desc())
    ).scalars().first()


def _find_recent_suggest(db, symbol: str, ttl_min: int) -> Optional[AIAnalysis]:
    """TTL 内同 symbol 的最近 suggest 结论"""
    cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
    return db.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.symbol == symbol,
            AIAnalysis.trade_decision == "suggest",
            AIAnalysis.created_at >= cutoff,
        )
        .order_by(AIAnalysis.created_at.desc())
    ).scalars().first()


def _copy(db, r: ScanResult, symbol: str, src: AIAnalysis, fp: str, note: str) -> None:
    """沿用旧结论：复制到新 scan_result_id（analysis 加沿用说明）。

    必须提交：前端行级轮询以"分析记录出现"为完成标志，不提交会导致轮询挂满超时。
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
        "take_profit_2": float(src.take_profit_2 or 0),
        "risk_reward_ratio": float(src.risk_reward_ratio or 0),
        "position_pct": float(src.position_pct or 0),
        "recommendation": float(src.recommendation or 0),
    }
    _finish(db, r, symbol, result, fp)
    logger.info("AI 结论沿用: %s ← %s", symbol, src.id)


def _finish(db, r: ScanResult, symbol: str, ai_result: dict, fp: str, commit: bool = True):
    _upsert_ai_analysis(db, r.id, symbol, ai_result, fingerprint=fp)
    if commit:
        db.commit()
        logger.info("AI 分析完成: %s (%s)", symbol, ai_result.get("trade_decision"))


def _upsert_ai_analysis(
    db, scan_result_id: UUID, symbol: str, ai_result: dict,
    fingerprint: Optional[str] = None,
):
    """latest-wins：有则更新，无则新建"""
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
        "fingerprint": fingerprint,
        "stage_trace": ai_result.get("stage_trace"),
    }
    existing = db.execute(
        select(AIAnalysis).where(AIAnalysis.scan_result_id == scan_result_id)
    ).scalars().first()

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(AIAnalysis(scan_result_id=scan_result_id, symbol=symbol, **fields))
