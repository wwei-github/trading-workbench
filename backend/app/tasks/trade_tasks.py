"""自动交易定时任务（docs/06）

每小时第 42 分钟（扫描第 2 分 + AI 分析之后）：
1. settle_trades：先结算/跟进在跑单子，释放额度与保证金
2. try_open_trades：再按评分降序开新仓（兜底：即时开仓未成交的候选）

即时开仓（2026-09-14）：AI 分析落库即由 ai_tasks 分发 open_trade_for_analysis，
suggest 且 ≥60 分不再等批次。批量与即时共用同一把 Redis 开仓锁串行执行，
防「在跑单上限 / 70% 余额」在并发下被同时绕过。

每次运行写任务记录（含跳过原因），失败进「系统日志」。
"""
import logging
import time
from uuid import uuid4

from redis import Redis

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.binance_trader import BinanceTrader
from app.services.task_log import close_task, open_task
from app.services.trade_engine import settle_trades, try_open_for_analysis, try_open_trades

logger = logging.getLogger(__name__)

# 开仓互斥锁：批量任务（分钟级，含结算）与即时开仓（秒级）串行
_OPEN_LOCK_KEY = "lock:trade_open"
_OPEN_LOCK_TTL_S = 900       # 批量任务最长持锁时间兜底（崩溃自动过期）
_OPEN_LOCK_WAIT_S = 45       # 即时开仓等锁上限，超时留给 :42 批次


def _acquire_open_lock(r: Redis, ident: str, wait_s: int, ttl_s: int) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if r.set(_OPEN_LOCK_KEY, ident, nx=True, ex=ttl_s):
            return True
        time.sleep(0.5)
    return False


def _release_open_lock(r: Redis, ident: str) -> None:
    try:
        if r.get(_OPEN_LOCK_KEY) == ident:
            r.delete(_OPEN_LOCK_KEY)
    except Exception:
        pass


@celery_app.task(name="app.tasks.trade_tasks.run_auto_trade_task", max_retries=0)
def run_auto_trade_task():
    task_id = open_task(task_type="trade", task_name="自动交易", trigger="scheduled")
    try:
        if not settings.TRADING_ENABLED:
            logger.info("自动交易未开启（TRADING_ENABLED=false），跳过")
            close_task(task_id, "skipped", summary="未开启自动交易（TRADING_ENABLED=false）")
            return
        trader = BinanceTrader()
        if not trader.configured:
            logger.warning("自动交易缺少币安 API 凭据（BINANCE_API_KEY/BINANCE_SECRET_KEY），跳过")
            close_task(task_id, "skipped", summary="缺少币安 API 凭据，跳过")
            return

        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        ident = uuid4().hex
        if not _acquire_open_lock(r, ident, wait_s=120, ttl_s=_OPEN_LOCK_TTL_S):
            close_task(task_id, "skipped", summary="开仓锁被即时开仓长期占用，本轮跳过")
            return
        try:
            db = SessionLocal()
            try:
                settled = settle_trades(db, trader)
                opened = try_open_trades(db, trader)
            finally:
                db.close()
        finally:
            _release_open_lock(r, ident)
        logger.info("自动交易任务完成：结算/巡检 %d 笔，新开仓 %d 笔", settled, opened)
        close_task(
            task_id, "completed",
            summary=f"结算/巡检 {settled} 笔，新开仓 {opened} 笔",
            detail={"settled": settled, "opened": opened},
        )
    except Exception as e:
        logger.exception("自动交易任务失败: %s", e)
        close_task(task_id, "failed", error=e)


@celery_app.task(name="app.tasks.trade_tasks.open_trade_for_analysis", max_retries=0)
def open_trade_for_analysis(analysis_id: str):
    """单条 AI 分析完成即开仓（2026-09-14：不再等 :42 统一批次）。

    由 ai_tasks 在分析落库后分发；Redis 锁与批量任务互斥，防并发绕过上限/余额
    闸门。等锁超时或闸门未过都无重试——该分析仍满足候选条件，:42 批次会兜底。
    """
    if not settings.TRADING_ENABLED:
        logger.info("即时开仓跳过：TRADING_ENABLED=false（analysis=%s）", analysis_id)
        return
    trader = BinanceTrader()
    if not trader.configured:
        logger.warning("即时开仓跳过：缺少币安 API 凭据（analysis=%s）", analysis_id)
        return
    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = uuid4().hex
    if not _acquire_open_lock(r, ident, wait_s=_OPEN_LOCK_WAIT_S, ttl_s=_OPEN_LOCK_TTL_S):
        logger.info("即时开仓等锁超时，留给 :42 批次（analysis=%s）", analysis_id)
        return
    try:
        db = SessionLocal()
        try:
            ok = try_open_for_analysis(db, trader, analysis_id)
        finally:
            db.close()
        if ok:
            logger.info("即时开仓完成 analysis=%s", analysis_id)
        else:
            logger.info("即时开仓未成交（闸门未过），候选留给 :42 批次（analysis=%s）", analysis_id)
    finally:
        _release_open_lock(r, ident)
