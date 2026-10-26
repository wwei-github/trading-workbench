"""定时扫描命中飞书卡片单测（2026-09-30 纯文本改卡片；2026-10-02 三列等分）

_scan_hit_card：标题计数 / 每命中一行 column_set（币种·信号族·质量分三列，
flex_mode=trisect 强制三等分）/ 未知信号与未知形态兜底 / 超 8 条截断 +
note 脚注。只构造卡片体，不发送不触网。
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


def test_card_title_and_three_equal_columns():
    """标题计数 + 每命中一行 column_set：trisect 三等分，内容各就各位"""
    card = _scan_hit_card([
        _hit("API3USDT", "trigger_pullback", "bullish_engulfing", 87),
        _hit("BTCUSDT", "trigger_breakout", None, 66),
    ])
    assert card["header"]["title"]["content"] == "🔎 定时扫描命中 2 个"
    assert card["header"]["template"] == "blue"

    row1, row2 = card["elements"][0], card["elements"][1]
    assert row1["tag"] == "column_set" and row2["tag"] == "column_set"
    assert row1["flex_mode"] == "trisect"
    cols = row1["columns"]
    assert [c["weight"] for c in cols] == [1, 1, 1]
    assert all(c["width"] == "weighted" for c in cols)
    assert cols[0]["elements"][0]["content"] == "**API3USDT**"
    assert cols[1]["elements"][0]["content"] == "回踩金K确认·看涨吞没"
    assert cols[2]["elements"][0]["content"] == "质量分 87"
    assert row2["columns"][0]["elements"][0]["content"] == "**BTCUSDT**"
    assert row2["columns"][1]["elements"][0]["content"] == "同向突破"
    assert row2["columns"][2]["elements"][0]["content"] == "质量分 66"
    assert len(card["elements"]) == 2  # ≤8 条无脚注


def test_card_unknown_signal_and_pattern_fallback():
    """信号族未知 → 「未知信号」；形态映射缺失 → 原始值兜底不吞"""
    card = _scan_hit_card([
        _hit("XYZUSDT", None, None, 60),
        _hit("ABCUSDT", "trigger_pullback", "unknown_pattern", 61),
    ])
    row1, row2 = card["elements"]
    assert row1["columns"][1]["elements"][0]["content"] == "未知信号"
    assert row2["columns"][1]["elements"][0]["content"] == "回踩金K确认·unknown_pattern"


def test_card_marks_prev_bar_pattern():
    """docs/11：形态在信号根之前时形态名带 (前根)；单形态不带"""
    card = _scan_hit_card([
        {**_hit("REZUSDT", "trigger_pullback_confirm", "hammer", 82),
         "golden_combo": True, "entry_lag_pct": 1.588},
        {**_hit("BTCUSDT", "trigger_pullback_confirm", "hammer", 70),
         "golden_combo": False, "entry_lag_pct": None},
    ])
    assert card["elements"][0]["columns"][1]["elements"][0]["content"] == "回踩金K确认·锤形线(前根)"
    assert card["elements"][1]["columns"][1]["elements"][0]["content"] == "回踩金K确认·锤形线"


def test_card_same_bar_combo_not_marked():
    """docs/11 §4.1：golden_combo 只留痕不参与渲染——双流星组合的形态就在
    信号根上（entry_lag_pct 为空），不该标 (前根)；判据只看滞后而非组合标记"""
    card = _scan_hit_card([
        {**_hit("ETHUSDT", "trigger_pullback_confirm", "shooting_star", 78),
         "golden_combo": True, "entry_lag_pct": None},
    ])
    assert (card["elements"][0]["columns"][1]["elements"][0]["content"]
            == "回踩金K确认·流星线")


def test_card_truncates_over_8_with_note():
    """超过 8 条：正文只列前 8（标题仍报总数），脚注给总命中数"""
    hits = [_hit(f"SYM{i}USDT", "trigger_breakout", None, 60 + i) for i in range(10)]
    card = _scan_hit_card(hits)
    assert card["header"]["title"]["content"] == "🔎 定时扫描命中 10 个"
    rows = card["elements"][:-1]
    assert len(rows) == 8
    contents = "\n".join(
        c["elements"][0]["content"]
        for r in rows for c in r["columns"]
    )
    assert contents.count("质量分") == 8
    assert "SYM7USDT" in contents and "SYM8USDT" not in contents
    note = card["elements"][-1]["elements"][0]["content"]
    assert note == "仅列前 8 个，共命中 10 个"
