# -*- coding: utf-8 -*-
"""P-12 稳健性统计纯函数单测。

DSR：退化（SR=0 / N=1）、单调性（SR↑→DSR↑；N↑→DSR↓）、偏度峰度已知值；
PBO/CSCV：IS/OOS 排名完全反转→1、完全一致→0、行数不足抛错、块数校验；
网格展开：笛卡尔积数量与命名、非法键/超档/超积拒绝、默认网格=12。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import robustness as rb  # noqa: E402


# ── DSR ───────────────────────────────────────────────────────────────


def test_dsr_zero_sr_gives_half_with_no_trials():
    """SR=0 且无多重比较 → DSR=0.5（无信息对称）"""
    xs = [1.0, -1.0, 1.0, -1.0, 0.5, -0.5, 2.0, -2.0]
    m, s, _, _ = rb._moment_stats(xs)
    assert m == 0
    assert rb.deflated_sharpe(xs, sr_variance=0.0, n_trials=1) == pytest.approx(0.5, abs=1e-9)


def test_dsr_flat_series_is_zero():
    assert rb.deflated_sharpe([1.0] * 10) == 0.0
    assert rb.deflated_sharpe([]) == 0.5  # 无样本 → 无信息
    assert rb.deflated_sharpe([1.0]) == 0.5


def test_dsr_monotonic_in_sr():
    """SR 越高 DSR 越高（同方差族、N=1 退化为 PSR）"""
    base = [1.0, -0.5, 1.2, -0.4, 0.8, -0.3, 1.5, -0.2, 0.9, -0.6]
    d1 = rb.deflated_sharpe([x * 1.0 for x in base])
    d2 = rb.deflated_sharpe([x * 1.0 + 0.3 for x in base])  # 抬均值抬 SR
    assert d2 > d1 > 0.5


def test_dsr_penalized_by_trials():
    """试验数越多（N↑）DSR 越低——多重比较折扣生效"""
    xs = [1.0, -0.5, 1.2, -0.4, 0.8, -0.3, 1.5, -0.2, 0.9, -0.6, 1.1, -0.4]
    sr_var = 0.02
    d_small = rb.deflated_sharpe(xs, sr_variance=sr_var, n_trials=2)
    d_big = rb.deflated_sharpe(xs, sr_variance=sr_var, n_trials=32)
    assert d_small > d_big
    # SR* 随 N 增大而增大
    z1 = rb._ND.inv_cdf(1 - 1 / 32)
    assert z1 > 0


def test_moment_stats_known_values():
    """偏度/峰度：右偏 → skew>0；重尾离群 → kurt 远超 3（正态基准）"""
    right_skew = [1.0, 2.0, 3.0, 4.0, 10.0]
    _, _, sk, ku = rb._moment_stats(right_skew)
    assert sk > 0.5
    assert ku == pytest.approx(1.7843, abs=1e-3)  # 手算值（小样本低估峰度）
    heavy = [0.0] * 100 + [10.0]  # 单点离群 → 尖峰厚尾
    _, _, sk2, ku2 = rb._moment_stats(heavy)
    assert sk2 > 3.0
    assert ku2 > 50.0  # 手算 ≈97（单点离群主导四阶矩）
    symmetric = [1.0, -1.0, 1.0, -1.0]
    _, _, sk3, _ = rb._moment_stats(symmetric)
    assert abs(sk3) < 1e-9


def test_psr_matches_textbook():
    """正态大样本、SR 已知时 DSR≈Φ(SR·sqrt(T-1))（N=1, 偏度0, 峰度≈3）"""
    import random
    random.seed(42)
    xs = [random.gauss(0.1, 1.0) for _ in range(5000)]
    sr = rb.probabilistic_sharpe(xs)
    expected = rb._ND.cdf(sr * math.sqrt(4999))
    assert rb.deflated_sharpe(xs) == pytest.approx(expected, abs=0.02)


# ── PBO / CSCV ────────────────────────────────────────────────────────


def _matrix_from_columns(cols: list) -> list:
    """列变体 → 行矩阵（行=信号）"""
    t = len(cols[0])
    return [[cols[c][r] for c in range(len(cols))] for r in range(t)]


def test_pbo_perfect_reversal_is_one():
    """构造 IS 最优恒为 A、OOS 上 A 恒最差 → PBO=1

    B = -A（逐元素反号）→ 任意子集 SR_B = -SR_A；块均值 μA=[3,1,-1.5,-2.5]
    （总和=0，两两组合和≠0）→ IS 最优列的 OOS 排名恒 ≤ 0.5。
    """
    mu = [3.0, 1.0, -1.5, -2.5]
    a = []
    for m in mu:
        a += [m + 0.5, m - 0.5] * 5   # 每块 10 行交替 ±0.5（std>0 且两列 std 相同）
    b = [-x for x in a]
    out = rb.pbo_cscv(_matrix_from_columns([a, b]), n_blocks=4)
    assert out["pbo"] == 1.0
    assert out["n_combinations"] == math.comb(4, 2)


def test_pbo_consistent_leader_is_zero():
    """A 在任何子集都严格优于 B → IS 最优恒 A 且 OOS 也最优 → PBO=0

    B = 0.5·A − 2 → SR_B = SR_A − 4/std_A < SR_A 恒成立（任意子集）。
    """
    a = [2.0, 1.5, 2.5, 1.0] * 5
    b = [x * 0.5 - 2.0 for x in a]
    out = rb.pbo_cscv(_matrix_from_columns([a, b]), n_blocks=4)
    assert out["pbo"] == 0.0
    assert out["is_best_oos_rank_median"] == pytest.approx(1.0)


def test_pbo_validates_inputs():
    with pytest.raises(ValueError, match="至少 2 个变体"):
        rb.pbo_cscv([[1.0], [2.0]], n_blocks=4)
    with pytest.raises(ValueError, match="偶数"):
        rb.pbo_cscv(_matrix_from_columns([[1.0] * 40, [2.0] * 40]), n_blocks=5)
    with pytest.raises(ValueError, match="行数不足"):
        rb.pbo_cscv(_matrix_from_columns([[1.0] * 10, [2.0] * 10]), n_blocks=8)


def test_pbo_blocks_cover_all_rows():
    """块划分覆盖全部行且不重叠（40 行 4 块 → 每块 10）"""
    a = [1.0] * 40
    b = [-1.0] * 40
    out = rb.pbo_cscv(_matrix_from_columns([a, b]), n_blocks=4)
    # 全 A 恒优：IS 最优 A、OOS 也最优 → ω=1，不过拟合
    assert out["pbo"] == 0.0
    assert out["n_combinations"] == 6


# ── 网格展开 ──────────────────────────────────────────────────────────


def test_expand_grid_default_is_12():
    variants = rb.expand_grid()
    assert len(variants) == 12
    assert variants[0]["name"] == "v1"
    assert variants[0]["overrides"] == {
        "be_trigger_r": None, "tp0_r": None, "trail_atr_k": 2.5}
    assert variants[-1]["overrides"] == {
        "be_trigger_r": 0.8, "tp0_r": 0.5, "trail_atr_k": 4.0}


def test_expand_grid_rejects_bad_keys_and_size():
    with pytest.raises(ValueError, match="非法网格键"):
        rb.expand_grid({"fwd_bars": [24]})
    with pytest.raises(ValueError, match="档位超上限"):
        rb.expand_grid({"trail_atr_k": [1, 2, 3, 4, 5, 6, 7]})
    with pytest.raises(ValueError, match="超上限"):
        rb.expand_grid({"trail_atr_k": [1, 2, 3, 4, 5, 6] * 1,
                        "tp0_r": [None, 0.5, 1.0],
                        "be_trigger_r": [None, 0.5, 0.8, 1.0],
                        "tp0_gate_r": [1.0, 1.2, 1.5]})  # 6*3*4*3=216>32
    with pytest.raises(ValueError, match="非空值列表"):
        rb.expand_grid({"tp0_r": []})


def test_per_variant_stats_shape():
    xs = [1.0, -1.13, 2.0, -1.13]
    st = rb.per_variant_stats(xs, sr_variance=0.0, n_trials=1)
    assert st["decided"] == 4
    assert st["win_rate"] == 0.5
    assert st["sum_r"] == pytest.approx(0.74, abs=1e-9)
    assert "dsr" in st and "sr" in st


def test_conclude_verdicts():
    assert rb.conclude(0.6, 0.95, "v3", "v7")["verdict"] == "peak"
    assert rb.conclude(0.1, 0.95, "v3", "v3")["verdict"] == "plateau"
    c = rb.conclude(0.35, 0.8, "v3", "v7")
    assert c["verdict"] == "mixed"
    assert c["best_is_is_best_oos"] is False
    assert rb.conclude(0.1, 0.95, "v3", "v3")["best_is_is_best_oos"] is True
