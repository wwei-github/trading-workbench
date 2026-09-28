"""共享 LLM 客户端（OpenAI 兼容接口）与 JSON 输出解析工具

模块级单例复用 httpx 连接池：每次分析新建客户端会重复 TLS 握手（100~500ms/次），
Agent 循环 + 双评委多处建连，统一收口到这里。
- timeout：防网关偶发挂起把整轮分析拖死（AI_CALL_TIMEOUT_S）
- max_retries=1：SDK 默认重试 2 次，超时场景最坏 3×timeout（实测出现过 87s 挂死）；
  收窄为 1 次兼顾瞬时抖动容错

parse_llm_json（2026-09-28）：LLM 输出 JSON 偶发语法瑕疵（字符串内未转义引号/
尾逗号/截断），标准 json.loads 失败后用 json_repair 库修复——线上实测
"Expecting ',' delimiter" 类告警即此类，修复后无需整轮重试 LLM。
"""
import json
import logging
from typing import Any, Optional

from json_repair import repair_json
from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# Optional 写法兼容 Python 3.9（PEP 604 的 X | Y 运行时求值需 3.10+）
_client: Optional[OpenAI] = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.AI_API_KEY,
            base_url=settings.AI_BASE_URL,
            timeout=settings.AI_CALL_TIMEOUT_S,
            max_retries=1,
        )
    return _client


def parse_llm_json(content: str) -> Any:
    """解析 LLM 输出 JSON：标准解析失败时降级 json_repair 修复。

    修复仍失败（极端截断）抛 json.JSONDecodeError，由调用方决定重试/降级。
    返回 None 视为修复出了空结果，同样按失败抛——不能让静默空对象
    混进后续 validate（会被当成缺字段的合法决策）。
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        repaired = repair_json(content, return_objects=True)
        if repaired is None or repaired == "":
            raise e
        logger.warning("LLM 输出 JSON 标准解析失败（%s），已修复", str(e)[:120])
        return repaired
