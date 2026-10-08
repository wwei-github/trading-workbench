"""TP2 分批止盈开关（2026-10-08）——关闭时 AI 不产出第二档止盈价。

三层防线各配回归：
1. P0/P1 系统提示词剔除止盈二规则与字段说明（render_system_prompt）
2. Agent submit_decision 工具 schema 隐藏 tp2_ref/tp2_offset_pct（_tools_schema）
3. 输出硬清零：Agent _handle_submit 不解析 tp2 锚点（P0 校验前清零同源逻辑）
"""

import pytest

from app.config import settings
from app.services.ai_agent import (
    _handle_submit,
    _tools_schema,
    render_system_prompt as render_agent_prompt,
)
from app.services.ai_analyzer import (
    render_system_prompt as render_p0_prompt,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5)
    monkeypatch.setattr(settings, "RISK_BUDGET_PCT", 3.0)


# ── 1. 提示词开关 ────────────────────────────────────────────────────


def test_p0_prompt_default_includes_tp2():
    """默认（tp2_enabled=True）：止盈二规则与字段说明在位"""
    p = render_p0_prompt()
    assert "止盈二：止盈一锚定档的下一档结构位外侧" in p
    assert "第二档止盈价，锚定更前方一档摆动结构位" in p


def test_p0_prompt_tp2_off_excludes():
    """关闭：提示词剔除止盈二条目，字段说明改为要求输出 0"""
    p = render_p0_prompt(tp2_enabled=False)
    assert "一律输出 0" in p
    assert "止盈二：止盈一锚定档" not in p
    assert "止盈二设 0（仅设一档）" not in p


def test_agent_prompt_default_includes_tp2():
    """默认：Agent 提示词含止盈二锚点语义（tp2_ref=next_resistance_zone_low）"""
    p = render_agent_prompt()
    assert "止盈二 tp2_ref=next_resistance_zone_low" in p
    assert "止盈二取止盈一锚定档的下一档同类结构位" in p
    assert "tp2_ref" in p  # 价格锚点枚举（entry/stop/tp1/tp2）在位


def test_agent_prompt_tp2_off_excludes():
    """关闭：止盈二锚点语义整体剔除，改为禁止提交 tp2_*"""
    p = render_agent_prompt(tp2_enabled=False)
    assert "不要提交 tp2_ref/tp2_offset_pct" in p
    assert "止盈二 tp2_ref=next_resistance_zone_low" not in p
    assert "止盈二取止盈一锚定档" not in p
    # 非止盈二部分不受影响
    assert "tp1_ref=resistance_zone_low" in p
    assert "ref 取值:" in p


# ── 2. Agent 工具 schema 过滤 ────────────────────────────────────────


def test_tools_schema_on_returns_constant():
    """开启：直接复用模块常量（零开销），tp2 参数在位"""
    schema = _tools_schema(tp2_enabled=True)
    assert schema is _tools_schema.__globals__["TOOLS_SCHEMA"]


def test_tools_schema_off_hides_tp2_args():
    """关闭：submit_decision 隐藏 tp2_ref/tp2_offset_pct，其余参数与常量不动"""
    from app.services.ai_agent import TOOLS_SCHEMA

    schema = _tools_schema(tp2_enabled=False)
    props = next(
        t["function"]["parameters"]["properties"]
        for t in schema if t["function"]["name"] == "submit_decision"
    )
    assert "tp2_ref" not in props
    assert "tp2_offset_pct" not in props
    assert "tp1_ref" in props and "stop_ref" in props and "entry_ref" in props
    # 常量本体不被污染
    base = next(
        t["function"]["parameters"]["properties"]
        for t in TOOLS_SCHEMA if t["function"]["name"] == "submit_decision"
    )
    assert "tp2_ref" in base


# ── 3. submit_decision 提交侧硬清零 ──────────────────────────────────


def _signal():
    return {
        "symbol": "TESTUSDT", "signal_type": "trigger_breakout",
        "current_price": 100.0,
        "recent_swings": {
            "highs": [{"price": 105.0, "label": "HH"}, {"price": 110.0, "label": "HH"}],
            "lows": [{"price": 97.0, "label": "HL"}],
        },
    }


def _klines():
    """[-2] = 突破K（low=99.0 供 signal_low 近锚），[-1] 未收盘"""
    return [
        [0, 98.0, 98.5, 97.5, 98.2, 10.0, 0, 10000.0],
        [1, 99.0, 99.8, 99.0, 99.7, 20.0, 0, 20000.0],
        [2, 99.8, 100.2, 99.6, 100.0, 5.0, 0, 5000.0],
    ]


def _submit_args():
    """tp2 锚点指到第二档摆动高点（110），开启时应解析出 110"""
    return {
        "trade_decision": "suggest", "direction": "long",
        "trade_type": "trend_follow",
        "entry_ref": "market", "entry_offset_pct": 0,
        "stop_ref": "signal_low", "stop_offset_pct": -0.3,
        "tp1_ref": "resistance_zone_low", "tp1_offset_pct": 0,
        "tp2_ref": "next_resistance_zone_low", "tp2_offset_pct": 0,
        "recommendation": 70,
        "reason": "1. r",
    }


def test_handle_submit_tp2_on_resolves():
    """开启：tp2_ref=next_resistance_zone_low → 解析到第二档摆动高点"""
    done, _, fixed = _handle_submit(
        _submit_args(), _signal(), _klines(), tp2_enabled=True,
    )
    assert done
    assert fixed["take_profit_2"] == pytest.approx(110.0)


def test_handle_submit_tp2_off_forces_zero():
    """关闭：即使模型沿旧习惯提交 tp2_ref，落库 take_profit_2 一律 0"""
    done, _, fixed = _handle_submit(
        _submit_args(), _signal(), _klines(), tp2_enabled=False,
    )
    assert done
    assert fixed["take_profit_2"] == 0
    # 其余三价正常解析不受影响
    assert fixed["take_profit_1"] == pytest.approx(105.0)
    assert fixed["entry_price"] == pytest.approx(100.0)
