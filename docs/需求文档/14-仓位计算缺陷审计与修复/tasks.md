# 仓位计算缺陷修复清单

> 对应 [spec.md](spec.md) 审计结论。T1/T2/T3（F1-F3，P1）已获授权实施（2026-10-27），其余项仍为候选 backlog，未经确认不执行。
> 项目约定：先补测试、仅本地提交（conventional commits）、不推送、不部署、不连服务器。

## P1（已实施）

### T1（F1）成本感知计价 —— 已完成

- 文件：`backend/app/services/trade_engine.py`
- 实施：新增 `_cost_adjusted_stop_pct(ref, sl, fee_rate) = (|ref−sl| + fee_rate×(ref+sl)) / ref`；`_attempt_open` 用其计算 `notional = risk_budget / stop_pct_eff`；强平闸门入参改为 `stop_pct_eff + max_slippage_pct`（`lever_stop_pct`，粗筛/精确档两处）；`risk_amount` 语义不变（仍为价格风险，heat/R 口径不变）。
- 测试：`backend/tests/test_open_gates_v2.py` 新增 `test_cost_adjusted_stop_pct_includes_round_trip_fee`、`test_pending_sizing_uses_cost_adjusted_distance`；既有保证金边界用例固定风险 40→41.98 保持原断言。
- 验收：含费止损亏损 ≈ 预算；多空费用基数差异 ≤1e-5；滑点余量使边界杠杆 20×→19×。

### T2（F2）风险基数封顶本金 —— 已完成

- 文件：`backend/app/services/trade_engine.py` `_capital_base`
- 实施：`return float(min(base, wallet))`；`wallet≥100` 行为不变。
- 测试：`test_capital_base_never_exceeds_wallet`、`test_daily_loss_stop.py::test_loss_cap_scales_with_wallet`（65→5.85）。
- 文档：`docs/风控/05-风控体系.md`、`docs/交易执行/04-交易执行与多账号.md`、`docs/总览与索引/12-交易系统总览.md`、`docs/交易系统/02-规则与AI混合的合约趋势突破系统.md` 已同步。

### T3（F3）挂单纳入组合闸门 —— 已完成

- 文件：`backend/app/services/trade_engine.py`
- 实施：`try_open_for_analysis` 的 heat/同向统计追加同账号活跃挂单 `risk_amount`（排除本分析自身）；新增 `_pending_gate_breaches`，`materialize_pending_entry` 落地时复核热度/同向/当日熔断，越线记 `GATE_BREACH` 事件 + warning，保护单照挂（复核异常降级跳过，不阻断保护单）。
- 复核口径复用开仓侧 `_effective_caps` / `_week_derate_stats`（周减速时同向上限同步减半）；只读复核包 `db.begin_nested()` SAVEPOINT，查询级错误不会污染事务、不会间接触发外层紧急平仓。
- 测试：`test_heat_cap_counts_active_pending`、`test_direction_cap_counts_active_pending`、`test_pending_materialize_records_gate_breach`、`test_pending_materialize_within_caps_writes_no_breach`、`test_pending_gate_review_failure_does_not_block_protection`、`test_pending_gate_review_applies_week_derate`。

## P2

### T4（F4）统一风险比例来源

- 文件：`risk_budget.py` L36、`risk_guard.py` L549、`ai_analyzer.py` L273/L476、`config.py` L429
- 变更：统一用 `settings.TRADING_RISK_PCT`，删除 `RISK_BUDGET_PCT` 并清理引用。
- 验收：`grep -rn RISK_BUDGET_PCT backend docs` 零命中（归档除外）；`test_risk_budget.py` 更新。

### T5（F5）过滤器按单型取值

- 文件：`trade_engine.py` L730-L760、L1027-L1035、L1277-L1285
- 变更：市价用 `market_min_qty/market_max_qty`，限价用 `min_qty/max_qty`。
- 验收：限价不被市价上限缩仓；市价低于 `market_min_qty` 被拦。

## P3

### T6（F6）接管单取整落库

- 文件：`trade_engine.py` `adopt_position` L3622 / L3681-L3682
- 变更：`qty_tp1/qty_tp2` 先 `round_qty`，挂单与 raw 共用。
- 验收：raw 数量等于交易所挂单数量；`remaining_qty` 一致。

### T7（F7）R 口径文档

- 文件：`docs/复盘归因/` 实际复盘文档
- 变更：标注 R 分母为价格风险（不含费）、PnL 为含费净额。
- 验收：口径说明可检索；不改 `actual_review` 算法。

## 验证与提交

- 已验证：`pytest backend/tests -q` → **1171 passed**（2026-10-27）；前端无改动。
- 仅提交本次相关文件；不推送、不部署、不连服务器。
