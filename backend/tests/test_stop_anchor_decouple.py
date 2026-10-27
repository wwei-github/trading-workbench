# -*- coding: utf-8 -*-
"""止损锚分档阈值与止盈上限解耦单测（docs/16 §20 P2-3，2026-10-28）。

背景：提示词里「结构位距入场 > {far_atr}×ATR 就不作止损锚」的 far_atr 原来
用 `TP1_MAX_ATR ÷ rr_min` 现算（2.0÷1.5≈1.33），与止盈一上限共用同一个数。
于是任何放宽止盈上限的动作都会顺带放宽止损锚——把更多单推向 ≥1.5ATR 的
宽止损，而 14 天实测宽止损更差（1–1.5ATR 18 笔 +9.90R；≥2ATR 13 笔 −4.06R）。

解耦后：止损锚走 `STOP_ANCHOR_MAX_ATR`（默认 1.33），止盈上限仍由
`TP1_MAX_ATR / TP1_MAX_R` 决定。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import ai_analyzer  # noqa: E402


def test_default_stop_anchor_is_1_33(monkeypatch):
    """默认止损锚阈值 1.33×ATR（与历史行为一致）。"""
    monkeypatch.setattr(settings, "STOP_ANCHOR_MAX_ATR", 1.33, raising=False)
    assert settings.STOP_ANCHOR_MAX_ATR == pytest.approx(1.33)


def test_sl_rule_uses_independent_key(monkeypatch):
    """放宽 TP1_MAX_ATR 时，止损锚阈值不应跟着变。"""
    monkeypatch.setattr(settings, "STOP_ANCHOR_MAX_ATR", 1.33, raising=False)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 3.0, raising=False)
    prompt = ai_analyzer.render_system_prompt()
    assert "> 1.33×ATR" in prompt


def test_tp_rule_still_follows_tp1_cap(monkeypatch):
    """止盈规则里的 far_atr 仍随 TP1_MAX_ATR/rr_min 变化（描述的是止盈上限）。"""
    monkeypatch.setattr(settings, "STOP_ANCHOR_MAX_ATR", 1.33, raising=False)
    monkeypatch.setattr(settings, "TP1_MAX_ATR", 3.0, raising=False)
    monkeypatch.setattr(settings, "AI_RR_MIN", 1.5, raising=False)
    prompt = ai_analyzer.render_system_prompt(rr_min=1.5)
    # 止盈一上限 = max(TP1_MAX_ATR×ATR, rr_min×止损距离) → 3×ATR
    assert "max(3×ATR" in prompt


def test_stop_anchor_change_is_reflected(monkeypatch):
    """显式改止损锚阈值时提示词立刻反映（回退/试验都靠它）。"""
    monkeypatch.setattr(settings, "STOP_ANCHOR_MAX_ATR", 2.0, raising=False)
    prompt = ai_analyzer.render_system_prompt()
    assert "> 2.0×ATR" in prompt
