from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "trading_workbench",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
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
            "schedule": crontab(minute=0),  # 每小时整点
            "kwargs": {"scan_type": "scheduled"},
        },
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

# 导入任务
celery_app.autodiscover_tasks(["app.tasks"])
