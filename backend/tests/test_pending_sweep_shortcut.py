# -*- coding: utf-8 -*-
"""挂单巡检空转短路（2026-10-31）：空闲轮次不查库、写入口解除短路。"""
from app.services import trade_locks
from app.tasks import trade_tasks


class _FakeRedis:
    """最小 Redis 替身：只覆盖 get/set/close + with。"""

    def __init__(self, value=None):
        self.value = value
        self.closed = False
        self.set_calls = []

    def get(self, _key):
        return self.value

    def set(self, _key, value, ex=None):
        self.value = value
        self.set_calls.append((value, ex))

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def execute(self, _stmt):
        return _FakeResult([])

    def close(self):
        pass


def test_idle_flag_zero_skips_db(monkeypatch):
    """标志为 '0' → 直接返回，连 SessionLocal 都不创建。"""
    fake = _FakeRedis(value="0")
    monkeypatch.setattr(trade_tasks, "pending_sweep_flag_client", lambda: fake)
    monkeypatch.setattr(
        trade_tasks, "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("不应查库")),
    )

    trade_tasks.watch_pending_entries()

    assert fake.closed is True
    assert fake.set_calls == []


def test_empty_db_sets_idle_flag(monkeypatch):
    """无挂单/无事故 → 查一次库，之后置 '0' 供下一轮短路。"""
    fake = _FakeRedis(value=None)
    monkeypatch.setattr(trade_tasks, "pending_sweep_flag_client", lambda: fake)
    monkeypatch.setattr(trade_tasks, "SessionLocal", _FakeDB)

    trade_tasks.watch_pending_entries()

    assert fake.set_calls == [("0", trade_locks.PENDING_SWEEP_FLAG_TTL_S)]
    assert fake.closed is True


def test_writer_marks_active(monkeypatch):
    """新建挂单/保护事故的写入口把标志置 '1'。"""
    fake = _FakeRedis(value="0")
    monkeypatch.setattr(trade_locks, "pending_sweep_flag_client", lambda: fake)

    trade_locks.mark_pending_sweep_active()

    assert fake.set_calls == [("1", trade_locks.PENDING_SWEEP_FLAG_TTL_S)]
    assert fake.closed is True
