# -*- coding: utf-8 -*-
"""stream_completion 流式调用单测（2026-09-30 网关挂死治理）

背景：GLM 推理模型非流式请求长推理期间零字节返回，读超时把「正在推理」
误杀成「网关挂死」（线上同一分析请求 4 连挂 240s 实测）。流式下 reasoning
增量持续到达保持 socket 活跃，不再触发读超时。

覆盖：
1. 正文增量拼接 + finish_reason 捕获 + 首 token 耗时记录
2. reasoning_content 增量不混入正文（推理过程与正文隔离）
3. 无 choices 的元数据块 / delta 为 None 的终止块安全跳过
4. markdown 包裹由 _call_llm 剥离后正常解析（流式桩端到端）
5. 全程无正文（纯 reasoning 耗尽）→ 空内容异常路径
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.ai_analyzer as ai_analyzer  # noqa: E402
from app.services.llm_client import stream_completion  # noqa: E402


class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning


class _Choice:
    def __init__(self, delta=None, finish=None):
        self.delta = delta
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choice=None):
        self.choices = [choice] if choice else []


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, model=None, messages=None, **kwargs):
        assert kwargs.get("stream") is True, "必须以流式发起请求"
        self.kwargs = kwargs
        return iter(self._chunks)


class _FakeChat:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.chat = _FakeChat(chunks)


def test_stream_accumulates_content_and_finish():
    """正文增量拼接 + finish_reason + 首 token 耗时被记录"""
    client = _FakeClient([
        _Chunk(),
        _Chunk(_Choice(_Delta(content='{"a"'))),
        _Chunk(_Choice(_Delta(content=": 1}"))),
        _Chunk(_Choice(None, finish="stop")),
    ])
    r = stream_completion(client, [{"role": "user", "content": "x"}])
    assert r.content == '{"a": 1}'
    assert r.finish_reason == "stop"
    assert 0 <= r.first_token_s <= r.total_s


def test_stream_reasoning_content_excluded():
    """reasoning_content 增量（推理过程）不混入正文"""
    client = _FakeClient([
        _Chunk(_Choice(_Delta(reasoning="先想一下..."))),
        _Chunk(_Choice(_Delta(reasoning="再想一下..."))),
        _Chunk(_Choice(_Delta(content="答案"))),
        _Chunk(_Choice(None, finish="stop")),
    ])
    r = stream_completion(client, [{"role": "user", "content": "x"}])
    assert r.content == "答案"
    assert "想" not in r.content


def test_stream_empty_content_first_token_negative():
    """纯 reasoning 耗尽（无正文增量）→ content 空 + first_token_s=-1"""
    client = _FakeClient([
        _Chunk(_Choice(_Delta(reasoning="只有推理没正文"))),
        _Chunk(_Choice(None, finish="length")),
    ])
    r = stream_completion(client, [{"role": "user", "content": "x"}])
    assert r.content == ""
    assert r.first_token_s == -1.0
    assert r.finish_reason == "length"


def test_call_llm_strips_markdown_and_parses(monkeypatch):
    """流式桩端到端：markdown 包裹剥离 + JSON 解析（_call_llm 主路径）"""
    client = _FakeClient([
        _Chunk(_Choice(_Delta(content="```json\n{\"verdict\": \"keep\"}\n```"))),
        _Chunk(_Choice(None, finish="stop")),
    ])
    out = ai_analyzer._call_llm(client, [{"role": "user", "content": "x"}])
    assert out == {"verdict": "keep"}


def test_call_llm_empty_content_raises(monkeypatch):
    """空正文（finish=length）抛 _LLMOutputError，走重试反馈链"""
    client = _FakeClient([
        _Chunk(_Choice(_Delta(reasoning="推理耗尽全部配额"))),
        _Chunk(_Choice(None, finish="length")),
    ])
    with pytest.raises(ai_analyzer._LLMOutputError) as ei:
        ai_analyzer._call_llm(client, [{"role": "user", "content": "x"}])
    assert "空内容" in str(ei.value)
