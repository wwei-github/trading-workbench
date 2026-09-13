"""任务记录读写助手（前端「任务记录」数据源）

任务粒度的运行历史：定时/手动扫描、自动交易（结算+开仓）、建议复盘。
各 celery 任务在开头 _open_task、结束/异常 _close_task，均为独立短会话，
不影响主流程事务；助手自身异常静默（任务记录不可用不能拖垮任务本身）。
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.database import SessionLocal
from app.models.ops import TaskRecord

logger = logging.getLogger(__name__)


def open_task(
    task_type: str,
    task_name: str,
    trigger: str = "system",
    scan_record_id: Optional[str] = None,
) -> str:
    """登记一条 running 任务记录，返回 id"""
    task_id = uuid4()
    db = SessionLocal()
    try:
        db.add(TaskRecord(
            id=task_id,
            task_type=task_type,
            task_name=task_name,
            trigger=trigger,
            status="running",
            scan_record_id=UUID(scan_record_id) if scan_record_id else None,
        ))
        db.commit()
        return str(task_id)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("登记任务记录失败（忽略）: %s", e)
        return ""
    finally:
        db.close()


def close_task(
    task_id: str,
    status: str,
    summary: Optional[str] = None,
    detail: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """收尾任务记录（完成/失败/跳过）；task_id 为空串说明登记失败，忽略"""
    if not task_id:
        return
    db = SessionLocal()
    try:
        rec = db.get(TaskRecord, UUID(task_id))
        if rec:
            rec.status = status
            if summary:
                rec.summary = summary[:250]
            if detail is not None:
                rec.detail = detail
            if error:
                rec.error = str(error)[:2000]
            rec.finished_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("更新任务记录失败（忽略）: %s", e)
    finally:
        db.close()
