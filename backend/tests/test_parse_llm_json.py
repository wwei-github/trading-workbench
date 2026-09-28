# -*- coding: utf-8 -*-
"""parse_llm_json 单测（2026-09-28）。

LLM 输出 JSON 偶发语法瑕疵（线上实测 "Expecting ',' delimiter"，
典型为字符串内未转义引号/尾逗号），标准 json.loads 失败后降级
json_repair 修复；修复不出对象时按原异常抛出。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm_client import parse_llm_json  # noqa: E402


def test_valid_json_passthrough():
    """合法 JSON 直接解析，不走修复路径"""
    assert parse_llm_json('{"a": 1, "b": "文本"}') == {"a": 1, "b": "文本"}


def test_unescaped_quote_repaired():
    """字符串内未转义引号（线上 11:04 告警同类）→ 修复成功"""
    broken = '{"analysis": "阻力位在"上方"附近", "decision": "skip"}'
    data = parse_llm_json(broken)
    assert data["decision"] == "skip"
    assert "上方" in data["analysis"]


def test_trailing_comma_repaired():
    """尾逗号 → 修复成功"""
    assert parse_llm_json('{"a": 1,}') == {"a": 1}


def test_markdown_wrapped_repaired():
    """markdown 代码块包裹（调用方漏剥时）→ 修复成功"""
    data = parse_llm_json('```json\n{"a": 1}\n```')
    assert data == {"a": 1}


def test_unrepairable_raises_original():
    """极端残缺（非 JSON 语义）→ 抛原 JSONDecodeError，由调用方重试/降级"""
    with pytest.raises(json.JSONDecodeError):
        parse_llm_json("这不是 JSON 输出")
