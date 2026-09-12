"""共享 LLM 客户端（OpenAI 兼容接口）

模块级单例复用 httpx 连接池：每次分析新建客户端会重复 TLS 握手（100~500ms/次），
Agent 循环 + 双评委多处建连，统一收口到这里。
- timeout：防网关偶发挂起把整轮分析拖死（AI_CALL_TIMEOUT_S）
- max_retries=1：SDK 默认重试 2 次，超时场景最坏 3×timeout（实测出现过 87s 挂死）；
  收窄为 1 次兼顾瞬时抖动容错
"""
from typing import Optional

from openai import OpenAI

from app.config import settings

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
