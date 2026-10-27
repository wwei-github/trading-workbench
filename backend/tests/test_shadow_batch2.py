# -*- coding: utf-8 -*-
"""影子打标：r2_slope（CARD-10，docs/16 S-10 立卡，2026-10-27 开庭升格）单测。

覆盖：三桶构造（r2_high / r2_mid / r2_low）、与 swing.linear_regression
复算一致（口径零漂移）、数据不足 None、注册表条目与 value_json 可序列化。

CARD-02 oi_conf / CARD-04 macd_div 已于 2026-10-27 开庭判 FAIL 关闭并删除代码
（docs/14 §八「关闭即删除」，归档见 §十一）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import shadow as shadow_mod  # noqa: E402
from app.services.shadow import SHADOW_FEATURES  # noqa: E402


def _mk_klines(closes):
    """K 线替身：high=close+0.5、low=close−0.5，末根未收盘占位（被剔除）"""
    body = [[0, c, c + 0.5, c - 0.5, c, 100.0] for c in closes]
    body.append([0, closes[-1], closes[-1], closes[-1], closes[-1], 0.0])
    return body


def test_r2_slope_buckets():
    """纯斜坡 r²≈1 → r2_high；对称 V 形 r²≈0 → r2_low；
    趋势+等幅震荡 r²≈0.66 → r2_mid（观察列）。r² 独立复算交叉验证"""
    import numpy as np

    from app.services.strategy.swing import linear_regression

    ramp = [100.0 + i for i in range(24)]                      # 强趋势
    vshape = [110.0 - i for i in range(12)] + [98.0 + i for i in range(12)]
    choppy = [100.0 + 0.5 * i + (2.5 if i % 2 else -2.5) for i in range(24)]

    hi = shadow_mod.compute_r2_slope(_mk_klines(ramp))
    lo = shadow_mod.compute_r2_slope(_mk_klines(vshape))
    mid = shadow_mod.compute_r2_slope(_mk_klines(choppy))
    assert hi["bucket"] == "r2_high" and hi["r2"] > 0.99
    assert lo["bucket"] == "r2_low" and lo["r2"] < 0.1
    assert mid["bucket"] == "r2_mid"

    # r²/slope 与 swing.linear_regression 直接复算一致（口径零漂移；
    # value_json 存 round 4 位，容差取舍入级）
    win = np.asarray(ramp, dtype=float)
    slope, _icpt, r2 = linear_regression(np.arange(24), win)
    assert hi["r2"] == pytest.approx(float(r2), abs=5e-5)
    assert hi["slope_pct"] == pytest.approx(
        float(slope) / float(win.mean()) * 100, abs=5e-5)


def test_r2_slope_insufficient_none():
    assert shadow_mod.compute_r2_slope(_mk_klines([100.0] * 23)) is None  # <24 已收盘
    assert shadow_mod.compute_r2_slope([]) is None


def test_r2_slope_registry_entry():
    """注册表条目 + 输出可直接进 value_json；min_sample=120 为展示口径
    （真实判据 = 判定桶各≥60，在 docs/14 §七 CARD-10 文本）"""
    f = SHADOW_FEATURES["r2_slope"]
    assert f.card == "CARD-10" and f.min_sample == 120
    v = f.compute(_mk_klines([100.0 + i for i in range(24)]))
    assert v is not None and set(v) == {"r2", "slope_pct", "bucket"}
    json.dumps(v)
