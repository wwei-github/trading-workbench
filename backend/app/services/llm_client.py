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
import time
from typing import Any, NamedTuple, Optional

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


class StreamResult(NamedTuple):
    """stream_completion 的返回：拼接正文 + 延迟观测（首 token / 总耗时）"""
    content: str
    finish_reason: Optional[str]
    first_token_s: float  # 首个正文增量到达耗时；-1 = 全程无正文（纯 reasoning 耗尽）
    total_s: float


def stream_completion(
    client: OpenAI, messages: list, *,
    temperature: float = 0.3, max_tokens: int = 32000, **extra,
) -> StreamResult:
    """流式调用 chat/completions 并拼接正文（2026-09-30 网关挂死治理）。

    背景：GLM 系列推理模型的**非流式**请求在长推理期间零字节返回，读超时
    （AI_CALL_TIMEOUT_S）把「正在正确推理」误杀成「网关挂死」——线上实测同一
    分析请求 4 连挂满 240s 而小请求 2s 正常；thinking 关闭参数被腾讯 MaaS 拒绝
    （原生 thinking 字段 400、enable_thinking 静默忽略），流式是唯一可行解。
    流式下 reasoning 增量 ~2s 内开始持续到达（tokenhub 实测 979 chunks），
    socket 活跃即不触发读超时；max_tokens 仍是总生成量硬上界。

    reasoning_content 增量对调用方不可见（正文只拼 delta.content，与推理过程隔离）；
    首个正文增量耗时与总耗时随结果携带，供调用方打延迟观测日志。
    """
    t0 = time.monotonic()
    first_at: Optional[float] = None
    parts: list = []
    finish: Optional[str] = None
    stream = client.chat.completions.create(
        model=settings.AI_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        **extra,
    )
    for ev in stream:
        if not getattr(ev, "choices", None):
            continue  # 网关可能先发无 choices 的元数据块
        choice = ev.choices[0]
        delta = getattr(choice, "delta", None)
        piece = (getattr(delta, "content", None) or "") if delta is not None else ""
        if piece:
            if first_at is None:
                first_at = time.monotonic() - t0
            parts.append(piece)
        if choice.finish_reason:
            finish = choice.finish_reason
    return StreamResult(
        "".join(parts).strip(),
        finish,
        first_at if first_at is not None else -1.0,
        time.monotonic() - t0,
    )


def json_mode_kwargs() -> dict:
    """JSON 输出模式的额外请求参数（docs/24 D2）。

    各 chat/completions 调用处用 `**json_mode_kwargs()` 挂接：
    LLM_JSON_MODE 开启时返回 response_format=json_object（OpenAI 兼容），
    关闭时返回空 dict（请求参数零变化）。默认关闭：部分 OpenAI 兼容网关
    不支持该参数（直接 400），按部署网关能力决定是否开启。
    """
    if settings.LLM_JSON_MODE:
        return {"response_format": {"type": "json_object"}}
    return {}


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
