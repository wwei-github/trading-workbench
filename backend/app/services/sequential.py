"""序贯统计检验（docs/16 P-08）——影子卡升格判据引擎，纯函数不读 DB。

背景（docs/14 §八）：影子卡原判据 = 「周期 ≥28 天 + decided ≥ min_sample」
固定窗口拍板。P-08 升级为序贯判据——样本每来一笔即更新一次检验，
达标即早停（省样本），失败即早停（省观察期），错误率仍受控于 α/β。

三件套（每张卡按预注册假设二选一或并用）：
1. sprt_bernoulli：Wald SPRT，H0: p=p0 vs H1: p=p1（单比例胜率/续走率类）
2. sprt_normal：均值的 SPRT（正态近似），H0: μ=μ0 vs H1: μ=μ0±δ（每笔 R 类）
3. beta_posterior：Jeffreys 先验 Beta(0.5,0.5) 后验——均值/95%CI/P(p<p0)，
   作 SPRT 的补充读数（贝叶斯等价区间，人工拍板时参考）

纪律（docs/14 §七 延伸）：
- p0/p1/δ/α/β **必须在样本积累前预注册**（写进标准卡），事后不可回调——
  序贯检验对边界参数敏感，事后挑参数等价于 p-hacking；
- α=0.05、β=0.20 为出厂默认（1−β=80% 功效），卡可覆盖但须在预注册时写明；
- 判读三态：h1（接受备择——升格/FAIL 方向）/ h0（接受原假设——卡可关闭）/
  continue（续观察）。序贯判读**不替代** §八 升格流程，只是把「等 28 天」
  换成有统计功效的早停依据。
"""
import math
from typing import Optional

DEFAULT_ALPHA = 0.05
DEFAULT_BETA = 0.20
_EPS = 1e-12  # p=0/1 边界的对数钳位


def _clamp(p: float) -> float:
    return min(max(float(p), _EPS), 1.0 - _EPS)


def sprt_bernoulli(wins: int, n: int, p0: float, p1: float,
                   alpha: float = DEFAULT_ALPHA,
                   beta: float = DEFAULT_BETA) -> dict:
    """单比例 Wald SPRT（逐笔 Bernoulli 胜/负流）。

    H0: p=p0 vs H1: p=p1（p1>p0 为「优于门槛」方向，p1<p0 为「劣于门槛」）。
    LLR = x·ln(p1/p0) + (n−x)·ln((1−p1)/(1−p0))
    边界：LLR ≥ ln((1−β)/α) → h1；LLR ≤ ln(β/(1−α)) → h0；否则 continue。
    n=0 → continue。返回 {verdict, llr, upper, lower, wins, n}。
    """
    n = int(n)
    wins = int(wins)
    upper = math.log((1.0 - beta) / alpha)
    lower = math.log(beta / (1.0 - alpha))
    if n <= 0:
        return {"verdict": "continue", "llr": 0.0, "upper": upper,
                "lower": lower, "wins": 0, "n": 0}
    p0c, p1c = _clamp(p0), _clamp(p1)
    llr = (wins * math.log(p1c / p0c)
           + (n - wins) * math.log((1.0 - p1c) / (1.0 - p0c)))
    if llr >= upper:
        verdict = "h1"
    elif llr <= lower:
        verdict = "h0"
    else:
        verdict = "continue"
    return {"verdict": verdict, "llr": round(llr, 4), "upper": round(upper, 4),
            "lower": round(lower, 4), "wins": wins, "n": n}


def sprt_normal(mean: float, n: int, sd: float, delta: float, mu0: float = 0.0,
                alpha: float = DEFAULT_ALPHA,
                beta: float = DEFAULT_BETA) -> dict:
    """均值 SPRT（正态近似，每笔 R 类读数：avg_r vs 0）。

    H0: μ=μ0 vs H1: μ=μ0+delta（delta<0 表示「显著为负」方向）。
    LLR = (delta/sd²)·(Σx − n·(μ0 + delta/2))
        = n·(delta/sd²)·(mean − μ0 − delta/2)
    边界同 Bernoulli。n<2 或 sd<=0 → continue（无尺度信息不下结论）。
    """
    n = int(n)
    upper = math.log((1.0 - beta) / alpha)
    lower = math.log(beta / (1.0 - alpha))
    if n < 2 or not sd or sd <= 0:
        return {"verdict": "continue", "llr": 0.0, "upper": round(upper, 4),
                "lower": round(lower, 4), "mean": mean, "n": n}
    sd = float(sd)
    if delta == 0:
        raise ValueError("delta 不能为 0（H0/H1 重合）")
    llr = n * (delta / (sd * sd)) * (float(mean) - mu0 - delta / 2.0)
    if llr >= upper:
        verdict = "h1"
    elif llr <= lower:
        verdict = "h0"
    else:
        verdict = "continue"
    return {"verdict": verdict, "llr": round(llr, 4), "upper": round(upper, 4),
            "lower": round(lower, 4), "mean": mean, "n": n}


# ── Beta 后验（Jeffreys 先验）────────────────────────────────────────────


def _log_beta_fn(a: float, b: float) -> float:
    """log B(a,b) = lgamma(a)+lgamma(b)−lgamma(a+b)"""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betainc_reg(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a,b)——数值积分实现（纯 stdlib）。

    t=s² 去奇异性替换：∫₀ˣ t^{a−1}(1−t)^{b−1}dt = ∫₀^√x 2s^{2a−1}(1−s²)^{b−1}ds，
    对 a≥0.5 被积函数 C^∞（后验参数恒 ≥0.5），复化 Simpson 千分位精度 ~1e-12；
    x>0.5 用对称式 I_x(a,b)=1−I_{1−x}(b,a) 折到左半平面（数值稳定）。
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > 0.5:
        return 1.0 - _betainc_reg(b, a, 1.0 - x)
    s_hi = math.sqrt(x)
    n = 1000
    h = s_hi / n
    a2 = 2.0 * a - 1.0  # s 指数：2s^{2a−1}

    def f(s: float) -> float:
        if s <= 0:
            return 2.0 if a2 == 0 else (0.0 if a2 > 0 else 2.0)
        return 2.0 * (s ** a2) * ((1.0 - s * s) ** (b - 1.0))

    total = f(0.0) + f(s_hi)
    for i in range(1, n):
        total += f(i * h) * (4.0 if i % 2 else 2.0)
    return min(max(total * h / 3.0 / math.exp(_log_beta_fn(a, b)), 0.0), 1.0)


def beta_posterior(wins: int, n: int,
                   prior_a: float = 0.5, prior_b: float = 0.5) -> dict:
    """Jeffreys 先验 Beta(prior_a, prior_b) 的后验摘要。

    后验 = Beta(prior_a + wins, prior_b + n − wins)。
    返回 {mean, ci95_low, ci95_high, n}——CI 为等尾 95% 分位（二分法反解
    I_x(a,b)=0.025/0.975，精确不依赖正态近似，小样本同样可用）。
    """
    a = prior_a + int(wins)
    b = prior_b + int(n) - int(wins)
    mean = a / (a + b)

    def _quantile(q: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2.0
            if _betainc_reg(a, b, mid) < q:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    return {
        "mean": round(mean, 4),
        "ci95_low": round(_quantile(0.025), 4),
        "ci95_high": round(_quantile(0.975), 4),
        "n": int(n),
    }


def sequential_verdict(wins: int, n: int, p0: float, p1: float, *,
                       alpha: float = DEFAULT_ALPHA,
                       beta: float = DEFAULT_BETA) -> dict:
    """比例卡复合判读：SPRT 三态 + Beta 后验补充读数（一次调用全出）。"""
    out = sprt_bernoulli(wins, n, p0, p1, alpha=alpha, beta=beta)
    out["p0"] = p0
    out["p1"] = p1
    out["posterior"] = beta_posterior(wins, n)
    return out


def sprt_normal_verdict(mean: float, n: int, sd: float, delta: float, *,
                        mu0: float = 0.0,
                        alpha: float = DEFAULT_ALPHA,
                        beta: float = DEFAULT_BETA) -> dict:
    """R 均值卡复合判读：normal SPRT + t 近似方向佐证（mean/sd 与 n 直读）。"""
    out = sprt_normal(mean, n, sd, delta, mu0=mu0, alpha=alpha, beta=beta)
    out["delta"] = delta
    out["mu0"] = mu0
    return out
