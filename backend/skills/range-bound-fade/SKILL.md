---
name: range-bound-fade
description: 震荡区间高抛低吸：区间边界角色 + 反向金K 反向开仓
use_when: signal_type == "range_bound"
version: 1
---
## 震荡区间高抛低吸打法

**前提**：`signal_type == "range_bound"`，区间边界已由关键位模块算出（range_top/range_bottom）。

**入场**：区间底部出现看涨金K → 做多（锚点 range_bottom）；区间顶部出现看跌金K → 做空（锚点 range_top）。止损锚点放区间外侧（做多止损 = range_bottom - 0.5×ATR，offset 为负）。

**止盈**：tp1 = 区间中线（range_top 与 range_bottom 的中点，可用 offset 表达）；tp2 = 对侧边界。

**区间收敛（高点降低 + 低点抬高）**：突破方向不确定，仓位减半（recommendation 降 20）。

**破位失效**：收盘价出界后原支撑变压力、原压力变支撑，不再做回归交易——此时应 skip 或按趋势反转打法（reversal-confirmation）评估。

**风险**：区间震荡市动能弱，信号强度普遍不高；量能萎缩（缩量）时区间维持概率更高，缩量加分。
