"""策略统一入口：依次尝试三种信号，返回首个命中（或全部命中）"""
from __future__ import annotations

from typing import Optional
from app.services.strategy import downtrend_breakout, range_bound, uptrend_pullback
from app.services.strategy.types import ALL_TYPES, LABEL_MAP

__all__ = [
    "detect_all_signals",
    "detect_any_signal",
    "ALL_TYPES",
    "LABEL_MAP",
]


def detect_all_signals(klines: list[list], config: dict) -> list[dict]:
    """检测所有命中的信号，返回列表"""
    results = []
    for detector in (downtrend_breakout, range_bound, uptrend_pullback):
        try:
            r = detector.detect(klines, config)
            if r:
                results.append(r)
        except Exception:
            continue
    return results


def detect_any_signal(klines: list[list], config: dict) -> Optional[dict]:
    """检测任意命中信号，返回首个命中（优先级：下跌突破 > 区间震荡 > 上涨回调）"""
    for detector in (downtrend_breakout, range_bound, uptrend_pullback):
        try:
            r = detector.detect(klines, config)
            if r:
                return r
        except Exception:
            continue
    return None
