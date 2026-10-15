# -*- coding: utf-8 -*-
"""ATR 行注入回归（docs/24 D1）：render_indicator_block 的 ATR 数值 + 百分位行。

提示词纪律多处按 ATR 定规则（≤1×ATR 的位是争夺位、近锚资格止损宽度
≥0.3×ATR），此前提示词只有规则没有数值，AI 无据可依。

覆盖：
1. atr_series：Wilder 平滑口径（与纯 Python 参考实现逐项对比、NaN 种子段、
   数据不足返回 None）
2. ATR 行：出现、格式（值/占现价%/分位）、分位端点（平坦=0%、递增=100%）、
   位置在"方向票"结论行之前
3. 数据不足：整行省略（块内无 ATR 行）
"""
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.services.strategy.technical import (  # noqa: E402
    analyze_supertrend,
    atr_series,
    render_indicator_block,
)


def _make_klines(closes, spreads=None):
    """合成 K 线：[ts, o, h, l, c, vol, close_ts, quote_vol]。

    o=前收，h=c+spread，l=c-spread（spreads 缺省全 1.0）；最后一根模拟未收盘。
    """
    if spreads is None:
        spreads = [1.0] * len(closes)
    assert len(spreads) == len(closes)
    out = []
    for i, c in enumerate(closes):
        ts = i * 3_600_000
        s = spreads[i]
        o = closes[i - 1] if i else c
        out.append([ts, o, c + s, c - s, c, 10.0, ts + 3_599_000, 1000.0])
    return out


# ── atr_series 口径 ───────────────────────────────────────────────────


def test_atr_series_matches_reference():
    """与纯 Python 参考实现（Wilder 平滑）逐项一致，前 period-1 项为 NaN"""
    closes = [100.0 + i * 0.5 for i in range(40)]
    spreads = [1.0 + (i % 5) * 0.3 for i in range(40)]
    klines = _make_klines(closes, spreads)
    arr = atr_series(klines, 14)

    closed = klines[:-1]
    trs = [closed[0][2] - closed[0][3]]
    for i in range(1, len(closed)):
        h, low, pc = closed[i][2], closed[i][3], closed[i - 1][4]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    p = 14
    ref = [None] * len(trs)
    ref[p - 1] = sum(trs[:p]) / p
    for i in range(p, len(trs)):
        ref[i] = (ref[i - 1] * (p - 1) + trs[i]) / p

    assert len(arr) == len(closed)
    for i in range(p - 1):
        assert math.isnan(arr[i])
    for i in range(p - 1, len(trs)):
        assert arr[i] == pytest.approx(ref[i], rel=1e-9)


def test_atr_series_insufficient_data():
    """已收盘不足 period+5 → None；atr_series 与 analyze_supertrend 复用同源"""
    assert atr_series(_make_klines([100.0] * 18), 14) is None
    # 复用后 SuperTrend 仍正常（数值口径不变的旁证）
    st = analyze_supertrend(_make_klines([100.0 + i * 0.8 for i in range(60)]))
    assert st is not None and st["state"] in ("bullish", "bearish")


# ── ATR 行渲染 ────────────────────────────────────────────────────────

_ATR_RE = re.compile(
    r"- ATR\(14\)=([\d.]+)（([\d.]+)% · (\d+)根分位 ([\d.]+)%）"
)


def test_atr_line_present_and_format():
    """平坦波动：ATR 恒为 2×spread，分位 0%（序列无更小值），行在方向票之前"""
    klines = _make_klines([100.0] * 60)  # 59 根已收盘，有效 ATR 46 个
    block = render_indicator_block(klines)
    m = _ATR_RE.search(block)
    assert m, f"ATR 行缺失:\n{block}"
    assert float(m.group(1)) == pytest.approx(2.0, abs=1e-6)   # 全平坦 TR=2s
    assert float(m.group(2)) == pytest.approx(2.0, abs=0.05)   # 2/100=2.0%
    assert int(m.group(3)) == 46                               # 59-14+1
    assert float(m.group(4)) == pytest.approx(0.0, abs=0.5)    # 全等 → 0%
    # 位置：ATR 行在指标行之后、"方向票"结论行之前
    assert block.index("ATR(14)=") < block.index("- 方向票（")


def test_atr_line_percentile_ramp_100():
    """波动单调放大：当前 ATR 为序列最大 → 分位达上限。

    分位序列含当前值（与布林带宽分位同口径），上限=(n-1)/n=45/46≈97.8%，
    非 100%。
    """
    closes = [100.0] * 60
    spreads = [1.0 + i * 0.1 for i in range(60)]  # TR=2s 单调递增
    block = render_indicator_block(_make_klines(closes, spreads))
    m = _ATR_RE.search(block)
    assert m
    assert float(m.group(4)) == pytest.approx(45 / 46 * 100, abs=0.5)
    assert int(m.group(3)) == 46


def test_atr_line_percentile_consistent_with_series():
    """分位=当前 ATR 在最近 100 根有效序列中"小于当前值"的占比（渲染与序列一致）"""
    closes = [100.0 + (i % 7) * 0.4 for i in range(80)]
    spreads = [1.0 + (i * 13 % 9) * 0.25 for i in range(80)]
    klines = _make_klines(closes, spreads)
    block = render_indicator_block(klines)
    m = _ATR_RE.search(block)
    assert m
    valid = [v for v in atr_series(klines, 14) if not math.isnan(v)]
    cur = valid[-1]
    seq = valid[-100:]
    expect = sum(1 for v in seq if v < cur) / len(seq) * 100
    assert float(m.group(1)) == pytest.approx(cur, abs=1e-4)
    assert int(m.group(3)) == len(seq)
    assert float(m.group(4)) == pytest.approx(expect, abs=0.5)


def test_atr_line_omitted_when_insufficient():
    """数据不足（K 线太少）→ ATR 行省略；无任何指标可用时整块本就为空"""
    # 15 根（14 已收盘）< period+5：ATR 不可算，整块空
    assert render_indicator_block(_make_klines([100.0] * 15)) == ""
    # 20 根（19 已收盘）：ATR 恰可算但其余指标数据门未到 → 无指标行时整块为空，
    # ATR 行随块省略（不单独撑起一个只有 ATR 的块）
    assert render_indicator_block(_make_klines([100.0] * 20)) == ""
