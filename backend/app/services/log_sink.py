"""应用日志落库 sink（前端「系统日志」数据源）

- DBLogHandler：WARNING 及以上、app.* 日志器的记录 → 内存队列 → 后台线程批量写 app_logs
- 仅捕获本应用（app.*）日志，排除 uvicorn/celery/httpx 等框架噪音；写库失败静默丢弃（防递归）
- 保留策略：日志 7 天、任务记录 14 天，写线程每 10 分钟清理一次
- API 进程（main.py）与 celery worker（celery_app.py）各自挂载，幂等
"""
import logging
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

_LOG_RETENTION_DAYS = 7
_TASK_RETENTION_DAYS = 14
_CLEANUP_INTERVAL_S = 600
_MESSAGE_MAX_LEN = 4000

_attached = False


class _AppLoggerFilter(logging.Filter):
    """只放行本应用日志器（app.*），框架与第三方库的日志不落库"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("app")


class DBLogHandler(logging.Handler):
    """把 WARNING+ 日志异步入库；emit 永不抛错、不阻塞业务线程"""

    def __init__(self, capacity: int = 2000):
        super().__init__(level=logging.WARNING)
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=capacity)
        self._thread: Optional[threading.Thread] = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                exc = record.exc_info
                if isinstance(exc, BaseException):
                    # py3.9 不归一化 exc_info=异常实例；formatException 只吃元组
                    exc = (type(exc), exc, exc.__traceback__)
                msg += "\n" + "".join(traceback.format_exception(*exc)).rstrip()
            self._q.put_nowait(
                (record.levelname, record.name, msg[:_MESSAGE_MAX_LEN], record.created)
            )
        except Exception:
            pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._writer_loop, name="db-log-sink", daemon=True
        )
        self._thread.start()

    def _writer_loop(self) -> None:
        from app.database import SessionLocal
        from app.models.ops import AppLog, TaskRecord

        last_cleanup = 0.0
        while True:
            batch: list[tuple] = []
            try:
                batch.append(self._q.get(timeout=5))
            except queue.Empty:
                pass
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break

            if batch:
                db = SessionLocal()
                try:
                    db.add_all([
                        AppLog(
                            level=level,
                            logger_name=name[:128],
                            message=msg,
                            created_at=datetime.utcfromtimestamp(created),
                        )
                        for level, name, msg, created in batch
                    ])
                    db.commit()
                except Exception as e:
                    # 落库失败直接丢弃（stderr 输出，不走 logging 防递归）
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    sys.stderr.write(f"[log_sink] 写入 {len(batch)} 条日志失败: {e}\n")
                finally:
                    db.close()

            if time.time() - last_cleanup >= _CLEANUP_INTERVAL_S:
                last_cleanup = time.time()
                db = SessionLocal()
                try:
                    db.query(AppLog).filter(
                        AppLog.created_at
                        < datetime.utcnow() - timedelta(days=_LOG_RETENTION_DAYS)
                    ).delete(synchronize_session=False)
                    db.query(TaskRecord).filter(
                        TaskRecord.started_at
                        < datetime.utcnow() - timedelta(days=_TASK_RETENTION_DAYS)
                    ).delete(synchronize_session=False)
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                finally:
                    db.close()


def attach_db_log_sink() -> None:
    """把 DBLogHandler 挂到 root logger（幂等）"""
    global _attached
    if _attached:
        return
    _attached = True
    handler = DBLogHandler()
    handler.addFilter(_AppLoggerFilter())
    logging.getLogger().addHandler(handler)
    handler.start()
