# -*- coding: utf-8 -*-
"""双评委事实包增厚单测（docs/24 D3）。

- _fmt_facts 支持可选 indicator_block / structure_summary 字段：存在时拼
  【指标数据】【摆动结构】块，不存在时保持原有 4 行事实（向后兼容）
- 调用方透传（ai_tasks._build_judge_signal）：调用现场已有的指标块与
  最近摆动高低点摘要注入 signal 副本，评委用数据核对三价与结构位矛盾
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.dual_judge as dual_judge  # noqa: E402
import app.tasks.ai_tasks as ai_tasks  # noqa: E402


def test_fmt_facts_backward_compatible():
    """未注入新字段 → 原有 4 行事实，不含指标/摆动标题（向后兼容）"""
    facts = dual_judge._fmt_facts({
        "symbol": "BTCUSDT", "signal_type": "breakout",
        "current_price": 100.0, "signal_reason": "放量突破",
    })
    assert "币种: BTCUSDT" in facts
    assert "信号类型: breakout" in facts
    assert "当前价格: 100.0" in facts
    assert "【指标数据】" not in facts
    assert "【摆动结构】" not in facts


def test_fmt_facts_with_enriched_fields():
    """注入 indicator_block / structure_summary → 以标题块拼进事实文本"""
    facts = dual_judge._fmt_facts({
        "symbol": "BTCUSDT", "signal_type": "breakout",
        "current_price": 100.0, "signal_reason": "放量突破",
        "indicator_block": "- MACD: 多头（金叉）\n- 指标方向: 看多2项 vs 看空1项",
        "structure_summary": "最近摆动高点 102.5 / 低点 98.2",
    })
    assert "【指标数据】" in facts
    assert "指标方向: 看多2项" in facts
    assert "【摆动结构】" in facts
    assert "最近摆动高点 102.5 / 低点 98.2" in facts
    # 块顺序：指标数据先于摆动结构
    assert facts.index("【指标数据】") < facts.index("【摆动结构】")


def test_fmt_facts_empty_fields_ignored():
    """空串/None 字段视同未传（不产出空标题块）"""
    facts = dual_judge._fmt_facts({
        "symbol": "BTCUSDT", "signal_type": "breakout",
        "current_price": 100.0, "signal_reason": "",
        "indicator_block": "", "structure_summary": None,
    })
    assert "【指标数据】" not in facts
    assert "【摆动结构】" not in facts


def test_run_dual_judge_enriched_facts_reach_prompts(monkeypatch):
    """mock 层面：增厚字段随事实包进入多空评委提示词，裁判含结构核对指令"""
    captured = []

    class _Delta:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content=None, finish=None):
            self.delta = _Delta(content)
            self.finish_reason = finish

    class _Chunk:
        """流式增量块：stream_completion 按 ev.choices[0].delta.content 取正文"""

        def __init__(self, content=None, finish=None):
            self.choices = [_Choice(content, finish)]

    class _Resp:
        """流式增量序列桩（2026-09-30 流式改造后 create 返回可迭代块）"""

        def __init__(self, content):
            self._content = content

        def __iter__(self):
            yield _Chunk(self._content)
            yield _Chunk(None, finish="stop")

    class _Completions:
        @staticmethod
        def create(model=None, messages=None, **kwargs):
            captured.append(messages[0]["content"])
            return _Resp('{"verdict": "keep", "reason": "数据一致"}')

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(dual_judge, "get_client", lambda: _Client())

    decision = {
        "trade_decision": "suggest", "direction": "long", "symbol": "BTCUSDT",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit_1": 104.0,
        "take_profit_2": 106.0, "risk_reward_ratio": 2.0, "position_pct": 5.0,
        "recommendation": 70, "analysis": "测试",
    }
    signal = {
        "symbol": "BTCUSDT", "signal_type": "breakout", "current_price": 100.0,
        "indicator_block": "- 指标方向: 看多3项 vs 看空0项",
        "structure_summary": "最近摆动高点 102.5 / 低点 98.2",
    }
    out = dual_judge.run_dual_judge(decision, signal)
    assert out == decision  # keep 原样返回
    assert len(captured) == 3  # 多头 / 空头 / 裁判
    # 多空评委提示词含增厚事实块（裁判提示词只含辩论，不含事实包，保持原设计）
    for prompt in captured[:2]:
        assert "【指标数据】" in prompt
        assert "最近摆动高点 102.5" in prompt
    # 裁判提示词含结构矛盾核对指令（docs/24 D3）
    assert "三价" in captured[2]
    assert "结构位矛盾" in captured[2]


def test_build_judge_signal_injects_fields(monkeypatch):
    """调用方透传：指标块与最近摆动高低点摘要注入副本，原 signal 不被污染"""
    monkeypatch.setattr(
        ai_tasks, "render_indicator_block",
        lambda k, h, **_kw: "- 指标方向: 看多2项 vs 看空1项",
    )
    signal = {
        "symbol": "BTCUSDT", "signal_type": "breakout", "current_price": 100.0,
        "recent_swings": {
            "highs": [{"label": "HH", "price": 102.5, "bars_ago": 3}],
            "lows": [{"label": "HL", "price": 98.2, "bars_ago": 5}],
        },
    }
    judge_signal = ai_tasks._build_judge_signal(signal, klines=[], htf_klines=None)
    assert judge_signal["indicator_block"] == "- 指标方向: 看多2项 vs 看空1项"
    assert judge_signal["structure_summary"] == "最近摆动高点 102.5 / 低点 98.2"
    # 原 signal 不被污染（增厚字段只存在于副本）
    assert "indicator_block" not in signal
    assert "structure_summary" not in signal


def test_build_judge_signal_without_swings(monkeypatch):
    """无摆动数据 → 只透传指标块（不产出摆动结构行）"""
    monkeypatch.setattr(ai_tasks, "render_indicator_block", lambda k, h, **_kw: "")
    judge_signal = ai_tasks._build_judge_signal({"symbol": "BTCUSDT"}, [], None)
    assert judge_signal["indicator_block"] == ""
    assert "structure_summary" not in judge_signal
