# -*- coding: utf-8 -*-
"""每日归因快照：分档/汇总口径单测（docs/16 §21，D6）。

快照本身要跑真库，单测只固定"口径"——分档边界、R 汇总、分组、卡片渲染。
口径一变，docs/16 里的表格解释就得同步改，所以把它钉在测试里。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import attribution as A  # noqa: E402


def test_bucket_boundaries_stop_atr():
    assert A._bucket(0.5, A.STOP_ATR_BUCKETS) == "<1ATR"
    assert A._bucket(1.0, A.STOP_ATR_BUCKETS) == "1-1.5ATR"      # 左闭
    assert A._bucket(1.49, A.STOP_ATR_BUCKETS) == "1-1.5ATR"
    assert A._bucket(1.5, A.STOP_ATR_BUCKETS) == "1.5-2ATR"
    assert A._bucket(2.0, A.STOP_ATR_BUCKETS) == "≥2ATR"
    assert A._bucket(None, A.STOP_ATR_BUCKETS) is None
    assert A._bucket("", A.STOP_ATR_BUCKETS) is None


def test_bucket_pos_and_room():
    assert A._bucket(0.1, A.POS_BUCKETS) == "pos<0.25"
    assert A._bucket(0.3, A.POS_BUCKETS) == "pos 0.25-0.6"
    assert A._bucket(0.6, A.POS_BUCKETS) == "pos≥0.6"
    assert A._bucket(0.9, A.ROOM_BUCKETS) == "room<1R"
    assert A._bucket(2.5, A.ROOM_BUCKETS) == "room≥2.5R"


def test_summarize_r_counts_and_winrate():
    rows = [{"pnl_pct": 150}, {"pnl_pct": -100}, {"pnl_pct": 0}, {"pnl_pct": None}]
    s = A.summarize_r(rows)
    assert s["n"] == 3                      # None 不计入
    assert s["win_rate"] == pytest.approx(33.3, abs=0.1)   # 仅 >0 算赢，0 不算
    assert s["sum_r"] == pytest.approx(0.5)
    assert s["avg_r"] == pytest.approx(0.167, abs=0.002)


def test_summarize_r_empty():
    assert A.summarize_r([]) == {"n": 0, "win_rate": None, "sum_r": 0.0, "avg_r": None}


def test_group_by_skips_none_keys():
    rows = [{"k": "a", "pnl_pct": 100}, {"k": None, "pnl_pct": -100},
            {"k": "a", "pnl_pct": -100}]
    out = A.group_by(rows, lambda x: x["k"])
    assert list(out) == ["a"]
    assert out["a"]["n"] == 2
    assert out["a"]["sum_r"] == pytest.approx(0.0)


def test_day_window_is_utc_half_open():
    from datetime import date
    start, end = A._day_window(date(2026, 10, 27))
    assert start.isoformat() == "2026-10-27T00:00:00"
    assert end.isoformat() == "2026-10-28T00:00:00"


def test_format_card_negative_is_red():
    snap = {
        "day": "2026-10-26",
        "trades": {"total": {"n": 21, "win_rate": 19.0, "sum_r": -9.95, "avg_r": -0.474},
                   "worst": [{"symbol": "ICNTUSDT", "direction": "long",
                              "signal_type": "trigger_pullback_confirm",
                              "exit_reason": "sl", "r": -1.2}]},
        "funnel": {"rounds": 24, "universe": 12000, "scanned": 8000},
        "candidates": {"n": 200}, "ai": {"n": 100},
        "blocked_probe": {"n": 4000, "pct_touch": 25.0, "pct_full": 6.3},
        "logs": {"ERROR": 2},
    }
    card = A.format_card(snap)
    assert card["header"]["template"] == "red"
    body = card["elements"][0]["text"]["content"]
    assert "-9.95" in body and "19.0%" in body
    assert "6.3%" in body
    assert "ICNTUSDT" in body
    assert "ERROR 2" in body


def test_format_card_positive_is_green_and_survives_missing_blocks():
    card = A.format_card({"day": "2026-10-27",
                          "trades": {"total": {"n": 3, "win_rate": 66.7,
                                               "sum_r": 2.5, "avg_r": 0.833}}})
    assert card["header"]["template"] == "green"
    assert "2026-10-27" in card["header"]["title"]["content"]


def test_format_card_empty_day():
    card = A.format_card({"day": "2026-10-20", "trades": {"total": {"n": 0}}})
    assert card["header"]["template"] == "grey"
    assert "无数据" in card["elements"][0]["text"]["content"] or \
        "无已平仓" in card["elements"][0]["text"]["content"]
