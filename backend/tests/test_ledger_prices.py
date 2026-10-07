# -*- coding: utf-8 -*-
"""虚拟账本三价推导单测（docs/16 P-07 步骤1）。

核心锁两处：
1. derive_signal_prices 纯函数行为（long/short 对称、stop_buf、None 边界）；
2. 与 build_signals_by_scan_replay 的同源一致性——源B 内联公式已抽取为
   derive_signal_prices，同输入必须逐字段相等（防两处公式漂移）。
纯函数 + 编排测试（monkeypatch 检测器，不触 DB 不触网）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backtest_engine import (  # noqa: E402
    build_signals_by_scan_replay,
    derive_signal_prices,
)
from app.services.ledger import (  # noqa: E402
    LEDGER_RR_TP1,
    LEDGER_TP2_RR,
    derive_prices_for_scan,
)

T0 = 1_791_000_000_000
HOUR_MS = 3_600_000


def kb5(close=100.0, low=99.5, high=100.5):
    """含信号K在内的最近 5 根已收盘 K 线（恒定 OHLC，信号K为末根）。"""
    return [[T0 + i * HOUR_MS, close, high, low, close, 100.0] for i in range(5)]


# ── 1. 纯函数行为 ──


def test_long_prices_and_tp2():
    p = derive_signal_prices(1, 100.0, kb5(), rr_tp1=1.5, tp2_rr=3.0)
    assert p["entry"] == 100.0
    assert p["stop"] == pytest.approx(99.5 * 0.997)
    r_unit = 100.0 - 99.5 * 0.997
    assert p["r_unit"] == pytest.approx(r_unit)
    assert p["tp1"] == pytest.approx(100.0 + 1.5 * r_unit)
    assert p["tp2"] == pytest.approx(100.0 + 3.0 * r_unit)


def test_short_symmetric():
    p = derive_signal_prices(-1, 100.0, kb5(), rr_tp1=1.5, tp2_rr=3.0)
    assert p["stop"] == pytest.approx(100.5 * 1.003)
    r_unit = 100.5 * 1.003 - 100.0
    assert p["r_unit"] == pytest.approx(r_unit)
    assert p["tp1"] == pytest.approx(100.0 - 1.5 * r_unit)
    assert p["tp2"] == pytest.approx(100.0 - 3.0 * r_unit)


def test_tp2_zero_when_rr_zero():
    p = derive_signal_prices(1, 100.0, kb5(), tp2_rr=0.0)
    assert p["tp2"] == 0.0  # 0=不设


def test_none_on_bad_inputs():
    assert derive_signal_prices(1, 0.0, kb5()) is None          # entry<=0
    assert derive_signal_prices(1, 100.0, []) is None            # 空 recent5
    # long 但 stop 在 entry 上方（r_unit<=0，如 5 根最低价高于收盘）
    bad = kb5(close=100.0, low=101.0, high=102.0)
    assert derive_signal_prices(1, 100.0, bad) is None
    # short 对称：5 根最高价低于收盘
    bad_s = kb5(close=100.0, low=98.0, high=99.0)
    assert derive_signal_prices(-1, 100.0, bad_s) is None


def test_recent5_extreme_not_only_signal_bar():
    """近5根极值（不只信号K）决定 stop：前 4 根更深的低点要被取到。"""
    bars = kb5(low=99.5)
    bars[0][3] = 97.0  # 首根深低点
    p = derive_signal_prices(1, 100.0, bars)
    assert p["stop"] == pytest.approx(97.0 * 0.997)


# ── 2. 账本口径 wrapper ──


def test_ledger_wrapper_defaults():
    """derive_prices_for_scan：账本口径 TP1=1.5R、TP2=3R（用户拍板）。"""
    p = derive_prices_for_scan(1, 100.0, kb5())
    ref = derive_signal_prices(1, 100.0, kb5(),
                               rr_tp1=LEDGER_RR_TP1, tp2_rr=LEDGER_TP2_RR)
    assert p == ref
    assert p["tp2"] > p["tp1"] > p["entry"]


# ── 3. 与源B 同源一致性 ──


def test_source_b_uses_derive(monkeypatch):
    """build_signals_by_scan_replay 输出的三价必须与 derive_signal_prices
    同输入逐字段相等（抽取等价重构的不变性锁）。"""
    n = 260
    kl = [[T0 + i * HOUR_MS, 100.0, 100.5, 99.5, 100.0, 100.0] for i in range(n)]
    kl[244][3] = 97.0   # 信号根前 5 根内深低点（极值来自近5根而非恒定值）
    htf = [[T0 + i * 4 * HOUR_MS, 100.0, 101.0, 99.0, 100.0, 100.0] for i in range(60)]
    fired = {245: 3, 249: -3}

    def fake_detect(pool, symbol, klines, pending=None, flt=None, retest=None,
                    event_out=None):
        t = (int(klines[-1][0]) - T0) // HOUR_MS
        if t not in fired:
            return None, ({"op": "expire", "reason": "test"} if pending else None)
        cand = {"signal_type": "trigger_pullback_confirm",
                "touch_ref": "ema21", "quality_score": 88,
                "votes": {"net": fired[t]}}
        return cand, None

    monkeypatch.setattr(
        "app.services.scanner.detect_indicator_candidate", fake_detect)

    class _F:
        pass

    sigs = build_signals_by_scan_replay("BTCUSDT", kl, htf, _F(), rr_tp1=1.5,
                                        repeat_bars=24)
    assert [s["direction"] for s in sigs] == ["long", "short"]
    for s in sigs:
        t = (s["entry_time_ms"] - T0) // HOUR_MS - 1  # 信号根索引
        direction = 1 if s["direction"] == "long" else -1
        ref = derive_signal_prices(direction, float(kl[t][4]),
                                   kl[t - 4: t + 1], rr_tp1=1.5)
        for key in ("entry", "stop", "tp1", "tp2", "r_unit"):
            assert s[key] == pytest.approx(ref[key]), f"{key} 漂移 @t={t}"
