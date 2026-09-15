"""自动交易定时任务（docs/06）

每小时第 10 分钟：settle_trades 结算/跟进在跑单子（TP/SL 事件推进、保本移损、
跟进止损、SL 挂单补挂、强平判定）。开仓不在本任务——2026-09-14 起唯一开仓
通道是即时开仓：AI 分析落库即由 ai_tasks 分发 open_trade_for_analysis
（suggest 且 ≥60 分），与结算共用同一把 Redis 开仓锁串行执行，
防「在跑单上限 / 70% 余额」在并发下被同时绕过。

每次运行写任务记录（含跳过原因），失败进「系统日志」。
"""
import logging
import time
from uuid import UUID, uuid4

from redis import Redis

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import AIAnalysis
from app.services.binance_trader import BinanceTrader
from app.services.task_log import close_task, open_task
from app.services.trade_engine import (
    _mark_open_block,
    settle_trades,
    try_open_for_analysis,
)

logger = logging.getLogger(__name__)

# 开仓互斥锁：结算巡检（分钟级）与即时开仓（秒级）串行
_OPEN_LOCK_KEY = "lock:trade_open"
_OPEN_LOCK_TTL_S = 900       # 结算巡检最长持锁时间兜底（崩溃自动过期）
_OPEN_LOCK_WAIT_S = 45       # 即时开仓等锁上限，超时放弃（无批次兜底）


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


def _mark_analysis_block(analysis_id: str, reason: str) -> None:
    """闸门之外丢败的兜底落因（2026-09-15）：等锁超时 / 任务执行异常同样写
    ai_analyses.open_block_reason——此前这类丢败既无交易记录也无拒开原因，
    前端"开单状态"列显示"-"成为悬案（SENTUSDT 2026-09-15 实例）。尽力而为，
    落因失败仅记日志。"""
    db = SessionLocal()
    try:
        a = db.get(AIAnalysis, UUID(str(analysis_id)))
        if a is not None:
            _mark_open_block(db, a, reason)
    except Exception:
        logger.exception("开仓失败原因兜底落库失败 analysis=%s", analysis_id)
    finally:
        db.close()


@celery_app.task(name="app.tasks.trade_tasks.run_auto_trade_task", max_retries=0)
def run_auto_trade_task():
    task_id = open_task(task_type="trade", task_name="结算巡检", trigger="scheduled")
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
            finally:
                db.close()
        finally:
            _release_open_lock(r, ident)
        logger.info("结算巡检完成：%d 笔", settled)
        close_task(
            task_id, "completed",
            summary=f"结算/巡检 {settled} 笔",
            detail={"settled": settled},
        )
    except Exception as e:
        logger.exception("结算巡检任务失败: %s", e)
        close_task(task_id, "failed", error=e)


@celery_app.task(name="app.tasks.trade_tasks.open_trade_for_analysis", max_retries=0)
def open_trade_for_analysis(analysis_id: str):
    """单条 AI 分析完成即开仓（唯一开仓通道，2026-09-14 起取消 :42 批量兜底）。

    由 ai_tasks 在分析落库后分发；Redis 锁与结算巡检互斥，防并发绕过上限/余额
    闸门。等锁超时或闸门未过即放弃（无重试）——等该币下次信号重新分析再触发。
    闸门之外的丢败（等锁超时 / 执行异常，2026-09-15 起）也落
    ai_analyses.open_block_reason，不留"既没开单也没原因"的悬案。
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
        logger.info("即时开仓等锁超时，放弃本次开仓（analysis=%s）", analysis_id)
        _mark_analysis_block(
            analysis_id,
            f"开仓等锁超时 {_OPEN_LOCK_WAIT_S}s（与结算巡检互斥），本次放弃",
        )
        return
    try:
        try:
            db = SessionLocal()
            try:
                ok = try_open_for_analysis(db, trader, analysis_id)
            finally:
                db.close()
        except Exception as e:
            # 闸门之外的意外（交易所 REST 瞬断等）：任务 max_retries=0 失败即止、
            # 无批次兜底，落因避免悬案（2026-09-15）
            logger.exception("即时开仓任务异常 analysis=%s", analysis_id)
            _mark_analysis_block(analysis_id, f"开仓任务异常：{e}")
            ok = False
        if ok:
            logger.info("即时开仓完成 analysis=%s", analysis_id)
        else:
            logger.info("即时开仓未成交（闸门未过），放弃（analysis=%s）", analysis_id)
    finally:
        _release_open_lock(r, ident)
