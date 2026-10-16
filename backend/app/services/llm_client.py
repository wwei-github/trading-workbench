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
# 已建客户端的 (base_url, timeout) 指纹：不一致即重建（模型名不参与，调用侧逐次传）
_client_key: tuple = ()

# ── AI 运行时参数覆盖（2026-10-14 上页面）──
# system_config 的 ai_model/ai_base_url/llm_concurrency/ai_call_timeout_s
# （列 NULL=回落 env）。各 LLM 任务入口调 configure_from(cfg) 每轮重读 DB 刷新；
# 未调用时全部回落 env（老路径零改动）。进程内模块级——celery worker 各自持有一份。
_runtime: dict = {}


def configure_from(cfg=None) -> None:
    """从 system_config 行刷新运行时 AI 参数（cfg 列 NULL/空 = 回落 env 默认）。

    cfg 为 None（未读到配置）时清空覆盖、全部回落 env，避免串用上一轮的旧值。
    """
    _runtime["model"] = (getattr(cfg, "ai_model", None) or "") if cfg is not None else ""
    _runtime["base_url"] = (getattr(cfg, "ai_base_url", None) or "") if cfg is not None else ""
    _runtime["concurrency"] = getattr(cfg, "llm_concurrency", None) if cfg is not None else None
    _runtime["timeout"] = getattr(cfg, "ai_call_timeout_s", None) if cfg is not None else None


def current_model() -> str:
    """生效模型名（DB > env）"""
    return _runtime.get("model") or settings.AI_MODEL


def current_base_url() -> str:
    """生效接口端点（DB > env）"""
    return _runtime.get("base_url") or settings.AI_BASE_URL


def current_timeout() -> int:
    """生效单次调用超时秒（DB > env）"""
    v = _runtime.get("timeout")
    return int(v) if v else settings.AI_CALL_TIMEOUT_S


def current_concurrency() -> int:
    """生效 LLM 并发上限（DB > env）"""
    v = _runtime.get("concurrency")
    return int(v) if v else settings.LLM_CONCURRENCY


def get_client() -> OpenAI:
    global _client, _client_key
    key = (current_base_url(), current_timeout())
    if _client is None or _client_key != key:
        # base_url/timeout 参与连接池与超时策略，变更即重建（否则运行时改动不生效）
        _client = OpenAI(
            api_key=settings.AI_API_KEY,
            base_url=key[0],
            timeout=key[1],
            max_retries=1,
        )
        _client_key = key
    return _client


class StreamResult(NamedTuple):
    """stream_completion 的返回：拼接正文 + 延迟观测（首 token / 总耗时）+ 聚合工具调用

    tool_calls（2026-10-16）：流式 tool_call 增量按 index 聚合后的 OpenAI 消息形状
    列表（每项 {"id","type","function":{"name","arguments"}}），供 Agent 工具循环
    直接回填 assistant 消息。P0（单次调用）与双评委不传 tools，该字段恒为空元组，
    属纯新增字段，调用方按 4 个位置参数构造即向后兼容。
    """
    content: str
    finish_reason: Optional[str]
    first_token_s: float  # 首个正文增量到达耗时；-1 = 全程无正文（纯 reasoning 耗尽）
    total_s: float
    tool_calls: tuple = ()


def stream_completion(
    client: OpenAI, messages: list, *,
    temperature: float = 0.3, max_tokens: int = 32000,
    on_progress: Optional[Any] = None, **extra,
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

    tool_calls（2026-10-16）：流式 tool_call 增量按 `delta.tool_calls[].index` 聚合
    ——首块带 id/name、后续块只带 arguments 片段，逐块拼接成完整工具调用（与
    非流式 message.tool_calls 同形状），随 StreamResult.tool_calls 返回。这是全仓
    LLM 调用统一收口点，Agent 工具循环因此无需另起非流式 create（消除流式治理的
    唯一例外）。

    on_progress: 可选回调 dict(reasoning_chars/content_chars/elapsed_s)，
    首个增量立即触发、此后 ≥2s 节流——供分析管线把活进度推给前端
    （ai_progress 事件流），让用户看到「推理中」而非干等。
    """
    t0 = time.monotonic()
    first_at: Optional[float] = None
    parts: list = []
    finish: Optional[str] = None
    n_reasoning = 0
    n_content = 0
    last_pub = -10.0  # 首个增量必触发（节流窗口外）
    tool_calls: dict = {}  # index -> {"id","type","function":{"name","arguments"}}
    stream = client.chat.completions.create(
        model=current_model(),
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
            n_content += len(piece)
        n_reasoning += len((getattr(delta, "reasoning_content", None) or "")) if delta is not None else 0
        for tcd in ((getattr(delta, "tool_calls", None) or []) if delta is not None else []):
            idx = getattr(tcd, "index", None)
            tcd_id = getattr(tcd, "id", None)
            # 槽位键：标准网关给 index；缺失时（非规范网关）退化为按 id 分槽——
            # 若一律回落 0，多个调用会被并成一条、arguments 串味（丢调用 + 参数垃圾）
            if idx is not None:
                key = int(idx)
            elif tcd_id:
                key = ("id", tcd_id)
            else:
                key = ("n", len(tool_calls))
            slot = tool_calls.setdefault(
                key, {"id": "", "type": "function",
                      "function": {"name": "", "arguments": ""}}
            )
            if tcd_id:
                slot["id"] = tcd_id
            fn = getattr(tcd, "function", None)
            if fn is not None:
                nm = getattr(fn, "name", None)
                if nm:
                    cur = slot["function"]["name"]
                    # 名称按 SDK 口径做字符串累加（可能分片下发）；网关重复发同名/全名时
                    # 不重复拼接（nm 是已聚合名的完整重发 → 直接覆盖）
                    if nm != cur:
                        slot["function"]["name"] = (
                            nm if not cur or nm.startswith(cur) else cur + nm
                        )
                if getattr(fn, "arguments", None):
                    slot["function"]["arguments"] += fn.arguments
        if choice.finish_reason:
            finish = choice.finish_reason
        if on_progress is not None:
            now = time.monotonic()
            if now - last_pub >= 2.0:
                last_pub = now
                on_progress({
                    "reasoning_chars": n_reasoning,
                    "content_chars": n_content,
                    "elapsed_s": round(now - t0, 1),
                })
    return StreamResult(
        "".join(parts).strip(),
        finish,
        first_at if first_at is not None else -1.0,
        time.monotonic() - t0,
        # 按插入顺序（dict 保序）：index 缺失时 key 退化为 ("id", …)/("n", …) 元组，
        # 与 int 混合后 sorted() 会 TypeError；标准网关 index 递增，插入序即索引序
        tuple(tool_calls.values()),
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
