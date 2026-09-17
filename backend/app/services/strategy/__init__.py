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

__all__ = ["recent_swings"]


def recent_swings(klines: list[list], order: int = 3, n: int = 2) -> dict:
    """最近 n 个已确认摆动高点/低点，附 HH/LH/HL/LL 结构分类。

    收盘价摆动点，两侧各 order 根确认。
    返回: {"highs": [{"label","price","bars_ago"}...新→旧], "lows": [...]}
    分类规则与 Pine 结构标签一致：高点 HH(高于前高点)/LH(低于)，无前参照为 H；
    低点 HL(高于前低点)/LL(低于)，无前参照为 L。bars_ago 以最新一根（含未收盘）为 0。
    """
    closes = np.array([float(k[4]) for k in klines], dtype=float)
    if len(closes) < 2 * order + 1:
        return {"highs": [], "lows": []}
    high_idx, low_idx = find_swing_points(closes, closes, order)
    swings = merge_swings(high_idx, low_idx, closes, closes)
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
            out.append({"label": label, "price": price, "bars_ago": n_bars - 1 - idx})
        return out

    return {
        "highs": _build([s for s in swings if s[1] == "H"], "H"),
        "lows": _build([s for s in swings if s[1] == "L"], "L"),
    }
