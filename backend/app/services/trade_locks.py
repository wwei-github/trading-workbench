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


# ── 挂单巡检空转短路（2026-10-31）────────────────────────────────────────
# 每轮巡检先读该标志：'0'=无待处理工作，直接返回不查库；'1'/缺失=照常查库。
# 写入口（新建挂单、新建保护事故）置 '1'；巡检处理完为空再置 '0'。
# TTL 既是缓存寿命也是漏置自愈上限（空闲时最多每 15 分钟查一次库）；读写失败
# 一律静默并回退查库，缓存不可用不影响正确性。
PENDING_SWEEP_FLAG_KEY = "pending:sweep:active:v1"
PENDING_SWEEP_FLAG_TTL_S = 900
_FLAG_TIMEOUT_S = 0.5


def pending_sweep_flag_client() -> Redis:
    """标志专用客户端：短超时，避免 Redis 抖动拖住巡检。"""
    from app.config import settings

    return Redis.from_url(
        settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=_FLAG_TIMEOUT_S, socket_timeout=_FLAG_TIMEOUT_S,
    )


def read_pending_sweep_flag(r: Redis) -> Optional[str]:
    """返回 '1'/'0'；Redis 不可用返回 None（调用方按未知处理，照常查库）。"""
    try:
        return r.get(PENDING_SWEEP_FLAG_KEY)
    except Exception:
        return None


def set_pending_sweep_flag(r: Redis, active: bool) -> None:
    try:
        r.set(PENDING_SWEEP_FLAG_KEY, "1" if active else "0",
              ex=PENDING_SWEEP_FLAG_TTL_S)
    except Exception:
        pass


def mark_pending_sweep_active() -> None:
    """写入口打标：新建挂单/保护事故后调用；失败静默，由 TTL 自愈兜底。"""
    try:
        with pending_sweep_flag_client() as r:
            set_pending_sweep_flag(r, True)
    except Exception:
        pass
