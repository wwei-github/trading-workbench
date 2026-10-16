# -*- coding: utf-8 -*-
"""P1 Agent 工具循环控制流测试（2026-10-16）。

此前唯一测试 test_ai_agent_anchors.py 只覆盖 _resolve_price/_nth_swing 锚点解析，
循环本身（submit 收敛/校验回炉/耗尽兜底/工具分发/异常路径）0 覆盖。本文件用
「流式桩件」回放 tool_calls 序列覆盖这些路径——桩件按 OpenAI 流式增量形状分块
（首块带 id/name、args 分两段），顺带验证 llm_client.stream_completion 的
tool_call 聚合（Agent 主循环已切到流式，见 docs/03 §5.1）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.ai_agent as ai_agent  # noqa: E402
from app.services.ai_agent import _trim_context  # noqa: E402
from app.services.llm_client import stream_completion  # noqa: E402


# ── 桩件：OpenAI 兼容流式假客户端（按序回放预设的 assistant 消息）──

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


_UNSET = object()


class _TC:
    """流式 tool_call 增量块：首块带 index/id/name，后续块只带 arguments 片段。

    index 传 _UNSET（默认不传 index 参数）时不设置 `.index` 属性——模拟非规范网关
    缺失 index 的场景（标准网关恒带 index）。
    """

    def __init__(self, index=_UNSET, id=None, name=None, arguments=None):
        if index is not _UNSET:
            self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, delta, finish=None):
        self.delta = delta
        self.finish_reason = finish


class _Chunk:
    def __init__(self, delta, finish=None):
        self.choices = [_Choice(delta, finish)]


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _Completions:
    def __init__(self, streams, calls):
        self._streams = list(streams)
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self._streams.pop(0))


class _Chat:
    def __init__(self, client):
        self.completions = _Completions(client.streams, client.calls)


class _FakeClient:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []
        self.chat = _Chat(self)


def _msg_stream(content: str = "", tool_calls=(), finish="stop"):
    """把一条 assistant 消息切成流式增量块。

    tool_calls: [(name, args_json_str), ...]；每个调用的 arguments 分两段拼接，
    覆盖 stream_completion 的增量聚合（id/name 首块、args 片段累加）。
    """
    chunks = []
    if content:
        chunks.append(_Chunk(_Delta(content=content)))
    for i, (name, args) in enumerate(tool_calls):
        half = len(args) // 2
        chunks.append(_Chunk(_Delta(tool_calls=[
            _TC(index=i, id=f"call_{i}", name=name, arguments=args[:half])
        ])))
        chunks.append(_Chunk(_Delta(tool_calls=[_TC(index=i, arguments=args[half:])])))
    chunks.append(_Chunk(_Delta(), finish=finish))
    return chunks


def _skip_call(reason: str = "[RR不足]1. 测试理由") -> tuple:
    return ("submit_decision",
            f'{{"trade_decision": "skip", "skip_reason": "{reason}"}}')


@pytest.fixture()
def _stub_pipeline(monkeypatch):
    """重计算桩：只保留循环控制流，屏蔽指标/K线渲染与技能匹配"""
    monkeypatch.setattr(ai_agent, "analyze_ema", lambda _k: None)
    monkeypatch.setattr(ai_agent, "calc_atr", lambda _k: 2.0)
    monkeypatch.setattr(ai_agent, "render_indicator_block",
                        lambda _k, _h=None, **_kw: "")
    monkeypatch.setattr(ai_agent.skill_library, "build_facts", lambda _s, _k: {})
    monkeypatch.setattr(ai_agent.skill_library, "render_index", lambda _f: "")


def _signal() -> dict:
    return {
        "symbol": "TESTUSDT",
        "signal_type": "breakout",
        "current_price": 100.0,
        "recent_swings": {
            "highs": [{"price": 101.0, "label": "HH", "bars_ago": 3}],
            "lows": [{"price": 99.0, "label": "HL", "bars_ago": 2}],
        },
        "pattern": "看涨吞没", "signal_reason": "放量突破",
        "position": "上方", "volume_type": "放量", "volume": 1.5, "strength": 0.8,
    }


def _klines(n: int = 8) -> list:
    return [
        [1700000000000 + i * 3600_000, 100.0, 101.0, 99.0, 100.5, 10.0, 0.0, 1000.0]
        for i in range(n)
    ]


def _run(monkeypatch, client, **kw):
    monkeypatch.setattr(ai_agent, "get_client", lambda: client)
    return ai_agent.analyze_coin_agent(_signal(), _klines(), **kw)


# ── 1. 流式收口：请求带 stream=True + tools，tool_call 增量被正确聚合 ──


def test_stream_completion_tool_calls(monkeypatch, _stub_pipeline):
    """主循环走流式（stream=True）且多块增量聚合出完整工具调用（id/name/args）"""
    client = _FakeClient([
        _msg_stream(tool_calls=[_skip_call()]),
    ])
    _run(monkeypatch, client)

    assert len(client.calls) == 1
    assert client.calls[0]["stream"] is True
    assert client.calls[0]["tools"]  # 工具 schema 已随请求下发


def test_stream_completion_aggregates_directly(monkeypatch):
    """llm_client.stream_completion：分块 tool_call 增量按 index 聚合（单测）"""
    client = _FakeClient([
        _msg_stream(tool_calls=[
            ("get_funding", "{}"),
            ("submit_decision", '{"trade_decision": "skip"}'),
        ]),
    ])
    res = stream_completion(client, [{"role": "user", "content": "hi"}])
    assert [tc["function"]["name"] for tc in res.tool_calls] == [
        "get_funding", "submit_decision",
    ]
    assert res.tool_calls[0]["id"] == "call_0"
    assert res.tool_calls[1]["function"]["arguments"] == '{"trade_decision": "skip"}'
    # P0/双评委不传 tools 时该字段恒空（向后兼容）
    assert stream_completion(
        _FakeClient([_msg_stream(content="纯正文")]), [{"role": "user", "content": "x"}]
    ).tool_calls == ()


# ── 1b. 聚合鲁棒性：非规范网关的增量形状（index 缺失/name 分片/name 重发）──


def test_stream_completion_missing_index_slots_by_id():
    """非规范网关不给 index：按 id 分槽，两个调用不被并成一条、参数不串味"""
    chunks = [
        _Chunk(_Delta(tool_calls=[
            _TC(id="call_a", name="get_funding", arguments='{"a": 1}')
        ])),
        _Chunk(_Delta(tool_calls=[
            _TC(id="call_b", name="submit_decision", arguments='{"trade_decision": "skip"}')
        ])),
        _Chunk(_Delta(), finish="tool_calls"),
    ]
    res = stream_completion(_FakeClient([chunks]), [{"role": "user", "content": "x"}])

    assert [tc["id"] for tc in res.tool_calls] == ["call_a", "call_b"]
    assert [tc["function"]["name"] for tc in res.tool_calls] == [
        "get_funding", "submit_decision",
    ]
    assert res.tool_calls[0]["function"]["arguments"] == '{"a": 1}'
    assert res.tool_calls[1]["function"]["arguments"] == '{"trade_decision": "skip"}'


def test_stream_completion_name_fragmented():
    """name 分片下发：按 SDK 口径字符串累加，拼回完整工具名（不丢调用→unknown tool）"""
    chunks = [
        _Chunk(_Delta(tool_calls=[
            _TC(index=0, id="call_0", name="get_re", arguments='{"n"')
        ])),
        _Chunk(_Delta(tool_calls=[
            _TC(index=0, name="cent_klines", arguments=': 50}')
        ])),
        _Chunk(_Delta(), finish="tool_calls"),
    ]
    res = stream_completion(_FakeClient([chunks]), [{"role": "user", "content": "x"}])

    assert len(res.tool_calls) == 1
    assert res.tool_calls[0]["function"]["name"] == "get_recent_klines"
    assert res.tool_calls[0]["function"]["arguments"] == '{"n": 50}'


def test_stream_completion_name_resent_not_duplicated():
    """网关每块重发已聚合的完整 name：不重复拼接（对照覆盖式写法）"""
    chunks = [
        _Chunk(_Delta(tool_calls=[
            _TC(index=0, id="call_0", name="get_funding", arguments='{"x"')
        ])),
        _Chunk(_Delta(tool_calls=[
            _TC(index=0, name="get_funding", arguments=': 1}')
        ])),
        _Chunk(_Delta(), finish="tool_calls"),
    ]
    res = stream_completion(_FakeClient([chunks]), [{"role": "user", "content": "x"}])

    assert res.tool_calls[0]["function"]["name"] == "get_funding"
    assert res.tool_calls[0]["function"]["arguments"] == '{"x": 1}'


def test_stream_completion_no_id_no_index_continuation():
    """index 与 id 皆缺：按续传归到最近一个槽，一次调用不被拆成多条、参数不碎片"""
    chunks = [
        _Chunk(_Delta(tool_calls=[_TC(name="get_funding", arguments='{"a"')])),
        _Chunk(_Delta(tool_calls=[_TC(arguments=': 1}')])),
        _Chunk(_Delta(), finish="tool_calls"),
    ]
    res = stream_completion(_FakeClient([chunks]), [{"role": "user", "content": "x"}])

    assert len(res.tool_calls) == 1
    assert res.tool_calls[0]["function"]["name"] == "get_funding"
    assert res.tool_calls[0]["function"]["arguments"] == '{"a": 1}'


def test_stream_completion_mixed_keys_no_crash():
    """混合键（部分条目带 index、部分缺 index）：返回处不得因排序崩溃，保持插入序"""
    chunks = [
        _Chunk(_Delta(tool_calls=[
            _TC(index=0, id="call_a", name="get_funding", arguments='{"a": 1}')
        ])),
        _Chunk(_Delta(tool_calls=[
            _TC(id="call_b", name="submit_decision", arguments='{"trade_decision": "skip"}')
        ])),
        _Chunk(_Delta(), finish="tool_calls"),
    ]
    res = stream_completion(_FakeClient([chunks]), [{"role": "user", "content": "x"}])

    assert [tc["id"] for tc in res.tool_calls] == ["call_a", "call_b"]


# ── 2. 首轮 submit 收敛（含 progress round/tool 事件）──


def test_first_round_submit_converges(monkeypatch, _stub_pipeline):
    events: list = []
    client = _FakeClient([_msg_stream(tool_calls=[_skip_call()])])
    out = _run(monkeypatch, client, progress_cb=events.append)

    assert out["trade_decision"] == "skip"
    assert out["skip_reason"].startswith("[RR不足]")
    assert out["stage_trace"]["rounds"] == 1
    assert out["stage_trace"]["tool_calls"] == 1
    assert [e["t"] for e in events] == ["round", "tool"]
    assert events[0]["round"] == 1
    assert events[0]["tools"] == ["submit_decision"]
    assert events[1]["tool"] == "submit_decision"


# ── 3. 校验违规回炉：违规明细作为工具结果回到下一轮上下文 ──


def test_validation_violation_refeeds_context(monkeypatch, _stub_pipeline):
    bad_suggest = (
        "submit_decision",
        '{"trade_decision": "suggest", "direction": "long",'
        ' "trade_type": "trend_follow", "entry_ref": "bogus_ref",'
        ' "stop_ref": "support", "tp1_ref": "resistance", "reason": "x"}',
    )
    client = _FakeClient([
        _msg_stream(tool_calls=[bad_suggest]),
        _msg_stream(tool_calls=[_skip_call("[RR不足]1. 修正后放弃")]),
    ])
    out = _run(monkeypatch, client)

    assert len(client.calls) == 2
    assert out["trade_decision"] == "skip"
    # 第 2 轮的上下文须含第 1 轮的打回明细（模型看着错处改）
    msgs = client.chat.completions.calls[1]["messages"]
    assert any(
        m.get("role") == "tool" and "锚点换算失败" in (m.get("content") or "")
        for m in msgs
    )


# ── 4. 耗尽兜底：全空转轮 → forced skip 且理由带轮次摘要 ──


def test_exhausted_rounds_forced_skip(monkeypatch, _stub_pipeline):
    client = _FakeClient([_msg_stream(content="我不确定") for _ in range(ai_agent.MAX_ROUNDS)])
    out = _run(monkeypatch, client)

    assert len(client.calls) == ai_agent.MAX_ROUNDS
    assert out["trade_decision"] == "skip"
    assert "未提交有效决策" in out["skip_reason"]
    assert "R1 空转未调工具" in out["skip_reason"]
    steps = out["stage_trace"]["steps"]
    assert len(steps) == ai_agent.MAX_ROUNDS
    assert all(s.get("nudge") for s in steps)


# ── 5. 工具异常护栏：畸形参数不再穿出循环 ──


def test_tool_exception_guarded(monkeypatch, _stub_pipeline):
    """get_recent_klines n="abc" → int() ValueError，须转为工具结果文本而非上抛"""
    client = _FakeClient([
        _msg_stream(tool_calls=[("get_recent_klines", '{"n": "abc"}')]),
        _msg_stream(tool_calls=[_skip_call()]),
    ])
    out = _run(monkeypatch, client)

    assert out["trade_decision"] == "skip"  # 未因异常崩掉整轮
    msgs = client.chat.completions.calls[1]["messages"]
    assert any(
        m.get("role") == "tool" and "工具执行异常" in (m.get("content") or "")
        for m in msgs
    )


def test_tool_args_invalid_json_feedback(monkeypatch, _stub_pipeline):
    """参数 JSON 非法 → 明确错误文本回喂（不再静默当 {} 执行）"""
    client = _FakeClient([
        _msg_stream(tool_calls=[("get_funding", "{不是 JSON")]),
        _msg_stream(tool_calls=[_skip_call()]),
    ])
    out = _run(monkeypatch, client)

    assert out["trade_decision"] == "skip"
    msgs = client.chat.completions.calls[1]["messages"]
    assert any(
        m.get("role") == "tool" and "工具参数 JSON 非法" in (m.get("content") or "")
        for m in msgs
    )


# ── 6. 单 submit 语义：一轮仅接受首个成功提交 ──


def test_single_submit_first_wins(monkeypatch, _stub_pipeline):
    client = _FakeClient([
        _msg_stream(tool_calls=[
            _skip_call("[RR不足]1. 第一个"),
            _skip_call("[其他]1. 后到的"),
        ]),
    ])
    out = _run(monkeypatch, client)

    assert out["trade_decision"] == "skip"
    assert "第一个" in out["skip_reason"]
    assert "后到的" not in out["skip_reason"]


# ── 7. 上下文预算裁剪 ──


def test_trim_context_trims_old_tool_results():
    big = "x" * 5000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
    ]
    for i in range(6):
        messages.append({"role": "assistant", "content": f"a{i}"})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": big})

    trimmed = _trim_context(messages, budget=6000, keep_recent=2)
    assert trimmed >= 1
    # 最近 2 条原文保留，较早的被压成头尾摘要
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert tool_msgs[-1]["content"] == big
    assert tool_msgs[-2]["content"] == big
    assert len(tool_msgs[0]["content"]) < len(big)
    assert "已裁剪" in tool_msgs[0]["content"]


def test_trim_context_noop_within_budget():
    messages = [
        {"role": "tool", "tool_call_id": "c0", "content": "small"},
    ]
    assert _trim_context(messages, budget=1000, keep_recent=1) == 0
