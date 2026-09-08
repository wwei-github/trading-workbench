"""Celery worker / beat 启动入口

启动 worker: celery -A celery_worker.celery_app worker --loglevel=info
启动 beat:   celery -A celery_worker.celery_app beat --loglevel=info
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.celery_app import celery_app  # noqa: E402
from app.database import init_db  # noqa: E402

# 启动时建表
init_db()

if __name__ == "__main__":
    celery_app.start()
