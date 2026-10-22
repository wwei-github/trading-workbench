# 配置项审计与去冗余（策略配置弹窗 / 交易配置弹窗）

## 1. 背景与目标

对「策略配置」（`ScanConfigPanel`）与「交易配置」（`TradingConfigPanel`）两个弹窗逐字段核对后端消费方，
**直接删除弹窗中已无实际作用的配置、收敛虚假取值区间、修正过期文案**。

约束：默认配置下不产生任何信号/质量分行为漂移；后端数据键保留兼容（避免旧 `scan_filters` JSON 迁移）。

## 2. 审计结论

| 配置项 | 位置 | 现状 | 结论 | 处置 |
|---|---|---|---|---|
| `breakout_vol_ratio` | 策略弹窗 | 标签「突破量比门槛」暗示硬门；后端仅用于触发描述文案与 AI 提示词，质量分放量加分写死 `1.2`（`technical.py:1467`），不参与任何判定 | **实际不生效** | **直接删除弹窗入口**（后端键保留兼容） |
| `kline_interval` | 策略弹窗 | 下拉 1m/5m/15m/30m/1h/2h/4h/1d 八档；扫描为整点小时级调度，非 1h 会使待确认窗口、腿门回看、retest 时效与触发语义错乱 | **7 档多余且危险** | **仅保留 1h** |
| `kline_window` | 策略弹窗 | 输入区间 30–1500；扫描取数 `limit=max(kline_window, 300)`（`scanner.py:845`），30–299 无实际作用 | **无效区间** | **最小值 30 → 300** |
| `pullback_touch_refs` tooltip | 策略弹窗 | 仍写「默认仅三条均线、ema144 已退役、EMA21 结构门不过则回踩不成立」；实际默认六触点全开、EMA144 已复出、结构门 2026-10-15 起仅扣质量分 15 | **文案过期** | **重写 tooltip** |
| `pullback_leg_*` 4 键 | 策略弹窗 | 已上线生效但前端无入口 | 反向缺口，非无用项 | 本次不动（避免扩大范围） |
| `risk_pct` / `risk_fixed_amount` | 交易弹窗 | 分别只在 `pct` / `fixed` 模式下生效 | 条件性无效，非死配置 | 不删 |
| `tp1_qty_pct_with_tp2` / `tp2_qty_pct` | 交易弹窗 | `tp2_enabled=false` 时无消费方 | 条件性无效 | 不删 |
| `daily_loss_stop_pct` | 交易弹窗 | `daily_loss_stop_enabled=false` 时不生效 | 条件性无效 | 不删 |
| `btc_crash_pause_pct` | 交易弹窗 | 仅向 AI 提示词注入 BTC 急跌警示，不拦截开单；tooltip 已如实标注 | 有消费方、文案已准确 | 不删 |
| `candle_max_ema21_dist_pct` / `structure_1h_gate` | 不在弹窗 | 后端兼容键，已无消费方 | 历史兼容 | 不处理 |

交易弹窗其余字段均确认有后端消费方，**不删除任何字段**。

## 3. 变更方案

### S1 删除 `breakout_vol_ratio` 弹窗入口

文件：`frontend/src/components/ScanConfigPanel.tsx`

- 从 `FILTER_FIELDS` 删除该条目（标签「突破量比门槛」与 tooltip 一并移除）。
- 保存逻辑按 `FILTER_FIELDS` 生成提交体，删除后不再提交该键；后端键级合并不受影响。
- 后端不改：`ScanFilters.breakout_vol_ratio` 保留，旧 DB JSON 仍可解析，默认 1.2，行为与现状一致。

### S2 `kline_interval` 仅保留 1h

文件：`frontend/src/components/ScanConfigPanel.tsx`

- `Select` 选项收敛为 `[{ value: '1h', label: '1小时' }]`。
- tooltip：「扫描按整点小时级调度，K线周期固定 1h（其他周期与待确认窗口、回踩触点时效口径不兼容）」。
- 后端字段与读取逻辑不动（`position_advisor` / `trade_engine` / `review_tasks` / `watchlist` 仍读该字段）。

### S3 `kline_window` 最小值 300

文件：`frontend/src/components/ScanConfigPanel.tsx`

- `InputNumber min={30}` → `min={300}`。
- tooltip 增加「后端扫描取数下限为 300 根，低于 300 按 300 取」。

### S4 重写 `pullback_touch_refs` tooltip

文件：`frontend/src/components/ScanConfigPanel.tsx`

新文案：
「回踩金K确认只认勾选的触点：触碰后当根/次根出现同向金K才触发。默认六触点全开（突破回测位/摆动结构位/EMA21/EMA21(4h)/EMA55/EMA144；EMA144 已复出并默认启用）。EMA21 的 1H 结构门（三线排列 + 快中线发散史 + EMA144 方向）自 2026-10-15 起不再否决，未过门仅质量分 -15；其余触点不受结构门约束。所有触点命中后还须通过回踩前置反向腿门（scan_filters 内配置）。全部不勾回落默认六触点」

### S5 文档同步

文件：`docs/前端与查询/08-前端界面.md` §4.1

- 更新「扫描基础」为：K线周期固定 1h / K线数量（300-1500）。
- 从筛选参数组列表中删除 `breakout_vol_ratio`。

## 4. 不在本次范围

- 交易弹窗字段删除或条件置灰：无死配置，全部保留。
- 回踩腿门 4 个键补前端入口：属于功能补充，不是「删除无用项」。
- 后端兼容键清理（`breakout_vol_ratio` / `candle_max_ema21_dist_pct` / `structure_1h_gate`）：保留。
- 任何信号判定逻辑不变。

## 5. 验收与验证

```bash
cd frontend && npm run build
```

手工验收：
1. 策略弹窗不再出现「突破量比门槛」；保存后 `scan_filters` 不新增该键。
2. K线周期下拉只有「1小时」；K线数量最小值 300，输入 300 可保存。
3. 回踩触点 tooltip 为新文案。
4. 交易弹窗字段与文案无变化。
5. 后端不产生行为变化：`breakout_vol_ratio` 仍按默认 1.2 参与描述文案/提示词。

## 6. 风险与回滚

- 线上 `scan_filters` 当前无 `breakout_vol_ratio` 键，删除入口零行为变化。
- 线上 `kline_interval=1h`、`kline_window=240`，S2/S3 不改变运行时取数（240 仍按 300 取）。
- 回滚：恢复 `ScanConfigPanel.tsx` 四个字段即可，无后端/DB 迁移。
