# -*- coding: utf-8 -*-
"""P-08 序贯统计检验纯函数单测。

SPRT-Bernoulli：强信号早停 h1 / 强 null 早停 h0 / 邻近门槛 continue /
n=0 continue / 单调性（胜越多越向 h1）；SPRT-Normal：负均值大样本 h1 /
正均值大样本 h0 / 邻界 continue / n<2 或 sd=0 continue；Beta 后验：
Beta(1,1) 均匀分布精确分位、对称二项 CI 覆盖 0.5、Jeffreys 先验下
wins=0 → mean 偏低；_betainc_reg 精确已知值（Beta(1,1)=x、Beta(0.5,0.5)
中点 0.5、对称恒等式）。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import sequential as sq  # noqa: E402


# ── SPRT-Bernoulli ────────────────────────────────────────────────────


def test_sprt_b_strong_alt_stops_h1():
    """真实率 0.65 vs H1 0.60/H0 0.40：120 笔 78 胜 → 应早停 h1"""
    out = sq.sprt_bernoulli(78, 120, p0=0.40, p1=0.60)
    assert out["verdict"] == "h1"
    assert out["llr"] >= out["upper"]


def test_sprt_b_strong_null_stops_h0():
    """真实率 0.30（门槛 0.40）：100 笔 30 胜 → 早停 h0（卡可 FAIL 关闭）"""
    out = sq.sprt_bernoulli(30, 100, p0=0.40, p1=0.60)
    assert out["verdict"] == "h0"
    assert out["llr"] <= out["lower"]


def test_sprt_b_near_threshold_continues():
    """LLR=0 的交叉点在 x=n/2（非 x=np0）：29/60 → 界内 continue"""
    out = sq.sprt_bernoulli(29, 60, p0=0.40, p1=0.60)
    assert out["verdict"] == "continue"
    assert out["lower"] < out["llr"] < out["upper"]


def test_sprt_b_zero_samples_and_monotonic():
    assert sq.sprt_bernoulli(0, 0, 0.40, 0.60)["verdict"] == "continue"
    # 胜越多 LLR 单调升
    lls = [sq.sprt_bernoulli(w, 50, 0.40, 0.60)["llr"] for w in range(0, 51)]
    assert all(b > a for a, b in zip(lls, lls[1:]))
    # 全负 → h0；全胜 → h1
    assert sq.sprt_bernoulli(0, 50, 0.40, 0.60)["verdict"] == "h0"
    assert sq.sprt_bernoulli(50, 50, 0.40, 0.60)["verdict"] == "h1"


def test_sprt_b_downward_direction():
    """p1<p0（负期望假设，S-01 方向）：p̂ 恰落 p1 且 n 足够 → h1（20/80）"""
    assert sq.sprt_bernoulli(10, 40, p0=0.40, p1=0.25)["verdict"] == "continue"
    out = sq.sprt_bernoulli(20, 80, p0=0.40, p1=0.25)
    assert out["verdict"] == "h1"


def test_sprt_b_boundary_values():
    """α=β 调宽后边界外移：同样 LLR 在更松的 α/β 下可能提前 h1"""
    strict = sq.sprt_bernoulli(60, 100, 0.40, 0.60, alpha=0.01, beta=0.05)
    loose = sq.sprt_bernoulli(60, 100, 0.40, 0.60, alpha=0.10, beta=0.30)
    order = {"h0": 0, "continue": 1, "h1": 2}
    assert order[loose["verdict"]] >= order[strict["verdict"]]


# ── SPRT-Normal ───────────────────────────────────────────────────────


def test_sprt_n_negative_mean_stops_h1():
    """avg_r=-0.5、sd=1.0、δ=-0.3：n=80 → 显著为负 → h1"""
    out = sq.sprt_normal(-0.5, 80, 1.0, delta=-0.3)
    assert out["verdict"] == "h1"


def test_sprt_n_positive_mean_stops_h0():
    """avg_r=+0.4：δ=-0.3 方向下大样本 → 拒绝「负期望」假设 → h0"""
    out = sq.sprt_normal(0.4, 80, 1.0, delta=-0.3)
    assert out["verdict"] == "h0"


def test_sprt_n_near_boundary_continues():
    """avg_r 压在 μ0+δ/2（-0.15）→ LLR≈0 → continue"""
    out = sq.sprt_normal(-0.15, 40, 1.0, delta=-0.3)
    assert out["verdict"] == "continue"


def test_sprt_n_degenerate_inputs():
    assert sq.sprt_normal(0.0, 1, 1.0, -0.3)["verdict"] == "continue"
    assert sq.sprt_normal(0.0, 50, 0.0, -0.3)["verdict"] == "continue"
    assert sq.sprt_normal(0.0, 0, 1.0, -0.3)["verdict"] == "continue"
    with pytest.raises(ValueError):
        sq.sprt_normal(0.0, 50, 1.0, delta=0.0)


# ── 正则化不完全 Beta ─────────────────────────────────────────────────


def test_betainc_uniform_exact():
    """Beta(1,1)=均匀分布 → I_x(1,1)=x 精确成立"""
    for x in (0.1, 0.25, 0.5, 0.7, 0.99):
        assert sq._betainc_reg(1.0, 1.0, x) == pytest.approx(x, abs=1e-7)


def test_betainc_jeffreys_midpoint():
    """Beta(0.5,0.5)（弧分布）中点：I_0.5 = 0.5（对称）"""
    assert sq._betainc_reg(0.5, 0.5, 0.5) == pytest.approx(0.5, abs=1e-7)


def test_betainc_symmetry_identity():
    """I_x(a,b) + I_{1-x}(b,a) = 1"""
    for x in (0.2, 0.4, 0.6, 0.8):
        lhs = sq._betainc_reg(2.5, 4.0, x) + sq._betainc_reg(4.0, 2.5, 1.0 - x)
        assert lhs == pytest.approx(1.0, abs=1e-9)


# ── Beta 后验 ─────────────────────────────────────────────────────────


def test_posterior_uniform_prior_matches_mle_shape():
    """Beta(1,1) 先验 → 后验均值 = (wins+1)/(n+2)；CI 覆盖 MLE"""
    wins, n = 43, 100
    post = sq.beta_posterior(wins, n, prior_a=1.0, prior_b=1.0)
    assert post["mean"] == pytest.approx((wins + 1) / (n + 2), abs=1e-3)
    assert post["ci95_low"] < wins / n < post["ci95_high"]


def test_posterior_jeffreys_all_losses():
    """Jeffreys 先验下全负样本：均值被先验托离 0（MLE 为 0）且上界收敛"""
    post = sq.beta_posterior(0, 20)
    assert 0.0 < post["mean"] < 0.05  # MLE=0，后验均值 ~0.024
    assert post["ci95_high"] < 0.15


def test_posterior_zero_samples_is_prior():
    """n=0 → 后验即先验 Beta(0.5,0.5)：均值 0.5、宽 CI"""
    post = sq.beta_posterior(0, 0)
    assert post["mean"] == 0.5
    assert post["ci95_low"] < 0.1 and post["ci95_high"] > 0.9


def test_sequential_verdict_composite():
    out = sq.sequential_verdict(78, 120, p0=0.40, p1=0.60)
    assert out["verdict"] == "h1"
    assert out["posterior"]["ci95_low"] > 0.5
    out2 = sq.sprt_normal_verdict(-0.5, 80, 1.0, delta=-0.3)
    assert out2["verdict"] == "h1" and out2["delta"] == -0.3
