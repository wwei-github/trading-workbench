# -*- coding: utf-8 -*-
"""扫描任务按套隔离单测（2026-10-14 双周期套，docs/22）

覆盖 run_scan_task 的三处按套隔离（两套共存的核心保障）：
1. running 互斥按 profile：s1 有运行中记录不阻断 s2（允许 s1/s2 并发）
2. Redis 小时锁键含套键：两套同一小时各自持锁、互不抢占（可并发起跑）
3. 按套启用开关早退：该套未启用则建记录后置 failed + skipped（定时/手动一致）

celery 任务用 .apply() 本地执行（不开 broker）；SessionLocal/Scanner/_get_redis/
open_task/close_task 以替身注入，不触网。sqlite 用 StaticPool 让任务内的多次
会话读写同一内存库。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.scan import ScanRecord  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.tasks import ai_tasks, scan_tasks  # noqa: E402


class FakeRedis:
    """SETNX 语义的内存替身，记录所有 lock key 供断言。"""

    def __init__(self):
        self.store: dict = {}
        self.lock_keys: list = []

    def set(self, key, value, nx=False, ex=None):
        self.lock_keys.append(key)
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(scan_tasks, "SessionLocal", Session)

    runs: list = []

    class _FakeScanner:
        def run(self, record_id):
            runs.append(str(record_id))
            db = Session()
            rec = db.get(ScanRecord, record_id)
            rec.status = "completed"
            rec.coin_count = 2
            rec.hit_count = 1
            db.commit()
            db.close()
            return None

    monkeypatch.setattr(scan_tasks, "Scanner", _FakeScanner)

    redis = FakeRedis()
    monkeypatch.setattr(ai_tasks, "_get_redis", lambda: redis)
    monkeypatch.setattr(scan_tasks, "open_task", lambda **kw: "tid")
    monkeypatch.setattr(scan_tasks, "close_task", lambda *a, **kw: None)

    yield SimpleNamespace(session=Session, runs=runs, redis=redis)


def test_running_isolation_allows_concurrent_profiles(env):
    """s1 运行中不阻断 s2；反向 s1 被同套运行中记录拦住。"""
    db = env.session()
    db.add(ScanRecord(scan_type="manual", status="running", profile_key="s1"))
    db.commit()
    db.close()

    # s2 定时扫描：running 查询按套过滤（s1 运行中不影响 s2）→ 正常执行
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s2"})
    assert len(env.runs) == 1
    db = env.session()
    s2 = db.query(ScanRecord).filter(ScanRecord.profile_key == "s2").one()
    assert s2.status == "completed"
    db.close()

    # s1 定时扫描：同套已有运行中记录 → 建记录后置 failed 早退，不再执行
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s1"})
    assert len(env.runs) == 1
    db = env.session()
    s1_new = (
        db.query(ScanRecord)
        .filter(ScanRecord.profile_key == "s1", ScanRecord.scan_type == "scheduled")
        .one()
    )
    assert s1_new.status == "failed"
    db.close()


def test_redis_lock_key_isolated_per_profile(env):
    """两套同一小时各持一把锁（键含套键），均可起跑。"""
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s1"})
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s2"})
    assert len(env.runs) == 2
    keys = env.redis.lock_keys
    assert any(k.startswith("scan:beat:s1:") for k in keys)
    assert any(k.startswith("scan:beat:s2:") for k in keys)
    # 套键段（"scan:beat:<profile>:<bucket>" 的第 3 段）两套互不相同
    assert len({k.split(":")[2] for k in keys}) == 2


def test_disabled_profile_skips(env):
    """s2 关闭 → 定时/手动均早退（置 failed），s1 启用正常执行。"""
    db = env.session()
    db.add(SystemConfig(id=1, scan_profile_s1_enabled=True, scan_profile_s2_enabled=False))
    db.commit()
    db.close()

    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s2"})
    assert env.runs == []
    db = env.session()
    rec = db.query(ScanRecord).filter(ScanRecord.profile_key == "s2").one()
    assert rec.status == "failed"
    db.close()

    # 手动触发同样受开关约束
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "manual", "profile_key": "s2"})
    assert env.runs == []

    # s1 启用 → 正常执行
    scan_tasks.run_scan_task.apply(kwargs={"scan_type": "scheduled", "profile_key": "s1"})
    assert len(env.runs) == 1
