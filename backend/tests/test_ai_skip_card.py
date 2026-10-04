# -*- coding: utf-8 -*-
"""满仓跳过 AI 分析飞书卡片单测（2026-10-04）

_ai_skip_card：橙标题 / 原因与两网计数拆行渲染 / note 脚注恢复提示。
只构造卡片体，不发送不触网（test_scan_hit_card 同风格）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scanner import _ai_skip_card  # noqa: E402


def test_card_title_reason_and_counts():
    """橙标题 + 原因行 + 计数行：从 slots_detail 拆出原因与括号内计数"""
    card = _ai_skip_card(
        "满仓跳过 AI 分析：测试网在跑单达上限（测试网 10/10、正式网 4/10）")
    assert card["header"]["title"]["content"] == "⏸️ 满仓跳过本轮 AI 分析"
    assert card["header"]["template"] == "orange"
    body = card["elements"][0]["content"]
    assert "测试网在跑单达上限" in body          # 原因（去掉前缀）
    assert "满仓跳过" not in body                # 前缀已由标题承载
    assert "测试网 10/10、正式网 4/10" in body   # 计数
    assert "在跑名额" in body


def test_card_note_recovery_hint():
    """note 脚注说明恢复条件与手动单币分析路径"""
    card = _ai_skip_card(
        "满仓跳过 AI 分析：正式网在跑单达上限（测试网 2/10、正式网 10/10）")
    note = card["elements"][1]
    assert note["tag"] == "note"
    text = note["elements"][0]["content"]
    assert "下一轮自动恢复" in text
    assert "手动单币分析" in text
