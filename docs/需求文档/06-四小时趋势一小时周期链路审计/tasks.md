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

- [x] **A3** 打开事件驱动（2026-10-21 直接打开；跳过 24h 观察前置——前置原为拍板建议，事件路径失败自动吞掉、巡检兜底保留，风险可控）
  - 文件：`backend/.env`（`EVENT_EXEC_ENABLED=true`）+ `.env.example` 同步文档位
  - 前置：确认 user-data stream 在目标账号稳定运行 ≥24h（已豁免：失败隔离 + 兜底机制完备）
  - 验收：`event_executor.react_to_fill` 与 `settle_trades` 不重复下单（幂等锁生效）——开仓锁双入口互斥 + 锁内重查，机制已由 test_event_executor 覆盖

## 批次 B — 观测（不改行为）

- [x] **B1** 三段时间戳（2026-10-21 完成）
  - `ScanResult` / `AIAnalysis` / `TradeRecord` 分别落 `signal_created_at`、`ai_finished_at`、`opened_at`，复盘输出 `signal_age_s`
  - 实现：`ScanResult.created_at` 是批量落库时间、`AIAnalysis.created_at` 原地更新不刷新，故新增两列——候选产生时打 `detected_at`（scanner / watchlist 两条入口）写入 `signal_created_at`；`_upsert_ai_analysis` 每次写入覆写 `ai_finished_at`；`TradeRecord.opened_at` 复用现有列。`/review/stats` 内嵌 `latency` 三段（`signal_age_s` / `ai_to_open_s` / `signal_to_open_s`），取近 7 天、P50/P90 线性插值、负值对剔除；前端「信号时效」卡片，`signal_age_s` 的 P90 > 300s 标红
  - 验证：`test_obs_batch_b.py::test_latency_three_segments`（正负值剔除、P50/P90 口径）
- [x] **B2** `htf_degraded` 标记（2026-10-21 完成；口径修正）
  - 文件：`backend/app/services/scanner.py` → `detect_indicator_candidate` 的 4h 降级分支
  - 落库：`ScanResult`（或 `funnel_json`），不影响放行
  - 实现：**口径修正**——方向职责拆分（`eb244c3`）后 4h 无方向已 fail-closed，「4h 降级仍放行」的路径不存在，故落点改为两处：① scanner 层四类数据缺失（拉取异常 / 空 K 线 / `htf_context` 异常 / `reject_reason ∈ data·adx`）标记 `reject_out["degraded"]`，汇总进 `funnel_json.htf_degraded` + `htf_degraded_symbols`（纯数据质量统计，不参与放行）；② AI 层 `htf_klines` 缺失仍可出 suggest，是真正存活的 fail-open 点，落 `AIAnalysis.htf_degraded`（缓存沿用随结论走）
  - 验证：`test_obs_batch_b.py` 的 4h 拉取失败 / data 拒绝 / tangle 拒绝（不标降级）/ AI 层缺失 四组用例
- [x] **B3** 复盘按 `htf_degraded` 分组（2026-10-21 完成）
  - 文件：`backend/app/services/trade_review.py`（或对应复盘服务）
  - 输出：两组胜率/期望/盈亏比
  - 实现：`review_tasks` 生成 `TradeReview` 时快照 `htf_degraded = AIAnalysis.htf_degraded`；`/review/stats` 输出 `htf_degraded_groups`（False / True 两组，各含样本、胜率、期望值 `ev_r`、盈亏比 `payoff`）。`payoff` = 平均盈利原始 R / 平均亏损原始 R，用扣费前 `r_mult`（`_agg_payoff`），与扣费后的 `ev_r` 口径区分；前端「4h 数据降级对比」表格
  - 验证：`test_obs_batch_b.py::test_stats_htf_degraded_groups_and_payoff`

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
- [ ] **DOC3** 在 `docs/总览与索引/12-交易系统总览.md` 补「执行节拍与信号时效」一节

---

## 批次 E — 提示词事实修复（不改判据，可直接上线）

> 对应 [spec.md §8.2](file:///Users/wangwei/Documents/github/trading-workbench/docs/需求文档/06-四小时趋势一小时周期链路审计/spec.md) P1/P2/P3/P6/P8。必须与批次 F 分开提交，避免与评分口径调整混在同一 `RULE_VERSION`。

- [ ] **E1** 量能口径修正（P1，确定性 Bug）
  - 文件：`backend/app/tasks/ai_tasks.py`（signal 增 `volume_ratio`）、`backend/app/services/ai_analyzer.py:628-630`、`backend/app/services/ai_agent.py:935-937`
  - 改法：渲染为 `量能: 缩量（量比 0.62×近20根中位量）`；原始成交量不得标为「×20根均量」，必要时单列 `原始量=...`
  - 验收：单测断言提示词中不再出现 `×20根均量` 后接原始量；`volume_ratio` 与 `indicators.volume_ratio` 同源

- [ ] **E2** 生效门槛注入（P2）
  - 文件：`backend/app/services/ai_analyzer.py:193-195`、`backend/app/services/ai_tasks.py`、`backend/app/services/trade_engine.py:1414-1420`
  - 改法：`_RULE_SCORE` 渲染 `effective_min_rec = max(min_rec, need_score)`，并说明「今日连损 N 笔，门槛提升至 X」；`analysis` 增「执行门槛」行
  - 验收：连损 2/3 笔的当日提示词门槛分别为 75/85

- [ ] **E3** 质量分注入（P3）
  - 文件：`backend/app/services/ai_analyzer.py`（P0 用户消息）、`backend/app/services/ai_agent.py`（P1 事实包）
  - 改法：注入 `quality_score` 与分项构成（base / 4h_state / vote / penalty）
  - 验收：P0/P1 均能在提示词中读到质量分；不改变 scanner 放行集合

- [ ] **E4** 指标块标题按触发类型（P6）
  - 文件：`backend/app/services/strategy/technical.py:1281`、调用方 `scanner.py` / `ai_analyzer.py` / `ai_agent.py`
  - 改法：`render_indicator_block(..., trigger_type=None)`；回踩渲染「方向=4h（1h 方向票仅作确认度）」
  - 验收：回踩提示词不再出现「方向=方向票多数决」

- [ ] **E5** 市场环境缺失显式标注（P8）
  - 文件：`backend/app/services/ai_analyzer.py`（`_RULE_BTC_ENV` / `build_market_context`）
  - 改法：缺数据渲染「BTC 环境：未知（数据缺失）」，禁止据此加分；`AIAnalysis` 落 `btc_env_missing`
  - 验收：数据缺失场景单测；复盘可按该标记分组

## 批次 F — 提示词结构（A/B + 影子 ≥30 笔）

- [ ] **F1** 评分档位按触发类型分表（P4，与 C5 绑定）
  - 文件：`backend/app/services/ai_analyzer.py:_RULE_SCORE`
  - 改法：突破 60/70/85 不变；回踩 60=4h同侧+触点+金K，70=+量能/结构位，85=回踩后收回并突破前高
  - 验收：回踩 rec 分布不再全部贴 60；与 C5 门槛分段共用一张表

- [ ] **F2** 4h 规则单一权威（P5）
  - 文件：`ai_analyzer.py` 提示词、`technical.py:enforce_htf_constraint`
  - 改法：`enforce_htf_constraint` 与 `scanner` 共用 `STATE_ALIGNED`；提示词删除不可达状态分支
  - 验收：全仓 `STATE_BLOCK_BOTH` 引用清零或仅作历史注释

- [ ] **F3** P0/P1 共享 facts builder（P7）
  - 文件：`ai_analyzer.py` / `ai_agent.py`
  - 改法：抽出 `build_signal_facts(...)`，市场环境、质量分、K 线列、止损止盈规则同源
  - 验收：同一 signal 下两管线事实文本 diff 仅剩「工具能力」段

- [ ] **F4** 提示词合并瘦身（P9）
  - 改法：合并为 5 段；目标 token 降 ≥30%
  - 验收：同一批历史信号离线重放，rec/skip 分布无系统性偏移

## 批次 G — 筛选排序 / 配额（先影子）

- [ ] **G1** AI 配额按触发类型分池（S2）
  - 文件：`backend/app/tasks/ai_tasks.py:156-177`、`backend/app/services/scan_filters.py`
  - 改法：新增 `ai_quota_breakout` / `ai_quota_pullback`，回踩独立排序
  - 验收：回踩进入 AI 的条数占比 ≥30%

- [ ] **G2** 4h 强度分入 `quality_score`（S1）
  - 文件：`backend/app/services/strategy/technical.py:compute_quality_score`
  - 改法：ADX 三档 + EMA144 斜率分位 + 乖离分位；只影响排序，不改放行
  - 验收：强弱多头分差可观测；配额命中率对比

- [ ] **G3** 可执行性筛选（S3）
  - 文件：`backend/app/services/scanner.py` 宇宙层/触发层、`scan_filters.py`
  - 改法：`max_spread_pct`、`min/max_atr_pct_1h`、复活 `candle_max_ema21_dist_pct`（仅突破）
  - 验收：`funnel_json` 新增 `spread` / `atr_band` / `overextended` 计数

- [ ] **G4** pending 与宇宙池解耦（S4）
  - 文件：`backend/app/services/scanner.py`
  - 改法：pending 中的币强制纳入本轮（跳过 liquidity/new_coin，保留 4h 门）
  - 验收：`pending_forced` 计数；pending 孤儿率下降

- [ ] **G5** 新币冷静期对齐 4h 数据需求（S5）
  - 改法：默认 14 → 34 天，或按已收盘 4h 根数 ≥200 判定
  - 验收：`data` 类拒绝占比下降

- [ ] **G6** 重复抑制键扩展（S6）
  - 文件：`backend/app/services/scanner.py:1274`
  - 改法：键加 `direction` + `price_bucket`；读最近 TradeRecord，`sl_hit` 后同向不抑制（-5 分）
  - 验收：止损后二次回踩的命中数可观测

- [ ] **G7** 排序次序键替换（S7）/ 配额相关性去重（S8）
  - 改法：同分次序键改 4h 强度分或 signal_age；配额按 4h 收益相关性聚类，每簇 ≤2
  - 验收：配额簇数、同簇占比统计
