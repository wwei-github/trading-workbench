"""现货屯币指标库（docs/18 §3，纯函数——回测同构前置 §10.2）

约束：
- 全部纯函数：输入收盘价数组（旧→新），输出标量/元组，无 IO、无时间读取、
  无全局状态——实盘日检与回测循环调用同一份实现（PR 一票否决项）
- 数据不足返回 None，由引擎决定降级口径
- RSI 用 Wilder 平滑（TradingView 默认口径，对照验收 §10 P0）
"""
from typing import List, Optional, Tuple


def sma(closes: List[float], n: int) -> Optional[float]:
    """简单均线：最后 n 根均值。"""
    if len(closes) < n or n <= 0:
        return None
    return sum(closes[-n:]) / n


def rsi(closes: List[float], n: int = 14) -> Optional[float]:
    """Wilder RSI（TradingView ta.rsi 同口径）。"""
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    # 初始均值：前 n 段变动的简单平均
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / n
    avg_loss = losses / n
    # Wilder 平滑推进到末尾
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def boll(closes: List[float], n: int = 20, k: float = 2.0) -> Optional[Tuple[float, float, float]]:
    """布林带 (mid, upper, lower)。"""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((c - mid) ** 2 for c in window) / n
    sd = var ** 0.5
    return mid, mid + k * sd, mid - k * sd


def macd(
    closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[Tuple[float, float, float]]:
    """MACD (dif, dea, hist)：EMA12−EMA26 / 信号线 / 柱。数据不足返回 None。"""
    if len(closes) < slow + signal:
        return None

    def ema_series(vals: List[float], n: int) -> List[float]:
        k = 2.0 / (n + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    dif_series = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea_series = ema_series(dif_series, signal)
    dif = dif_series[-1]
    dea = dea_series[-1]
    return dif, dea, dif - dea


def macd_golden_cross(closes: List[float]) -> Optional[bool]:
    """最新一根是否刚金叉（前一根 dif≤dea 且当前 dif>dea）。

    返回 None=数据不足；True/False=当前处于金叉后状态（周K加分用「金叉后」宽松口径，
    与 docs/18 §3.2 '周MACD 金叉 +5' 一致——金叉后未死叉期间均视为多头发散）。
    """
    if len(closes) < 26 + 9 + 1:
        return None
    m = macd(closes)
    if m is None:
        return None
    return m[0] > m[1]


def drawdown_from_high(closes: List[float], n: int = 30) -> Optional[float]:
    """距 n 根最高收盘的回撤比例（负值，如 -0.22 = 回撤 22%）。"""
    if len(closes) < n or n <= 0:
        return None
    window = closes[-n:]
    hi = max(window)
    if hi <= 0:
        return None
    return closes[-1] / hi - 1.0


def rebound_from_low(closes: List[float], n: int = 30) -> Optional[float]:
    """距 n 根最低收盘的反弹比例（正值，如 0.35 = 反弹 35%）。"""
    if len(closes) < n or n <= 0:
        return None
    window = closes[-n:]
    lo = min(window)
    if lo <= 0:
        return None
    return closes[-1] / lo - 1.0


def price_vs_ma(closes: List[float], n: int) -> Optional[float]:
    """现价相对 MA(n) 的偏离比例（正=上方，如 0.08 = 高于均线 8%）。"""
    ma = sma(closes, n)
    if ma is None or ma <= 0:
        return None
    return closes[-1] / ma - 1.0
