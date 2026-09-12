"""信号类型、位置、形态枚举"""
from __future__ import annotations

# ===== 市场结构分类（新逻辑，见 docs/03-关键位筛选重构需求.md）=====
UPTREND = "uptrend"                # 上涨趋势（HH+HL）
DOWNTREND = "downtrend"            # 下跌趋势（LH+LL）
TREND_REVERSAL = "trend_reversal"  # 趋势反转（趋势中收盘破前高/前低，123法则第②步）
RANGE_BOUND = "range_bound"        # 震荡区间（水平/收敛/高低点方向冲突）
UNKNOWN = "unknown"                # 未分类（摆动点不足或无法识别）

# ===== 旧版类型（仅用于历史数据兼容展示）=====
DOWNTREND_BREAKOUT = "downtrend_breakout"
UPTREND_PULLBACK = "uptrend_pullback"

STRUCTURE_TYPES = [UPTREND, DOWNTREND, TREND_REVERSAL, RANGE_BOUND, UNKNOWN]
ALL_TYPES = STRUCTURE_TYPES + [DOWNTREND_BREAKOUT, UPTREND_PULLBACK]

LABEL_MAP = {
    UPTREND: "上涨趋势",
    DOWNTREND: "下跌趋势",
    TREND_REVERSAL: "趋势反转",
    RANGE_BOUND: "震荡区间",
    UNKNOWN: "未分类",
    # 旧值（历史数据）
    DOWNTREND_BREAKOUT: "下跌突破",
    UPTREND_PULLBACK: "上涨回调",
}

# ===== 信号类型补充 =====
BREAKOUT = "breakout"                # 放量突破（收盘越过整个关键位区域 + 量能≥1.2×均量 + EMA 同向）

# ===== 关键位类型（scan_results.position 取值）=====
# 【2026-09-12 两类化】关键位只保留支撑/压力两类，kind 由角色动态推导（位在现价
# 上方=resistance、下方=support，与 role 同源）。前高/前低/区间顶底不再作为新值产生，
# 仅用于历史数据兼容展示
POS_SUPPORT = "support"              # 支撑位
POS_RESISTANCE = "resistance"        # 压力位
# 旧值（历史数据兼容展示，新扫描不再产生）
POS_PREV_HIGH = "prev_high"          # 前高
POS_PREV_LOW = "prev_low"            # 前低
POS_RANGE_TOP = "range_top"          # 区间顶部
POS_RANGE_BOTTOM = "range_bottom"    # 区间底部

POSITION_LABEL_MAP = {
    POS_SUPPORT: "支撑位",
    POS_RESISTANCE: "压力位",
    # 旧值（历史数据）
    POS_PREV_HIGH: "前高",
    POS_PREV_LOW: "前低",
    POS_RANGE_TOP: "区间顶部",
    POS_RANGE_BOTTOM: "区间底部",
}
