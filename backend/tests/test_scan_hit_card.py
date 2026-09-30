"""定时扫描命中飞书卡片单测（2026-09-30 纯文本改消息卡片）

_scan_hit_card：标题计数 / lark_md 行（信号族·金K形态·质量分）/ 未知信号
与未知形态兜底 / 超 8 条截断 + note 脚注。只构造卡片体，不发送不触网。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scanner import _scan_hit_card  # noqa: E402


def _hit(symbol, signal_type=None, pattern=None, quality=0):
    return {
        "symbol": symbol,
        "signal_type": signal_type,
        "pattern": pattern,
        "quality_score": quality,
    }


def test_card_title_and_hit_lines():
    """标题计数 + 命中两行：信号族中文名 + 金K形态标签 + 质量分"""
    card = _scan_hit_card([
        _hit("API3USDT", "trigger_pullback", "bullish_engulfing", 87),
        _hit("BTCUSDT", "trigger_breakout", None, 66),
    ])
    assert card["header"]["title"]["content"] == "🔎 定时扫描命中 2 个"
    assert card["header"]["template"] == "blue"
    body = card["elements"][0]["text"]["content"]
    assert "**API3USDT**　回踩金K确认·看涨吞没　质量分 87" in body
    assert "**BTCUSDT**　同向突破　质量分 66" in body
    assert len(card["elements"]) == 1  # ≤8 条无脚注


def test_card_unknown_signal_and_pattern_fallback():
    """信号族未知 → 「未知信号」；形态映射缺失 → 原始值兜底不吞"""
    card = _scan_hit_card([
        _hit("XYZUSDT", None, None, 60),
        _hit("ABCUSDT", "trigger_pullback", "unknown_pattern", 61),
    ])
    body = card["elements"][0]["text"]["content"]
    assert "未知信号" in body
    assert "unknown_pattern" in body


def test_card_truncates_over_8_with_note():
    """超过 8 条：正文只列前 8（标题仍报总数），脚注给总命中数"""
    hits = [_hit(f"SYM{i}USDT", "trigger_breakout", None, 60 + i) for i in range(10)]
    card = _scan_hit_card(hits)
    assert card["header"]["title"]["content"] == "🔎 定时扫描命中 10 个"
    body = card["elements"][0]["text"]["content"]
    assert body.count("质量分") == 8
    assert "SYM7USDT" in body and "SYM8USDT" not in body
    note = card["elements"][1]["elements"][0]["content"]
    assert note == "仅列前 8 个，共命中 10 个"