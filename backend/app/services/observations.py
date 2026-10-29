# -*- coding: utf-8 -*-
"""观察期任务服务（docs/23 §3，2026-10-29）

「改一个参数 → 观察 N 天 → 到期决策」的登记与提醒：
- 登记时把判据写死（decision_rule），到期只核对，不做事后合理化；
- 到期扫描挂在每日 08:10 的归因任务末尾（不新增 beat），标 due + 推飞书；
- 只提醒与留痕，绝不自动改配置。

状态机：pending → due → decided；任意时刻可 cancelled。
"""
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.observation import ObservationTask
from app.services import feishu_bot

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DUE = "due"
STATUS_DECIDED = "decided"
STATUS_CANCELLED = "cancelled"

ALL_STATUSES = (STATUS_PENDING, STATUS_DUE, STATUS_DECIDED, STATUS_CANCELLED)


def create(db: Session, *, name: str, days: int = 7,
           config_key: Optional[str] = None, decision_rule: Optional[str] = None,
           metrics: Optional[dict] = None, note: Optional[str] = None,
           due_at: Optional[datetime] = None) -> ObservationTask:
    """登记一条观察期任务。due_at 优先；否则 now + days。"""
    now = datetime.utcnow()
    task = ObservationTask(
        name=name.strip()[:64],
        config_key=(config_key or None),
        started_at=now,
        due_at=due_at or (now + timedelta(days=max(1, min(int(days), 90)))),
        decision_rule=(decision_rule or None),
        metrics=(metrics or None),
        note=(note or None),
        status=STATUS_PENDING,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("观察期任务登记 %s（%s 到期）", task.name, task.due_at)
    return task


def decide(db: Session, task: ObservationTask, decision: str,
           note: Optional[str] = None) -> ObservationTask:
    """记录决策结论（不自动改配置）。"""
    task.status = STATUS_DECIDED
    task.decision = decision
    task.decided_at = datetime.utcnow()
    if note is not None:
        task.note = note
    db.commit()
    db.refresh(task)
    logger.info("观察期任务决策 %s：%s", task.name, decision)
    return task


def check_due(db: Session, now: Optional[datetime] = None) -> list[ObservationTask]:
    """把到期未决策的任务标 due 并推一张飞书卡片；返回本次到期项。

    幂等：只扫 status=pending 且 due_at<=now；已 due 的不重复推（人工补推走 API）。
    """
    now = now or datetime.utcnow()
    rows = db.execute(
        select(ObservationTask)
        .where(ObservationTask.status == STATUS_PENDING,
               ObservationTask.due_at <= now)
        .order_by(ObservationTask.due_at)
    ).scalars().all()
    if not rows:
        return []
    for t in rows:
        t.status = STATUS_DUE
        t.updated_at = now
    db.commit()

    if feishu_bot.configured():
        try:
            feishu_bot.send_app_card(format_due_card(rows))
        except Exception:  # noqa: BLE001
            logger.warning("观察期到期飞书推送失败", exc_info=True)
    logger.info("观察期到期 %d 项：%s", len(rows), "、".join(t.name for t in rows))
    return rows


def counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(ObservationTask.status, func.count())
        .group_by(ObservationTask.status)
    ).all()
    base = {s: 0 for s in ALL_STATUSES}
    base.update({status: int(n) for status, n in rows})
    return base


def _metrics_text(metrics: dict, limit: int = 4) -> str:
    parts = []
    for k, v in list(metrics.items())[:limit]:
        text = str(v)
        if len(text) > 24:
            text = text[:24] + "…"
        parts.append(f"{k}={text}")
    return " · ".join(parts)


def format_due_card(items: Iterable[ObservationTask]) -> dict:
    """到期提醒卡片：名称 / 到期日 / 判据 / 当前读数。"""
    items = list(items)
    lines = []
    for t in items:
        head = f"**{t.name}** · 到期 {t.due_at:%m-%d}"
        if t.config_key:
            head += f"（{t.config_key}）"
        lines.append(head)
        if t.decision_rule:
            lines.append(f"　判据：{t.decision_rule}")
        if t.metrics:
            lines.append(f"　读数：{_metrics_text(t.metrics)}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⏰ 观察期到期 · {len(items)} 项待决策"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "到「任务记录 → 待决策」记录结论；判据按登记时口径核对"}]},
        ],
    }
