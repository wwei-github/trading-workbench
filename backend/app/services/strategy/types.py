"""位置枚举（scan_results.position 取值）。

旧结构分类常量族（UPTREND/TREND_REVERSAL/LABEL_MAP 等）与 BREAKOUT 已随
指标驱动筛选重设计移除（2026-09-17）：signal_type 由 trigger.py 的
trigger_* 接管；历史行旧值仅前端 labels.ts 做兼容展示。
"""
from __future__ import annotations

# position 由信号方向派生：看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance。
# 前高/前低/区间顶底不再作为新值产生，仅用于历史数据兼容展示
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
