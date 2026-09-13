"""自动交易定时任务（docs/06）

每小时第 42 分钟（扫描第 2 分 + AI 分析之后）：
1. settle_trades：先结算/跟进在跑单子，释放额度与保证金
2. try_open_trades：再按评分降序开新仓

每次运行写任务记录（含跳过原因），失败进「系统日志」。
"""
import logging

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.services.binance_trader import BinanceTrader
from app.services.task_log import close_task, open_task
from app.services.trade_engine import settle_trades, try_open_trades

logger = logging.getLogger(__name__)


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

        db = SessionLocal()
        try:
            settled = settle_trades(db, trader)
            opened = try_open_trades(db, trader)
        finally:
            db.close()
        logger.info("自动交易任务完成：结算/巡检 %d 笔，新开仓 %d 笔", settled, opened)
        close_task(
            task_id, "completed",
            summary=f"结算/巡检 {settled} 笔，新开仓 {opened} 笔",
            detail={"settled": settled, "opened": opened},
        )
    except Exception as e:
        logger.exception("自动交易任务失败: %s", e)
        close_task(task_id, "failed", error=e)
