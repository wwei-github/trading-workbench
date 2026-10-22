"""TP2 分批止盈开关（2026-10-08）——关闭时 AI 不产出第二档止盈价。

P0 系统提示词剔除止盈二规则与字段说明（render_system_prompt）。
"""

import pytest

from app.config import settings
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


# ── 1.5 止盈一兜底拆情形（2026-10-12-3）：结构位过远时 TP2 保留远端锚 ──


def test_p0_prompt_tp2_fallback_keeps_far_level():
    """开启：兜底拆 (a) 无结构位仅一档 / (b) 结构位过远时止盈二保留远端锚"""
    p = render_p0_prompt()
    assert "(a) 前方无任何结构位可锚" in p
    assert "止盈二设 0（仅设一档）" in p          # (a) 情形维持原行为
    assert "止盈二保留" in p and "趋势跑手档" in p  # (b) 情形保留远端锚


def test_p0_prompt_tp2_off_no_retention_clause():
    """关闭：无 (b) 保留分支（TP2 整体关闭）"""
    p = render_p0_prompt(tp2_enabled=False)
    assert "止盈二保留" not in p
    assert "趋势跑手档" not in p



