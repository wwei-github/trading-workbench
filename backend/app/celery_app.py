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
        "app.tasks.daily_report",
        "app.tasks.structure_watch",
        "app.tasks.spot_tasks",
        "app.tasks.shadow_tasks",
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
            # 每小时第 SCAN_BEAT_MINUTE 分 = 整点 :00（2026-10-12 拍板，回退
            # v2026-10-11-4 的 :55 提前捕捉）：等上一根收盘后再看它，不再用
            # 未收盘根快照。分钟数走单一来源 settings.SCAN_BEAT_MINUTE，
            # market_data.FORMING_TAIL_MINUTES 由同一常量派生（2026-10-12 解耦注释）
            "schedule": crontab(minute=settings.SCAN_BEAT_MINUTE),
            "kwargs": {"scan_type": "scheduled"},
        },
        "structure-watch": {
            "task": "app.tasks.structure_watch.run_structure_watch_task",
            # 每小时整点（北京，2026-10-13）：BTC/ETH 1h 结构形态 + 4h 完整性检测；
            # 误差 ≤30s 由任务自监控（lag 落 detail，超限告警），与 hourly-scan /
            # trade-review 同刻 :00（只读轻量、worker 并发 10，K线小时桶缓存共享）
            "schedule": crontab(minute=0),
        },
        "trade-review": {
            "task": "app.tasks.review_tasks.run_trade_review_task",
            # 每小时整点（2026-09-24 定档）：处理超 24h 的 AI 建议决策。
            # 结算巡检 2026-10-21 起为 30min 节奏（:01/:31，docs/06 A2），本任务仍留 :00
            "schedule": crontab(minute=0),
        },
        "ai-attribution": {
            "task": "app.tasks.review_tasks.run_ai_attribution_task",
            "schedule": crontab(minute=14),  # 每小时第14分：结算/建议复盘之后，对已平仓交易做 AI 错误归因
        },
        "patch-trigger": {
            "task": "app.tasks.review_tasks.run_patch_trigger_task",
            "schedule": crontab(minute=40, hour=11),  # 每日 11:40（北京，celery timezone=Asia/Shanghai）：标签热度检查 → 补丁草稿
        },
        "patch-evaluate": {
            "task": "app.tasks.review_tasks.run_patch_evaluate_task",
            "schedule": crontab(minute=45, hour=11),  # 每日 11:45（北京）：active 补丁三态评估
        },
        "auto-trade": {
            "task": "app.tasks.trade_tasks.run_auto_trade_task",
            # 30min 巡检（docs/06 A2，2026-10-21 由每小时 :01 恢复 :01/:31）：
            # 结算/巡检在跑仓位、自动导入手动持仓、AI 持仓诊断；开仓唯一通道 =
            # 分析后即时开仓，每轮批量分析完成后另有链式触发一次（ai_tasks chord
            # 回调，docs/06 A1）。分钟数走单一来源 settings.AUTO_TRADE_BEAT_MINUTES，
            # 改回每小时节奏把该值设为 "1"。
            # :01/:31 而非 :00/:30——等 1h K 线收盘且交易所已落定，避免整点边界上
            # 拉到「上一根仍在形成」的快照导致结构/极值少一根
            "schedule": crontab(minute=settings.AUTO_TRADE_BEAT_MINUTES),
        },
        "pending-entry-sweep": {
            "task": "app.tasks.trade_tasks.watch_pending_entries",
            # 每分钟：挂单入场兜底（docs/27）——成交落地 + 1 小时未成交撤单清除。
            # 与 user-data-stream 实时路径（EVENT_EXEC_ENABLED 开时）双入口，幂等
            "schedule": crontab(minute="*"),
        },
        "daily-report": {
            "task": "app.tasks.daily_report.notify_daily_report",
            # 每日 08:05（北京）：币安日K 08:00 北京收盘后 5 分钟，推主流币收盘价 + 前一日交易统计
            # 注意 timezone=Asia/Shanghai，crontab 的 hour 按北京时间解释，勿写 UTC
            "schedule": crontab(minute=5, hour=8),
        },
        "shadow-evaluate": {
            "task": "app.tasks.shadow_tasks.run_shadow_evaluate_task",
            # 每日 08:00（北京，docs/14 §六）：影子特征分桶评估（复盘归因 join），
            # 结果经 /api/shadow/report 与日报附录输出；评估只统计不决策
            "schedule": crontab(minute=0, hour=8),
        },
        "reentry-backfill": {
            "task": "app.tasks.shadow_tasks.run_reentry_backfill_task",
            # 每日 08:25（北京，docs/13 #12，CARD-07）：出场 48h 观察窗已过的
            # 止损单补打标（结构×续走）；错开 shadow-evaluate 与 08:05 日报
            "schedule": crontab(minute=25, hour=8),
        },
        "spot-daily-check": {
            "task": "app.tasks.spot_tasks.run_spot_daily_task",
            # 现货屯币日检（docs/18 P0，仅评分推送不下单）：日K收盘后 8 分钟，错开 08:05 日报
            "schedule": crontab(minute=8, hour=8),
        },
        "spot-dca": {
            "task": "app.tasks.spot_tasks.run_spot_dca_task",
            # 现货周度定投（P1，docs/18 §6）：周一 08:15，dca_enabled 才执行
            "schedule": crontab(minute=15, hour=8, day_of_week="mon"),
        },
        "spot-weekly-review": {
            "task": "app.tasks.spot_tasks.run_spot_weekly_review_task",
            # AI 周度研判（P1，纯展示无执行权限）：周一 08:30，定投之后
            "schedule": crontab(minute=30, hour=8, day_of_week="mon"),
        },
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
