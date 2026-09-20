"""市场风险快检任务（docs/15）：每 30 分钟，升级即时执行仓位动作。"""
import logging
from uuid import uuid4

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.risk_tasks.market_risk_check", max_retries=0)
def market_risk_check():
    if not settings.MARKET_GUARD_ENABLED:
        return
    # 与结算巡检共用开仓锁：升级动作要动 SL/市价平仓，禁止与 settle 并发
    from redis import Redis
    from app.tasks.trade_tasks import _acquire_open_lock, _release_open_lock, _OPEN_LOCK_TTL_S
    from app.services.risk_guard import run_risk_check
    from app.services.task_log import close_task, open_task

    task_id = open_task(task_type="guard", task_name="风险快检", trigger="scheduled")
    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = uuid4().hex
    try:
        if not _acquire_open_lock(r, ident, wait_s=30, ttl_s=_OPEN_LOCK_TTL_S):
            close_task(task_id, "skipped", summary="开仓锁被占用，本轮快检跳过")
            return
        try:
            result = run_risk_check()
        finally:
            _release_open_lock(r, ident)
        summary = (
            "数据失败，等级冻结" if result.get("frozen")
            else f"L{result['level']}" + (
                "（升级，已执行动作）" if result.get("upgraded")
                else "（降级）" if result.get("changed") else ""
            )
        )
        close_task(task_id, "completed", summary=summary, detail=result)
    except Exception as e:
        logger.exception("风险快检任务失败: %s", e)
        close_task(task_id, "failed", error=e)
    finally:
        r.close()
