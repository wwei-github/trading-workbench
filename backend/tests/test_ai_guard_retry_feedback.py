# -*- coding: utf-8 -*-
"""AI 分析重试链解析失败反馈 + JSON 输出模式单测（docs/24 D2）。

- 解析失败（空内容抛异常 / json-repair 后仍非法重抛 JSONDecodeError）重试时
  须把错误摘要附进上下文（与校验违规反馈同模式），模型看着错处改而非盲重发
- LLM_JSON_MODE 开关对 chat/completions 请求参数的影响：开启带
  response_format={"type":"json_object"}，关闭请求参数零变化（网关兼容）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.ai_analyzer as ai_analyzer  # noqa: E402
import app.services.dual_judge as dual_judge  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.llm_client import json_mode_kwargs  # noqa: E402


# ── 桩件：OpenAI 兼容假客户端（记录每次调用参数，按序回放预设返回内容）──
# 2026-09-30 流式改造后 create(stream=True) 返回增量块序列，桩件按此形状回放

class _FakeDelta:
    def __init__(self, content):
        self.content = content


class _FakeStreamChoice:
    def __init__(self, content=None, finish=None):
        self.delta = _FakeDelta(content)
        self.finish_reason = finish


class _FakeChunk:
    def __init__(self, content=None, finish=None):
        self.choices = [_FakeStreamChoice(content, finish)]


class _FakeStreamResp:
    """单段正文块 + 终止块（模拟网关流式增量）"""

    def __init__(self, content):
        self._content = content

    def __iter__(self):
        yield _FakeChunk(self._content)
        yield _FakeChunk(None, finish="stop")


class _FakeCompletions:
    def __init__(self, client):
        self._client = client

    def create(self, model=None, messages=None, **kwargs):
        client = self._client
        client.calls.append({"messages": messages, "kwargs": kwargs})
        return _FakeStreamResp(client.contents.pop(0))


class _FakeChat:
    def __init__(self, client):
        self.completions = _FakeCompletions(client)


class _FakeClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []
        self.chat = _FakeChat(self)


@pytest.fixture()
def _stub_build_messages(monkeypatch):
    """_build_messages 重计算桩（K线指标/技能匹配），测试只关注重试链本身"""
    monkeypatch.setattr(ai_analyzer, "analyze_ema", lambda _k: None)
    monkeypatch.setattr(ai_analyzer, "render_indicator_block", lambda _k, _h: "")
    monkeypatch.setattr(ai_analyzer.skill_library, "build_facts", lambda s, k: {})
    monkeypatch.setattr(ai_analyzer.skill_library, "render_matched", lambda _f: "")


def _signal():
    return {
        "symbol": "TESTUSDT",
        "signal_type": "breakout",
        "current_price": 100.0,
        "breakout_pct": 0.5,
        "pattern": "看涨吞没",
        "signal_reason": "放量突破前高",
        "position": "上方",
        "volume_type": "放量",
        "volume": 1.5,
        "volume_24h": 5_000_000,
    }


def _klines():
    """最简 8 列K线（_build_messages 只取前 8 列）"""
    return [
        [1700000000000 + i * 3600_000, 100.0, 101.0, 99.0, 100.5, 10.0, 0.0, 1000.0]
        for i in range(3)
    ]


def test_json_mode_kwargs_toggle(monkeypatch):
    """开关直测：开启返回 response_format 参数，关闭返回空 dict（请求零变化）"""
    monkeypatch.setattr(settings, "LLM_JSON_MODE", True)
    assert json_mode_kwargs() == {"response_format": {"type": "json_object"}}
    monkeypatch.setattr(settings, "LLM_JSON_MODE", False)
    assert json_mode_kwargs() == {}


def test_json_mode_wired_into_main_analysis(monkeypatch, _stub_build_messages):
    """LLM_JSON_MODE 开启 → 主分析请求带 response_format；关闭 → 请求无该键"""
    monkeypatch.setattr(settings, "LLM_JSON_MODE", True)
    client = _FakeClient(['{"trade_decision": "skip", "skip_reason": "1. 测试"}'])
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda: client)
    ai_analyzer.analyze_with_guard(_signal(), _klines())
    assert client.calls[0]["kwargs"].get("response_format") == {"type": "json_object"}

    monkeypatch.setattr(settings, "LLM_JSON_MODE", False)
    client2 = _FakeClient(['{"trade_decision": "skip", "skip_reason": "1. 测试"}'])
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda: client2)
    ai_analyzer.analyze_with_guard(_signal(), _klines())
    assert "response_format" not in client2.calls[0]["kwargs"]


def test_parse_failure_appends_error_feedback(monkeypatch, _stub_build_messages):
    """首次输出不可解析 → 第2次调用 messages 须含解析失败错误反馈（docs/24 D2）

    覆盖「json-repair 修复后仍非法」路径：不可修复文本经 parse_llm_json
    重抛原 JSONDecodeError，进入解析失败分支。
    """
    client = _FakeClient([
        "这不是 JSON 输出",  # 第1次：标准解析失败且修复无果 → 抛原异常
        '{"trade_decision": "skip", "skip_reason": "1. 测试理由", "recommendation": 10}',
    ])
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda: client)

    result = ai_analyzer.analyze_with_guard(_signal(), _klines())

    assert len(client.calls) == 2
    retry_msgs = client.calls[1]["messages"]
    feedback = [
        m for m in retry_msgs if "上一次输出 JSON 解析失败" in (m.get("content") or "")
    ]
    assert len(feedback) == 1
    assert feedback[0]["role"] == "user"
    assert "请严格只输出合法 JSON 对象" in feedback[0]["content"]
    # 上次输出非空时以 assistant 消息回放进上下文（模型看着原文改错，docs/24 D2）
    assert any(
        (m.get("role") == "assistant" and "这不是 JSON 输出" in (m.get("content") or ""))
        for m in retry_msgs
    )
    # 反馈后重试成功 → 返回该决策而非强制 skip
    assert result["trade_decision"] == "skip"
    assert result["skip_reason"] == "1. 测试理由"


def test_parse_failure_empty_content_also_feedback(monkeypatch, _stub_build_messages):
    """覆盖「直接抛异常」路径：空内容 ValueError 同样产生错误反馈"""
    client = _FakeClient([
        "",  # 第1次：空内容 → ValueError("AI 返回空内容 ...")
        '{"trade_decision": "skip", "skip_reason": "1. 测试理由"}',
    ])
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda: client)

    result = ai_analyzer.analyze_with_guard(_signal(), _klines())

    feedback = [
        m for m in client.calls[1]["messages"]
        if "上一次输出 JSON 解析失败" in (m.get("content") or "")
    ]
    assert len(feedback) == 1
    assert "空内容" in feedback[0]["content"]
    # 空内容无原文可回放，不得出现空 assistant 消息
    assert not any(
        m.get("role") == "assistant" and not (m.get("content") or "").strip()
        for m in client.calls[1]["messages"]
    )
    assert result["trade_decision"] == "skip"


def test_parse_failure_exhausted_forces_skip(monkeypatch, _stub_build_messages):
    """3 次全不可解析 → 强制 skip，且每次重试前都附了错误反馈"""
    client = _FakeClient(["这不是 JSON 输出"] * 3)
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda: client)

    result = ai_analyzer.analyze_with_guard(_signal(), _klines())

    assert len(client.calls) == 3
    assert result["trade_decision"] == "skip"
    assert "无法解析" in result["skip_reason"]
    for call in client.calls[1:]:
        assert any(
            "上一次输出 JSON 解析失败" in (m.get("content") or "")
            for m in call["messages"]
        )


def test_dual_judge_json_mode_only_on_judge_call(monkeypatch):
    """JSON 模式只挂裁判调用（多空评委输出论据正文，不能强制 JSON）"""
    monkeypatch.setattr(settings, "LLM_JSON_MODE", True)
    client = _FakeClient(["多头论据", "空头论据", '{"verdict": "keep", "reason": "ok"}'])
    monkeypatch.setattr(dual_judge, "get_client", lambda: client)

    decision = {
        "trade_decision": "suggest", "direction": "long", "symbol": "TESTUSDT",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 104.0,
        "take_profit_2": 0, "risk_reward_ratio": 2.0, "position_pct": 5.0,
        "recommendation": 70, "analysis": "测试",
    }
    out = dual_judge.run_dual_judge(decision, {"symbol": "TESTUSDT"})
    assert out == decision  # keep 原样返回
    assert len(client.calls) == 3
    assert "response_format" not in client.calls[0]["kwargs"]
    assert "response_format" not in client.calls[1]["kwargs"]
    assert client.calls[2]["kwargs"].get("response_format") == {"type": "json_object"}
