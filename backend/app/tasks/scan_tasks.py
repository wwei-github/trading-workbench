import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.scan import ScanRecord
from app.services.scanner import Scanner

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.scan_tasks.run_scan_task", bind=True, max_retries=0)
def run_scan_task(self, scan_record_id: Optional[str] = None, scan_type: str = "manual"):
    """执行扫描任务

    scan_record_id: 已有的扫描记录 ID（手动触发时传入）
    scan_type: scheduled / manual
    """
    db = SessionLocal()
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
            return

        scanner = Scanner()
        scanner.run(record.id)
    except Exception as e:
        logger.exception("扫描任务失败: %s", e)
    finally:
        db.close()
