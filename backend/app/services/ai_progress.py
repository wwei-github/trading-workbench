"""AI 分析进度事件（docs/04 P2+）：Redis list 存储逐事件追加，TTL 1h。

celery worker 推送 → backend API 读取 → 前端每 2s 轮询展示"流式"分析过程。
事件类型：start / gate / round / tool / submit / done / error。
"""
import json
import logging
import time
from typing import Optional
from uuid import UUID

from redis import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_TTL_S = 3600


def _key(scan_result_id) -> str:
    return f"ai:progress:{scan_result_id}"


def push(scan_result_id, ev_type: str, **kw) -> None:
    """追加一条进度事件（任何异常静默失败——进度展示不能影响主流程）"""
    try:
        c = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        ev = {"t": ev_type, "ts": int(time.time() * 1000), **kw}
        c.rpush(_key(scan_result_id), json.dumps(ev, ensure_ascii=False))
        c.expire(_key(scan_result_id), _TTL_S)
    except Exception as e:
        logger.debug("进度事件推送失败: %s", e)


def get_events(scan_result_id) -> list[dict]:
    try:
        c = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        raw = c.lrange(_key(scan_result_id), 0, -1)
        return [json.loads(x) for x in raw]
    except Exception:
        return []


def clear(scan_result_id) -> None:
    try:
        Redis.from_url(settings.REDIS_URL, decode_responses=True).delete(
            _key(scan_result_id)
        )
    except Exception:
        pass


def push_start(scan_result_id: UUID, symbol: str, pipeline: str) -> None:
    push(scan_result_id, "start", symbol=symbol, pipeline=pipeline)


def push_done(scan_result_id, decision: Optional[str], note: str = "") -> None:
    push(scan_result_id, "done", decision=decision, note=note)
