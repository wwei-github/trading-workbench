# -*- coding: utf-8 -*-
"""摆动结构位价口径（docs/15 §17，2026-10-27）

`SWING_LEVEL_SOURCE` 只影响 `_detect_swing_touch`：
- close   = 收盘价摆动点（旧口径，与 AI 事实包/K线标注同源）
- extreme = 摆动K线自身 high/low 定价（真极值）

依据（正式网 swing 成交 124 笔回放）：位价与真极值偏移 ≥0.3% 的 60 笔 -14.83R，
<0.3% 的 46 笔 +22.65R；多头侧 14 笔 +13.99R 对 33 笔 -13.65R。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.strategy import recent_swings  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402

TS0 = 1716000000000
STEP = 3_600_000


def _klines(closes, highs, lows):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append([TS0 + i * STEP, prev, highs[i], lows[i], c, 100.0, 1e6])
        prev = c
    return out


def _swing_series(swing_close: float, swing_low: float) -> list[list]:
    """上升序列 n=60，摆动低点 103.0 附近：idx48 为 HL，收盘按 swing_close 给、
    最低价按 swing_low 给；信号K线（idx58）下探 103.0、收盘 103.2。"""
    closes = [100.0 + 0.1 * i for i in range(15)]                  # 100.0→101.4
    closes += [100.8, 100.2, 99.6, 100.2, 100.8, 101.4, 102.0]     # 谷底99.6@17
    closes += [102.8 + 0.1 * i for i in range(24)]                 # idx22→45
    closes += [104.6, 103.8, 103.0, 103.8, 104.6, 105.4]           # 谷底@48
    closes += [106.2, 106.4, 106.6, 106.8]                         # idx52→55
    closes += [107.0, 106.8]                                       # idx56,57
    closes += [103.2]                                              # idx58 信号K线
    closes += [103.4]                                              # idx59 未收盘
    closes[48] = swing_close
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    highs[48] = swing_close + 0.25
    lows[48] = swing_low
    kl = _klines(closes, highs, lows)
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, 106.8, 103.6, 103.0, 103.2, vol, qv]             # 下探 103.0、收 103.2
    return kl


@pytest.fixture(autouse=True)
def _pin_source(monkeypatch):
    monkeypatch.setattr(settings, "SWING_LEVEL_SOURCE", "close")


# ── recent_swings 口径 ──────────────────────────────────────────────────

def test_recent_swings_close_source_uses_close():
    sw = recent_swings(_swing_series(103.15, 103.0), order=3, n=3)
    hl = [p for p in sw["lows"] if p["label"] == "HL"]
    assert hl and hl[0]["price"] == pytest.approx(103.15)
    assert hl[0]["extreme"] == pytest.approx(103.0)   # 极值列始终可读


def test_recent_swings_extreme_source_uses_low_high():
    sw = recent_swings(_swing_series(103.15, 103.0), order=3, n=3,
                       price_source="extreme")
    hl = [p for p in sw["lows"] if p["label"] == "HL"]
    assert hl and hl[0]["price"] == pytest.approx(103.0)
    assert hl[0]["extreme"] == pytest.approx(103.0)


def test_recent_swings_price_source_is_noop_for_default():
    kl = _swing_series(103.15, 103.0)
    a = recent_swings(kl, order=3, n=3)
    b = recent_swings(kl, order=3, n=3, price_source="close")
    assert a == b


# ── _detect_swing_touch 口径 ────────────────────────────────────────────

def test_touch_close_source_hits_close_level():
    """收盘价口径：位价 103.15（收盘），与极值差 0.146% → 命中 103.15"""
    assert trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3) \
        == pytest.approx(103.15)


def test_touch_extreme_source_hits_true_low(monkeypatch):
    monkeypatch.setattr(settings, "SWING_LEVEL_SOURCE", "extreme")
    assert trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3) \
        == pytest.approx(103.0)


def test_touch_close_source_misses_when_close_far_above_low(monkeypatch):
    """收盘 103.4 高于信号K线收盘 103.2 → 旧口径「收盘守住」失败不出触点"""
    kl = _swing_series(103.4, 103.0)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) is None


def test_touch_extreme_source_still_hits(monkeypatch):
    """同一根 K 线，极值口径位价 103.0 → 影线入带 + 收盘守住，命中"""
    monkeypatch.setattr(settings, "SWING_LEVEL_SOURCE", "extreme")
    kl = _swing_series(103.4, 103.0)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) == pytest.approx(103.0)


def test_touch_invalid_source_falls_back_to_close():
    """大小写/空格容错；未知值回落 close（不得抛错、不得意外启用 extreme）"""
    assert trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3) \
        == pytest.approx(103.15)


@pytest.mark.parametrize("raw", ["extreme", "EXTREME", " Extreme "])
def test_touch_source_case_insensitive(monkeypatch, raw):
    monkeypatch.setattr(settings, "SWING_LEVEL_SOURCE", raw)
    assert trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3) \
        == pytest.approx(103.0)


def test_touch_diag_records_offset():
    diag: dict = {}
    trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3, diag=diag)
    reasons = "；".join(diag.get("reasons", []))
    assert "摆动触点命中" in reasons
    assert "偏移 0.15%" in reasons


def test_touch_diag_none_is_silent(monkeypatch):
    monkeypatch.setattr(settings, "SWING_LEVEL_SOURCE", "extreme")
    assert trig._detect_swing_touch(_swing_series(103.15, 103.0), 1, 0.3, 3) \
        == pytest.approx(103.0)
