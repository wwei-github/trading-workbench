# -*- coding: utf-8 -*-
"""Agent 锚点解析摆动路径回归（2026-09-28 关键位子系统移除后）。

LEVEL_REFS 不再含 key_ 前缀锚点，所有结构锚点一律解析到摆动结构位：
- support/resistance = 现价下/上方最近的摆动低/高点
- zone 系（历史命名）= 现价上/下方第 nth 近的摆动高/低点（止盈锚）
- 同侧无摆动位 → 返回 None（submit 侧据此打回重试）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai_agent import (
    LEVEL_REFS,
    _resolve_price,
)


def _signal(cur=100.0, highs=(101.0, 103.0), lows=(99.0, 97.0)) -> dict:
    """现价 100：highs 升序在上、lows 降序在下（recent_swings 收盘价口径）"""
    return {
        "current_price": cur,
        "recent_swings": {
            "highs": [{"price": p, "label": "HH"} for p in highs],
            "lows": [{"price": p, "label": "HL"} for p in lows],
        },
    }


def test_level_refs_no_key_prefix():
    """key_ 前缀锚点已随关键位子系统移除，枚举只剩结构/线外/market 三族"""
    assert not any(r.startswith("key_") for r in LEVEL_REFS)


def test_support_resistance_nearest_swing():
    sig = _signal()
    assert _resolve_price("support", 0, sig) == 99.0
    assert _resolve_price("resistance", 0, sig) == 101.0


def test_zone_refs_nth_swing():
    sig = _signal()
    # 止盈锚：做多锚现价上方最近摆动高点、止盈二取更远一档
    assert _resolve_price("resistance_zone_low", 0, sig) == 101.0
    assert _resolve_price("next_resistance_zone_low", 0, sig) == 103.0
    assert _resolve_price("support_zone_high", 0, sig) == 99.0
    assert _resolve_price("next_support_zone_high", 0, sig) == 97.0


def test_offset_pct_applied():
    assert _resolve_price("support", -0.5, _signal()) == 99.0 * 0.995


def test_empty_side_returns_none():
    sig = _signal(highs=(), lows=(99.0,))
    assert _resolve_price("resistance", 0, sig) is None
    assert _resolve_price("support", 0, sig) == 99.0


def test_market_and_invalid_ref():
    sig = _signal()
    assert _resolve_price("market", 0, sig) == 100.0
    assert _resolve_price(None, 0, sig) is None


def test_legacy_key_ref_rejected():
    """关键位时代 key_ 前缀锚点已移除：模型沿旧习惯报出时按非法锚点
    返回 None（走 _handle_submit 打回重试），不得静默落到错误侧"""
    sig = _signal()
    assert _resolve_price("key_support_confirmed", 0, sig) is None
    assert _resolve_price("key_nearest_strong", 0, sig) is None
    assert _resolve_price("bogus_ref", 0, sig) is None


# ── 信号K线极值锚（docs/23 近锚降级链）────────────────────────────────


def _klines_with_signal_candle(low=99.0, high=99.8):
    """3 根K线：[-2] 为突破K（low/high 可控），[-1] 未收盘"""
    return [
        [0, 98.0, 98.5, 97.5, 98.2, 10.0, 0, 10000.0],
        [1, 99.0, high, low, 99.7, 20.0, 0, 20000.0],  # 突破K
        [2, 99.8, 100.2, 99.6, 100.0, 5.0, 0, 5000.0],  # 未收盘
    ]


def test_signal_extreme_refs_resolve_to_klines_minus2():
    """signal_low/signal_high 解析到突破K（klines[-2]，[-1] 未收盘）极值"""
    sig = _signal()
    kl = _klines_with_signal_candle(low=99.0, high=99.8)
    assert _resolve_price("signal_low", 0, sig, klines=kl) == 99.0
    assert _resolve_price("signal_high", 0, sig, klines=kl) == 99.8
    # offset：做多近锚 = 突破K最低 × (1 - 0.3%)
    assert _resolve_price("signal_low", -0.3, sig, klines=kl) == 99.0 * 0.997


def test_signal_extreme_without_klines_is_none():
    """klines 缺失时极值锚返回 None（走 _handle_submit 打回重试），不静默落 0"""
    sig = _signal()
    assert _resolve_price("signal_low", 0, sig, klines=None) is None
    assert _resolve_price("signal_low", 0, sig, klines=[_klines_with_signal_candle()[0]]) is None
    assert _resolve_price("signal_high", 0, sig, klines=[]) is None


# ── zone 系止盈锚：剔除 ≤1×ATR 争夺位（2026-10-12）─────────────────────


def _flat_klines(n=40, rng=2.0, base=100.0):
    """n 根 1h K 线，每根 high-low=rng → TR 恒为 rng → ATR(14)=rng"""
    return [
        [i, base, base + rng / 2, base - rng / 2, base, 10.0, 0, 0.0]
        for i in range(n)
    ]


def test_zone_refs_exclude_contention_zone_with_atr():
    """有 K 线：ATR=2 → 距现价 0.5 的 100.5 属争夺位剔除，第一档落到 103"""
    kl = _flat_klines()
    sig = _signal(cur=100.0, highs=(100.5, 103.0, 110.0), lows=(99.0,))
    assert _resolve_price("resistance_zone_low", 0, sig, klines=kl) == 103.0
    assert _resolve_price("next_resistance_zone_low", 0, sig, klines=kl) == 110.0
    # 止损结构锚（support/resistance）不受该剔除影响，仍取最近位
    assert _resolve_price("resistance", 0, sig, klines=kl) == 100.5


def test_zone_refs_without_klines_keeps_nearest():
    """无 K 线（ATR 不可算）→ 不剔除，回落"最近位"语义（旧行为不变）"""
    sig = _signal(cur=100.0, highs=(100.5, 103.0), lows=(99.0,))
    assert _resolve_price("resistance_zone_low", 0, sig) == 100.5


def test_zone_refs_all_within_contention_returns_none():
    """全部摆动位都在 1×ATR 内 → 无可锚档位返回 None（走打回/兜底路径）"""
    kl = _flat_klines()
    sig = _signal(cur=100.0, highs=(100.5, 101.0), lows=(99.0,))
    assert _resolve_price("resistance_zone_low", 0, sig, klines=kl) is None

