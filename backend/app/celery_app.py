from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.config import settings
from app.services.log_sink import attach_db_log_sink

attach_db_log_sink()  # worker 进程同样落库 WARNING+ 日志（前端「系统日志」）


@worker_process_init.connect
def _restart_db_log_sink(**_kwargs):
    """prefork 子进程初始化：重启日志写库线程 + 丢弃 fork 继承的数据库连接池。

    线程不随 fork 存活：不重启则 worker 日志全部丢失（「系统日志」看不到 worker 记录）。
    连接池是父进程 import 时建立的：子进程继承同一批 TCP socket，父子并发使用会串包，
    连接被服务端掐断（"server closed the connection unexpectedly"）——04:02 扫描的
    AI 分发任务即因此整批失败。dispose(close=False) 让子进程自建连接、不关父进程的。
    """
    from app.services.log_sink import restart_db_log_sink_after_fork

    restart_db_log_sink_after_fork()
    from app.database import engine

    engine.dispose(close=False)

celery_app = Celery(
    "trading_workbench",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.scan_tasks",
        "app.tasks.ai_tasks",
        "app.tasks.review_tasks",
        "app.tasks.trade_tasks",
        "app.tasks.risk_tasks",
        "app.tasks.daily_report",
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
            "schedule": crontab(minute=10),  # 每小时第10分：与结算巡检同节点（2026-09-15 由每 30 分钟改为每小时一次）
        },
        "ai-attribution": {
            "task": "app.tasks.review_tasks.run_ai_attribution_task",
            "schedule": crontab(minute=14),  # 每小时第14分：结算/建议复盘之后，对已平仓交易做 AI 错误归因
        },
        "patch-trigger": {
            "task": "app.tasks.review_tasks.run_patch_trigger_task",
            "schedule": crontab(minute=40, hour=3),  # 每日 03:40 UTC（北京 11:40）：标签热度检查 → 补丁草稿
        },
        "patch-evaluate": {
            "task": "app.tasks.review_tasks.run_patch_evaluate_task",
            "schedule": crontab(minute=45, hour=3),  # 每日 03:45 UTC（北京 11:45）：active 补丁三态评估
        },
        "auto-trade": {
            "task": "app.tasks.trade_tasks.run_auto_trade_task",
            "schedule": crontab(minute=10),  # 每小时第10分：结算/巡检在跑仓位（开仓唯一通道 = 分析后即时开仓）
        },
        "daily-report": {
            "task": "app.tasks.daily_report.notify_daily_report",
            # 每日 08:05（北京）：币安日K 08:00 北京收盘后 5 分钟，推主流币收盘价 + 前一日交易统计
            # 注意 timezone=Asia/Shanghai，crontab 的 hour 按北京时间解释，勿写 UTC
            "schedule": crontab(minute=5, hour=8),
        },
        "market-risk-check": {
            "task": "app.tasks.risk_tasks.market_risk_check",
            "schedule": crontab(minute="*/30"),  # 每 30 分钟快检（用户拍板 2026-09-20，原设计 5 分钟）
        },
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
