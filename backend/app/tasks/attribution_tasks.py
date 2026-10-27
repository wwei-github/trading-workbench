# -*- coding: utf-8 -*-
"""每日归因快照任务（docs/16 §21，D6，2026-10-28）。

每天 08:10 北京（UTC 00:10，昨日 UTC 已收口）跑一次：算快照 → 落
`task_records.detail` → 推飞书卡片。只读业务表，唯一写入是任务记录。
"""
import logging
from datetime import datetime, timedelta, timezone

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services import attribution, feishu_bot
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.tasks.attribution_tasks.run_daily_attribution_task")
def run_daily_attribution_task(self, day: str = "") -> dict:
    """day 为空=昨天（UTC）；显式传 'YYYY-MM-DD' 可补跑任意一天。"""
    from datetime import date as _date

    tid = open_task("review", "每日归因快照", trigger="scheduled")
    db = SessionLocal()
    try:
        target = (_date.fromisoformat(day) if day
                  else (datetime.now(timezone.utc).date() - timedelta(days=1)))
        snap = attribution.build_snapshot(db, target)
        tr = (snap.get("trades") or {}).get("total") or {}
        summary = (f"{snap['day']}：平仓 {tr.get('n', 0)} 笔 "
                   f"ΣR {tr.get('sum_r', 0):+.2f} 胜率 {tr.get('win_rate')}%")
        close_task(tid, "completed", summary=summary, detail=snap)
        card_ok = False
        if feishu_bot.configured():
            try:
                card_ok = feishu_bot.send_app_card(attribution.format_card(snap))
            except Exception:
                logger.warning("每日归因快照飞书推送失败", exc_info=True)
        logger.info("每日归因快照完成 %s 卡片=%s", summary, card_ok)
        return {"day": snap["day"], "card": card_ok, "summary": summary}
    except Exception as e:
        close_task(tid, "failed", error=str(e))
        logger.exception("每日归因快照失败")
        raise
    finally:
        db.close()
