"""策略包入口：摆动点结构工具（AI 事实包与锚点解析使用）。

各叶子模块按职责拆分：ema（均线形态）、indicators（量能）、swing（摆动点）、
candlestick（K线形态）、technical（技术指标投票+质量分）、trigger（入场触发）。

旧信号管线（detect_all_signals：Donchian/形态主动作 + EMA 硬门控）已随
指标驱动筛选重设计整体移除（2026-09-17）：方向判定权移交
technical.compute_indicator_votes（多数决），入场时机移交 trigger.detect_trigger。
"""
from __future__ import annotations

import numpy as np

from app.services.strategy.swing import find_swing_points, merge_swings

__all__ = ["recent_swings", "swing_annotations"]


def recent_swings(
    klines: list[list], order: int = 3, n: int = 2, price_source: str = "close",
) -> dict:
    """最近 n 个已确认摆动高点/低点，附 HH/LH/HL/LL 结构分类。

    默认收盘价摆动点，两侧各 order 根确认（与 AI 事实包/K线标注同源）。
    price_source="extreme" 时改用摆动K线自身的 high/low 归一（真极值口径，
    仅回踩 swing 触点消费，2026-10-27 docs/15 §17）——两者确认逻辑完全相同，
    差别只在「结构位价格取自哪一列」。

    返回: {"highs": [{"label","price","bars_ago","extreme"}...新→旧], "lows": [...]}
    分类规则与 Pine 结构标签一致：高点 HH(高于前高点)/LH(低于)，无前参照为 H；
    低点 HL(高于前低点)/LL(低于)，无前参照为 L。bars_ago 以最新一根（含未收盘）为 0。
    extreme 为该摆动K线自身的 high（高点序列）/ low（低点序列），供调用方做
    位价偏移诊断（|price-extreme|/extreme）。
    """
    use_extreme = (price_source or "close").strip().lower() == "extreme"
    # 真极值恒抽出（extreme 列是诊断口径，与 price 取哪一列无关）
    hs = np.array([float(k[2]) for k in klines], dtype=float)
    ls = np.array([float(k[3]) for k in klines], dtype=float)
    if use_extreme:
        highs, lows = hs, ls
    else:
        closes = np.array([float(k[4]) for k in klines], dtype=float)
        highs = lows = closes
    if len(highs) < 2 * order + 1:
        return {"highs": [], "lows": []}
    high_idx, low_idx = find_swing_points(highs, lows, order)
    swings = merge_swings(high_idx, low_idx, highs, lows)
    n_bars = len(klines)

    def _build(seq: list[tuple[int, str, float]], kind: str) -> list[dict]:
        out: list[dict] = []
        for i in range(len(seq) - 1, -1, -1):  # 新 → 旧
            if len(out) >= n:
                break
            idx, _, price = seq[i]
            prev = seq[i - 1][2] if i > 0 else None
            if kind == "H":
                label = "H" if prev is None else ("HH" if price > prev else "LH")
            else:
                label = "L" if prev is None else ("HL" if price > prev else "LL")
            ext = float(hs[idx]) if kind == "H" else float(ls[idx])
            out.append({"label": label, "price": price, "bars_ago": n_bars - 1 - idx,
                        "extreme": ext})
        return out

    return {
        "highs": _build([s for s in swings if s[1] == "H"], "H"),
        "lows": _build([s for s in swings if s[1] == "L"], "L"),
    }


def swing_annotations(klines: list[list], order: int = 3, n: int = 5) -> dict:
    """最近摆动点 → 图表标注格式（time/price/label；bars_ago 换算回时间戳）。

    K线接口标注共用（scan.py 扫描图 / trades.py 复盘图），单一事实源防漂移。
    """
    sw = recent_swings(klines, order=order, n=n)
    n_bars = len(klines)
    return {
        kind: [
            {
                "time": int(klines[n_bars - 1 - it["bars_ago"]][0]),
                "price": it["price"],
                "label": it["label"],
            }
            for it in sw[kind]
        ]
        for kind in ("highs", "lows")
    }
