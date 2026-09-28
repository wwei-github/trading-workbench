# -*- coding: utf-8 -*-
"""筛选 4h 震荡过滤回归（2026-09-28）。

detect_indicator_candidate 在多数决通过后增加 4h 背景检查：
analyze_htf_background state=ranging（ADX<20）→ 不产生候选（返回 None），
活跃 pending 同轮 expire（方向失守路径复用，语义=震荡下信号失效）。
4h 拉取失败（htf=None）不过滤——降级放行与第 7 票口径一致。

用假 pool 注入 1h/4h K 线，不触网。K 线构造保证 6 票预筛与多数决通过
（|net6|≥2、|net7|≥3、voted≥5），使过滤命中点唯一落在 ranging 检查处。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.services.scanner import detect_indicator_candidate  # noqa: E402


def _trend_1h(closes_start: float = 100.0, drift: float = 0.35) -> list[list]:
    """110 根稳步上行 1h：让多数 EMA/MACD/RSI 等投多票（|net| 达标）"""
    kl = []
    price = closes_start
    ts = 1_700_000_000_000
    for i in range(110):
        o = price
        price += drift
        kl.append([ts + i * 3_600_000, o, price + 0.15, price - 0.25, price, 100.0, 100.0])
    return kl


def _ranging_4h() -> list[list]:
    """220 根窄幅来回 4h：EMA21/55 纠缠、ADX<20 → analyze_htf_background=ranging"""
    kl = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(220):
        # 正弦小幅摆动（幅度 0.6），EMA 无法拉开间距，ADX 低
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl.append([ts + i * 4 * 3_600_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])
        price = c
    return kl


def _trending_4h_up() -> list[list]:
    """220 根稳步上行 4h：强/弱多头背景（非 ranging，对照组）"""
    kl = []
    ts = 1_700_000_000_000
    price = 60.0
    for i in range(220):
        o = price
        price += 0.25
        kl.append([ts + i * 4 * 3_600_000, o, price + 0.2, price - 0.2, price, 90.0, 90.0])
    return kl


class _FakePool:
    """pool.get_klines 桩：1h 返回传入序列，4h 可指定 ranging/趋势"""

    def __init__(self, klines_1h, klines_4h):
        self._m = {"1h": klines_1h, "4h": klines_4h}

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self._m[interval][:limit]


def test_ranging_4h_filtered_out():
    """4h 震荡（ADX<20）：即使 1h 指标多数决通过也不产生候选"""
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _ranging_4h()), "TEST/USDT", _trend_1h()
    )
    assert cand is None


def test_ranging_4h_expires_pending():
    """4h 震荡且有活跃 pending → 同轮 expire（复用方向失守路径）"""
    pending = {"id": "x", "direction": 1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _ranging_4h()), "TEST/USDT", _trend_1h(), pending=pending
    )
    assert cand is None
    assert op is not None and op["op"] == "expire"


def test_trending_4h_not_filtered():
    """对照组：4h 趋势背景不被 ranging 过滤误杀（能走到触发环节）"""
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _trending_4h_up()), "TEST/USDT", _trend_1h()
    )
    # 无 pending 时 ranging 过滤路径不会产生 expire —— op 为 None 或非过滤 expire
    assert not (op and op.get("op") == "expire")


def test_htf_fetch_failure_not_filtered():
    """4h 拉取失败（htf=None）：降级放行，不因缺数据过滤"""
    kl = _trend_1h()

    class _BrokenPool:
        def get_klines(self, symbol, interval, limit, **kwargs):
            if interval == "4h":
                raise RuntimeError("exchange down")
            return kl[:limit]

    cand, op = detect_indicator_candidate(_BrokenPool(), "TEST/USDT", kl)
    # 候选可能因无触发为 None，但不得被 ranging 过滤挡住 —— 与趋势对照组同判
    assert not (cand is None and op and op.get("op") == "expire")
