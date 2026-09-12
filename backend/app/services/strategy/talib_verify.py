"""TA-Lib 交叉验证（双源互证）

本地 12 金K 检测器针对加密货币做了刻意适配（7x24 无跳空、强度分级、
策略层关键位角色匹配），保留为主检测源；TA-Lib 作为第二意见：

- 方向与 TA-Lib 判定一致时 strength +0.1（封顶 1.0），打上 talib_confirmed 标记
- 不一致或 TA-Lib 未命中时不降权（本地定义对加密更宽松是刻意设计）
- 孕线突破是自定义三根结构，TA-Lib 无对应语义（CDLHARAMI 为两根孕线），跳过
- talib 未安装时整体跳过，不影响主流程
"""

import logging

logger = logging.getLogger(__name__)

try:
    import numpy as np
    import talib
except ImportError:
    talib = None

# 本地 pattern → (TA-Lib 函数名, 期望方向)
_TALIB_MAP = {
    # 看涨
    "hammer": ("CDLHAMMER", "bullish"),
    "bullish_engulfing": ("CDLENGULFING", "bullish"),
    "morning_star": ("CDLMORNINGSTAR", "bullish"),
    # 看跌
    "hanging_man": ("CDLHANGINGMAN", "bearish"),
    "bearish_engulfing": ("CDLENGULFING", "bearish"),
    "evening_star": ("CDLEVENINGSTAR", "bearish"),
}

_BOOST = 0.1


def boost_with_talib(klines: list, results: list[dict], idx: int = -2) -> list[dict]:
    """用 TA-Lib 复核本地形态检测结果，一致时提升 strength（原地修改并返回）

    klines: [[open_time, open, high, low, close, ...], ...]
    results: detect_all_patterns 的输出（含 pattern/direction/strength）
    idx: 形态所在 K 线索引（与 detect_all_patterns 一致，默认 -2 已收盘）
    """
    if talib is None or not results:
        return results

    pos = len(klines) + idx
    if pos < 0 or len(klines) < 5:
        return results

    try:
        o = np.array([float(k[1]) for k in klines], dtype=float)
        h = np.array([float(k[2]) for k in klines], dtype=float)
        low = np.array([float(k[3]) for k in klines], dtype=float)
        c = np.array([float(k[4]) for k in klines], dtype=float)

        for r in results:
            entry = _TALIB_MAP.get(r.get("pattern"))
            if entry is None or r.get("direction") != entry[1]:
                continue
            arr = getattr(talib, entry[0])(o, h, low, c)
            v = float(arr[pos])
            if v != v:  # NaN（回看窗口不足）
                continue
            if (v > 0 and entry[1] == "bullish") or (v < 0 and entry[1] == "bearish"):
                r["strength"] = min(r.get("strength", 0.5) + _BOOST, 1.0)
                r["talib_confirmed"] = True
    except Exception as e:
        logger.warning("TA-Lib 交叉验证失败（跳过，不影响主流程）: %s", e)

    return results
