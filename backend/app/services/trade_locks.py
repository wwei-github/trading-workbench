"""开仓互斥锁（docs/17 P1 按账号拆分）——共享助手。

结算巡检（celery 任务）、即时开仓、手动平仓、P-09 事件驱动结算（backend
事件流线程）共用同一把账号维度 Redis 锁：key = lock:trade_open:{account_key|default}。

2026-10-07 P-09 从 tasks/trade_tasks.py 原样提取到 services（事件流模块在
services 层，不能反向 import tasks；行为零变化）。
"""
import logging
import time
from typing import Optional

from redis import Redis

logger = logging.getLogger(__name__)

# 结算巡检最长持锁时间兜底（崩溃自动过期）
OPEN_LOCK_TTL_S = 900
# 即时开仓等锁上限，超时放弃（无批次兜底）
OPEN_LOCK_WAIT_S = 45


def open_lock_key(account: Optional[str] = None) -> str:
    return f"lock:trade_open:{account or 'default'}"


def acquire_open_lock(r: Redis, ident: str, wait_s: int, ttl_s: int,
                      account: Optional[str] = None) -> bool:
    key = open_lock_key(account)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if r.set(key, ident, nx=True, ex=ttl_s):
            return True
        time.sleep(0.5)
    return False


def release_open_lock(r: Redis, ident: str, account: Optional[str] = None) -> None:
    try:
        key = open_lock_key(account)
        if r.get(key) == ident:
            r.delete(key)
    except Exception:
        pass
