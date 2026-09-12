from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "trading_workbench",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.scan_tasks",
        "app.tasks.ai_tasks",
        "app.tasks.review_tasks",
        "app.tasks.trade_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    beat_schedule={
        "hourly-scan": {
            "task": "app.tasks.scan_tasks.run_scan_task",
            "schedule": crontab(minute=2),  # 每小时第2分钟执行，等待K线收盘
            "kwargs": {"scan_type": "scheduled"},
        },
        "trade-review": {
            "task": "app.tasks.review_tasks.run_trade_review_task",
            "schedule": crontab(minute="*/30"),  # 每 30 分钟扫一次到期建议
        },
        "auto-trade": {
            "task": "app.tasks.trade_tasks.run_auto_trade_task",
            "schedule": crontab(minute=42),  # 每小时第42分：扫描(02分)+AI分析后，先结算后开仓
        },
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
