# 整改任务清单（对应 spec.md）

> 状态：**待批准**。批准前不执行任何代码改动。
> 纪律：A 批次（执行层，不改判据）可直接做；B 批次只加观测；C/D 批次必须先影子验证。

## 批次 A — 执行层（不改开单判据）

- [x] **A1** 分析完成后链式触发执行（2026-10-21 完成）
  - 文件：`backend/app/tasks/ai_tasks.py` → `run_ai_analysis_task`
  - 改法：`group.apply_async()` 后不再直接关任务；改为在全部子任务完成后派发一次 `run_auto_trade_task.delay()`（`chord` 回调，或对每个子任务 `on_success` 汇总计数，全部结束再触发一次）
  - 实现：批量路径改 `chord(jobs)(run_auto_trade_task.si())`——全部子任务结束后派发一次结算巡检；force 手动单币路径保持裸 `apply_async()` 不链式
  - 验收：`ScanRecord.started_at → TradeRecord.opened_at` 的 P90 ≤ 5min；同一轮扫描只触发 1 次执行
  - 回归：手动单币重分析（`force=True`）路径不得触发执行（test_shadow_batch2 断言覆盖）

- [x] **A2** 巡检恢复为 30min（2026-10-21 完成）
  - 文件：`backend/app/celery_app.py`（`auto-trade` beat）
  - 改法：`crontab(minute="1,31")`，或配置化 `AUTO_TRADE_BEAT_MINUTES`
  - 实现：`config.py` 新增 `AUTO_TRADE_BEAT_MINUTES: str = "1,31"`（crontab minute 表达式，改回每小时设 `"1"`），beat 引用同一来源；test_screen_window 断言同源 + 触发点集合
  - 验收：TP1 成交 → SL 修改 P90 ≤ 5min（A3 未开时）

- [ ] **A3** 打开事件驱动
  - 文件：`backend/.env`（`EVENT_EXEC_ENABLED=true`）
  - 前置：确认 user-data stream 在目标账号稳定运行 ≥24h
  - 验收：`event_executor.react_to_fill` 与 `settle_trades` 不重复下单（幂等锁生效）

## 批次 B — 观测（不改行为）

- [ ] **B1** 三段时间戳
  - `ScanResult` / `AIAnalysis` / `TradeRecord` 分别落 `signal_created_at`、`ai_finished_at`、`opened_at`，复盘输出 `signal_age_s`
- [ ] **B2** `htf_degraded` 标记
  - 文件：`backend/app/services/scanner.py` → `detect_indicator_candidate` 的 4h 降级分支
  - 落库：`ScanResult`（或 `funnel_json`），不影响放行
- [ ] **B3** 复盘按 `htf_degraded` 分组
  - 文件：`backend/app/services/trade_review.py`（或对应复盘服务）
  - 输出：两组胜率/期望/盈亏比

## 批次 C — 判据（影子验证后上线）

- [ ] **C1** 4h 状态放行面：`macro_cross_*` / `*_recover` 影子
- [ ] **C2** `direction_flip` 按触发类型分级（仅回踩允许 1h-4h 背离）
- [ ] **C3** `range_score` 分档（1 项降仓 / ≥2 项拒）
- [ ] **C4** `quality_score` 硬地板（先只作用于 AI 配额）
- [ ] **C5** `rec` 门槛分段（突破 ≥70 / 回踩 ≥60）

每项统一验收：影子 ≥30 笔，期望 > 基线且最大回撤不劣化；样本按 `RULE_VERSION` 独立统计。

## 批次 D — 组合与 TP 结构（影子/小仓）

- [ ] **D1** `SAME_DIRECTION_CAP` 由 0 → 6（先影子打标）
- [ ] **D2** `PORTFOLIO_HEAT_CAP` 由 0 → 0.06（先影子）
- [ ] **D3** 新增「1R 减 30% + 移保本」可选结构，并行影子
- [ ] **D4** 时间止损（持仓 12 根 1h 未达 0.5R → `time_stop` 影子标记）

## 文档一致性

- [ ] **DOC1** 修正 `docs/需求文档/04-稳定盈利优化路线图.md` §4.1 的 TP 分批口径（50/25 → 代码实际 60/20/20 与 80）
- [ ] **DOC2** 修正同文档「同向上限 6」与代码默认 0 的漂移，注明「以线上 system_config 为准」
- [ ] **DOC3** 在 `docs/总览与索引/功能流程/12-交易系统总览.md` 补「执行节拍与信号时效」一节
