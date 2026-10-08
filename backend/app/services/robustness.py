"""参数稳健性统计（docs/16 P-12）——纯函数核心，不读 DB 不调交易所。

三个折扣判据（Bailey & López de Prado 2015 口径，每笔 R 记账、非年化）：
1. deflated_sharpe：把「已试 N 个变体」折进显著性的 Sharpe 检验——观测 SR
   超过 N 重比较下期望最大 SR（SR*）的概率；
2. pbo_cscv：组合对称交叉验证（CSCV）——IS 最优变体在 OOS 掉到中位数以下
   的组合占比，即回测过拟合概率 PBO；网格看「高原」不看「山峰」的直接读数；
3. conclude：PBO × DSR × best_is/best_oos 对照 → plateau/peak/mixed 判读。

本模块只做算术：R 矩阵由任务层（backtest_tasks）用 P-06 引擎逐变体重放后
对齐传入。不做组合模拟、不自动改任何生产参数（报告给人，拍板走 §0 流程）。
"""
import itertools
import math
from statistics import NormalDist
from typing import Optional

# 引擎 overrides 中允许进网格的键（fwd_bars/fee_rate 是语义性参数，
# 进网格会改变「比较的是同一出场规则族」的前提，排除）
GRID_KEYS = (
    "be_trigger_r", "tp0_r", "tp0_qty_pct", "tp0_gate_r",
    "trail_atr_k", "trail_swing_bars", "atr_period",
)

# 默认出厂网格：保本武装 × 微止盈 × ATR 跟进松紧 = 12 变体
# （CARD-05 / CARD-06 影子卡对应参数 + ATR k 敏感性三档）
DEFAULT_GRID = {
    "be_trigger_r": [None, 0.8],
    "tp0_r": [None, 0.5],
    "trail_atr_k": [2.5, 3.0, 4.0],
}

MAX_GRID_AXES_VALUES = 6   # 每轴最多 6 档
MAX_GRID_SIZE = 32         # 网格变体总数上限（单任务成本）
DEFAULT_N_BLOCKS = 8       # CSCV 块数（偶数）
_MIN_ROWS_PER_BLOCK = 5    # 每块至少 5 行样本，否则 CSCV 无意义

_EULER_GAMMA = 0.5772156649015329
_ND = NormalDist()


# ── 单序列统计 ─────────────────────────────────────────────────────────


def _moment_stats(xs: list) -> tuple:
    """返回 (mean, std, skew, kurt_non_excess)；std/skew/kurt 不足时按正态默认。"""
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0, 3.0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0, 0.0, 3.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return mean, 0.0, 0.0, 3.0
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    skew = m3 / std**3
    kurt = m4 / std**4  # 非超额（正态=3），Bailey 公式口径
    return mean, std, skew, kurt


def probabilistic_sharpe(r_series: list) -> float:
    """每笔概率论 Sharpe（PSR 基础量）：mean/std；退化序列返回 0。"""
    mean, std, _, _ = _moment_stats(r_series)
    return mean / std if std > 0 else 0.0


def deflated_sharpe(r_series: list, sr_variance: Optional[float] = None,
                    n_trials: int = 1) -> float:
    """Deflated Sharpe Ratio：P(SR* < 观测 SR)——把 N 重比较折进去的显著性。

    r_series: 该变体每笔 R（decided，时间序）
    sr_variance: 跨变体 SR 的样本方差（None=由调用方各变体 SR 算，N=1 时取 0）
    n_trials: 网格变体总数 N

    SR* = sqrt(V) · ((1-γ)·Z(1-1/N) + γ·Z(1-1/(N·e)))，γ=欧拉常数
    DSR = Φ( (SR-SR*)·sqrt(T-1) / sqrt(1 - γ3·SR + ((γ4-1)/4)·SR²) )
    退化：std=0 → 0.0；T<2 → 0.5（无信息）。
    """
    n = len(r_series)
    if n < 2:
        return 0.5
    mean, std, skew, kurt = _moment_stats(r_series)
    if std <= 0:
        return 0.0
    sr = mean / std
    n_trials = max(1, int(n_trials))
    if sr_variance is None:
        sr_variance = 0.0
    v = max(0.0, float(sr_variance))
    if n_trials == 1:
        sr_star = 0.0
    else:
        z1 = _ND.inv_cdf(1.0 - 1.0 / n_trials)
        z2 = _ND.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
        sr_star = math.sqrt(v) * ((1 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return 1.0 if sr > sr_star else 0.0
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return _ND.cdf(z)


# ── PBO / CSCV ────────────────────────────────────────────────────────


def _sr(xs: list) -> float:
    """子集每笔 Sharpe；退化（std=0）返回 0（排名时按中位处理无碍——极罕见）。"""
    mean, std, _, _ = _moment_stats(xs)
    return mean / std if std > 0 else 0.0


def pbo_cscv(r_matrix: list, n_blocks: int = DEFAULT_N_BLOCKS) -> dict:
    """组合对称交叉验证 PBO。

    r_matrix: T×N 矩阵（行=信号，按时间序；列=变体；值=每笔 R，全 decided 行）
    返回 {"n_blocks", "n_combinations", "pbo", "is_best_oos_rank_median",
          "overfit_combos", "excluded_rows"}

    ω = IS 最优变体在 OOS 的相对排名（1=最优）；ω ≤ 0.5 记过拟合选择。
    """
    t = len(r_matrix)
    n_var = len(r_matrix[0]) if r_matrix else 0
    if n_var < 2:
        raise ValueError("PBO 需要至少 2 个变体")
    if n_blocks % 2 != 0 or not (4 <= n_blocks <= 16):
        raise ValueError("n_blocks 须为 4~16 的偶数")
    if t < n_blocks * _MIN_ROWS_PER_BLOCK:
        raise ValueError(
            f"矩阵行数不足：{t} 行 < {n_blocks}块×{_MIN_ROWS_PER_BLOCK}；"
            "扩 days 或放宽过滤后重试")

    # 时间序切块（块大小尽量均匀，余数摊前块）
    base, rem = divmod(t, n_blocks)
    blocks, start = [], 0
    for i in range(n_blocks):
        size = base + (1 if i < rem else 0)
        blocks.append(list(range(start, start + size)))
        start += size

    overfit = 0
    oos_ranks = []
    n_combos = 0
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_rows = [r for b in is_idx for r in blocks[b]]
        oos_rows = [r for b in range(n_blocks) if b not in is_idx for r in blocks[b]]
        n_combos += 1
        is_sr = [_sr([r_matrix[r][c] for r in is_rows]) for c in range(n_var)]
        oos_sr = [_sr([r_matrix[r][c] for r in oos_rows]) for c in range(n_var)]
        best = max(range(n_var), key=lambda c: (is_sr[c], -c))
        better = sum(1 for c in range(n_var) if oos_sr[c] > oos_sr[best])
        tie = sum(1 for c in range(n_var) if c != best and oos_sr[c] == oos_sr[best])
        # 相对排名：严格更优的占比 + 并列折半（保守向 0.5 靠）
        omega = 1.0 - (better + tie / 2.0) / n_var
        oos_ranks.append(omega)
        if omega <= 0.5:
            overfit += 1

    oos_ranks.sort()
    mid = len(oos_ranks) // 2
    rank_median = (oos_ranks[mid - 1] + oos_ranks[mid]) / 2 if n_combos % 2 == 0 \
        else oos_ranks[mid]
    return {
        "n_blocks": n_blocks,
        "n_combinations": n_combos,
        "pbo": overfit / n_combos,
        "is_best_oos_rank_median": rank_median,
        "overfit_combos": overfit,
        "excluded_rows": 0,
    }


# ── 网格展开与判读 ─────────────────────────────────────────────────────


def expand_grid(grid: Optional[dict] = None) -> list:
    """网格 dict → 变体列表 [{name, overrides}]（笛卡尔积，键序稳定）。

    非法键 / 每轴超档 / 积超上限 / 空网格 → ValueError（API 层转 400）。
    """
    g = dict(DEFAULT_GRID) if not grid else dict(grid)
    if not g:
        raise ValueError("网格为空")
    bad = [k for k in g if k not in GRID_KEYS]
    if bad:
        raise ValueError(f"非法网格键: {bad}（合法: {GRID_KEYS}）")
    keys = sorted(g.keys())
    total = 1
    for k in keys:
        vals = g[k]
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"网格 {k} 须为非空值列表")
        if len(vals) > MAX_GRID_AXES_VALUES:
            raise ValueError(f"网格 {k} 档位超上限（≤{MAX_GRID_AXES_VALUES}）")
        total *= len(vals)
    if total > MAX_GRID_SIZE:
        raise ValueError(f"网格变体总数 {total} 超上限 {MAX_GRID_SIZE}")

    out = []
    for i, combo in enumerate(itertools.product(*[g[k] for k in keys])):
        overrides = {k: v for k, v in zip(keys, combo)}
        out.append({"name": f"v{i + 1}", "overrides": overrides})
    return out


def per_variant_stats(r_series: list, sr_variance: Optional[float],
                      n_trials: int) -> dict:
    """单变体统计包：decided/exp_r/sum_r/win_rate/sr/skew/kurt/dsr。"""
    n = len(r_series)
    mean, std, skew, kurt = _moment_stats(r_series)
    sr = mean / std if std > 0 else 0.0
    return {
        "decided": n,
        "exp_r": round(mean, 4) if n else 0.0,
        "sum_r": round(sum(r_series), 4) if n else 0.0,
        "win_rate": round(sum(1 for x in r_series if x > 0) / n, 4) if n else 0.0,
        "sr": round(sr, 4),
        "skew": round(skew, 4),
        "kurt": round(kurt, 4),
        "dsr": round(deflated_sharpe(r_series, sr_variance, n_trials), 4),
    }


def conclude(pbo: float, dsr_best: float, best_is: str, best_oos: str) -> dict:
    """PBO × DSR × IS/OOS 对照 → 判读。"""
    same = best_is == best_oos
    if pbo >= 0.5:
        verdict = "peak"
        text = (f"PBO={pbo:.2f}：样本内最优（{best_is}）多半是过拟合山峰——"
                "禁止把该参数当规律，扩大样本或换轴重扫。")
    elif pbo <= 0.2 and dsr_best >= 0.9:
        verdict = "plateau"
        text = (f"PBO={pbo:.2f}、最优变体 DSR={dsr_best:.2f}：网格顶部是高原，"
                f"IS 最优（{best_is}）经 N 重比较折扣后仍显著"
                + ("且 OOS 最优同变体。" if same else f"；OOS 最优为 {best_oos}，"
                   "注意两变体差异是否实质（通常在噪音带内）。"))
    else:
        verdict = "mixed"
        text = (f"PBO={pbo:.2f}、最优变体 DSR={dsr_best:.2f}：证据不足——"
                "需要更多 decided 样本（扩 days）或收窄网格后重扫；"
                "IS 最优（{b}）暂不可采信。".replace("{b}", best_is))
    return {"verdict": verdict, "text": text,
            "best_is": best_is, "best_oos": best_oos,
            "best_is_is_best_oos": same}
