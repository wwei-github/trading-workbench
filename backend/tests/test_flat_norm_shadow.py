# -*- coding: utf-8 -*-
"""走平门归一化影子读数（docs/23 T8，2026-10-29）。

现网 flat 判定用绝对百分比阈值（SLOPE_FLAT_PCT=0.5%），高波动品种会被误判走平；
T8 只增加影子读数、不改判定：
- `atr_pct_4h` = ATR14(4h)/price
- `slope_atr_ratio` = 20 根 EMA144 位移 / ATR（跨品种可比）
- `flat_abs` = 现网判定；`flat_norm` = 按 ATR 归一后的影子判定

三个夹具覆盖：差集（绝对走平但归一不平）、两口径一致、趋势通过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy.technical import (  # noqa: E402
    SLOPE_FLAT_FLOOR_PCT,
    htf_context,
)

_TS0 = 1_700_000_000_000
_H4 = 4 * 3_600_000


def _slow_grind() -> list[list]:
    """极低波动缓涨：位移 0.275% < 0.5%（现网判走平），但 ATR 极小 → 归一后不平"""
    kl = []
    price = 100.0
    for i in range(240):
        o = price
        price += 0.015
        kl.append([_TS0 + i * _H4, o, price + 0.02, o - 0.02, price, 90.0, 90.0])
    return kl


def _flat_swing() -> list[list]:
    """窄幅来回摆动：位移≈0、ATR 反而大 → 两口径都判走平"""
    kl = []
    for i in range(240):
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl.append([_TS0 + i * _H4, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])
    return kl


def _trend_up() -> list[list]:
    """稳步上行：斜率 3.97% 远高于阈值，两口径都放行"""
    kl = []
    price = 60.0
    for i in range(240):
        o = price
        price += 0.25
        kl.append([_TS0 + i * _H4, o, price + 0.2, price - 0.2, price, 90.0, 90.0])
    return kl


def test_shadow_fields_present_on_flat():
    """走平样本：影子读数齐全，且现网判定仍为 flat（行为零变化）"""
    res = htf_context(_flat_swing())
    ev = res["evidence"]
    assert res["reject_reason"] == "flat"
    assert ev["flat_abs"] is True
    assert ev["flat_norm"] is True
    assert ev["atr_pct_4h"] and ev["atr_pct_4h"] > 0
    assert ev["slope_atr_ratio"] is not None


def test_abs_flat_but_norm_not_flat_is_now_passed():
    """差集样本：绝对阈值判走平、ATR 归一后放行。

    2026-10-30（docs/23 T12）起走平门以 flat_norm 为主——此前这批低波动缓涨
    品种被固定 0.5% 误杀；现在放行（flat_abs 仍随 evidence 落库供审计）"""
    res = htf_context(_slow_grind())
    ev = res["evidence"]
    assert res["reject_reason"] is None
    assert ev["flat_abs"] is True
    assert ev["flat_norm"] is False
    # 归一化斜率 = 位移/ATR，应远大于 1（位移 5 倍于 4h ATR）
    assert ev["slope_atr_ratio"] > 2


def test_atr_missing_falls_back_to_flat_abs():
    """ATR 不可得（core.atr=0，如全平K线 TR 全 0）：atr_pct_4h 无法计算、
    flat_norm 缺失，走平判定回落 flat_abs——同一低波动样本按旧口径拒绝"""
    from app.services.strategy.technical import htf_core

    kl = _slow_grind()
    core = htf_core(kl)
    core["atr"] = 0.0
    res = htf_context(kl, core=core)
    ev = res["evidence"]
    assert res["reject_reason"] == "flat"
    assert ev["flat_abs"] is True
    assert ev["flat_norm"] is None
    assert ev["atr_pct_4h"] is None


def test_trend_sample_both_gates_pass():
    """趋势样本：两口径都不判走平，方向正常给出"""
    res = htf_context(_trend_up())
    ev = res["evidence"]
    assert res["reject_reason"] is None
    assert res["direction"] == "long"
    assert ev["flat_abs"] is False
    assert ev["flat_norm"] is False
    assert ev["slope_atr_ratio"] > 5


def test_floor_prevents_infinite_loosening():
    """下限保护：ATR 极小时不允许把门槛放到 0（floor=0.2%）"""
    assert SLOPE_FLAT_FLOOR_PCT == 0.002
    res = htf_context(_slow_grind())
    ev = res["evidence"]
    # 影子门槛 = max(floor, k×atr_pct)；位移 0.275% > floor 0.2% → 放行
    assert ev["slope_pct"] > SLOPE_FLAT_FLOOR_PCT
