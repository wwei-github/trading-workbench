"""信号类型枚举"""
from __future__ import annotations

# 下跌趋势突破
DOWNTREND_BREAKOUT = "downtrend_breakout"
# 区间震荡
RANGE_BOUND = "range_bound"
# 上涨趋势回调
UPTREND_PULLBACK = "uptrend_pullback"

ALL_TYPES = [DOWNTREND_BREAKOUT, RANGE_BOUND, UPTREND_PULLBACK]

LABEL_MAP = {
    DOWNTREND_BREAKOUT: "下跌突破",
    RANGE_BOUND: "区间震荡",
    UPTREND_PULLBACK: "上涨回调",
}
