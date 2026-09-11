---
name: trend-pullback
description: 顺势回踩：趋势中回踩支撑/压力出现同向金K 顺势开仓
use_when: signal_type == "uptrend" or signal_type == "downtrend"
version: 1
---
## 顺势回踩打法

**前提**：趋势结构明确（uptrend=HH+HL / downtrend=LH+LL），且 EMA 形态与信号方向一致（EMA 多头排列支撑做多、空头排列支撑做空）。**EMA 与信号方向矛盾时直接 skip**。

**入场**：上涨趋势回踩支撑位出现看涨金K → 做多，锚点 = 该支撑位；下跌趋势反抽压力位出现看跌金K → 做空，锚点 = 该压力位。止损锚点 = 结构低点/高点（prev_low/prev_high），offset 外侧 0.5~1×ATR。

**止盈**：tp1 = 前高/前低（趋势内最近一个未突破的摆动点）；tp2 = 再前一个摆动点。要求复算 rr ≥ 1.5，达不到就 skip。

**加分项**：回踩缩量（量能分位 < 30%）说明抛压轻；关键位触及次数 ≥ 2 说明位置可靠。

**减分项**：回踩放量长阴/长阳击穿位置；EMA 刚死叉/拐头向下还做多。
