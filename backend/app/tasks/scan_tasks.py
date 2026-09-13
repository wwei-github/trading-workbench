import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.scan import ScanRecord
from app.services.scanner import Scanner
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.scan_tasks.run_scan_task", bind=True, max_retries=0)
def run_scan_task(self, scan_record_id: Optional[str] = None, scan_type: str = "manual"):
    """执行扫描任务

    scan_record_id: 已有的扫描记录 ID（手动触发时传入）
    scan_type: scheduled / manual
    """
    db = SessionLocal()
    task_id = ""
    try:
        if scan_record_id:
            record = db.get(ScanRecord, UUID(scan_record_id))
            if not record:
                logger.error("扫描记录不存在: %s", scan_record_id)
                return
        else:
            # 定时任务：创建新记录
            record = ScanRecord(scan_type=scan_type, status="running")
            db.add(record)
            db.commit()
            db.refresh(record)
            scan_record_id = str(record.id)

        # 任务记录（前端「任务记录」列表）
        task_id = open_task(
            task_type="scan",
            task_name="定时扫描" if scan_type == "scheduled" else "手动扫描",
            trigger=scan_type,
            scan_record_id=scan_record_id,
        )

        # 如果已有正在运行的扫描，跳过
        running = (
            db.query(ScanRecord)
            .filter(ScanRecord.status == "running", ScanRecord.id != record.id)
            .first()
        )
        if running:
            logger.info("已有扫描任务运行中，跳过本次扫描")
            record.status = "failed"
            record.finished_at = datetime.utcnow()
            db.commit()
            close_task(task_id, "skipped", summary="已有扫描任务运行中，跳过")
            return

        scanner = Scanner()
        scanner.run(record.id)

        # scanner.run 用自己的会话收尾，重读最终状态
        db.expire_all()
        record = db.get(ScanRecord, record.id)
        close_task(
            task_id,
            "completed" if record.status == "completed" else "failed",
            summary=f"扫描 {record.coin_count} 个，命中 {record.hit_count} 个",
            detail={
                "coin_count": record.coin_count,
                "hit_count": record.hit_count,
                "error_count": record.error_count,
            },
        )
    except Exception as e:
        logger.exception("扫描任务失败: %s", e)
        close_task(task_id, "failed", error=e)
    finally:
        db.close()
