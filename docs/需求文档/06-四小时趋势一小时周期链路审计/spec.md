# 四小时趋势 → 一小时周期链路审计（筛选 / AI / 止盈止损）

- 状态：待评审（**未批准前不进入实施**）
- 范围：`scanner` → `ai_tasks/ai_analyzer` → `risk_guard` → `trade_engine`（开仓/持仓管理/结算）
- 方法：以代码为准 + 正式网实盘归因数据交叉验证；不引入新指标、不改判据，除非有影子回放证据
- 目标定义（用户口径）：**按 4h 趋势方向，做 1h 周期入场**的单子

---

## 0. 结论摘要（按对净值的影响排序）

| # | 判定 | 问题 | 证据 | 影响 |
|---|---|---|---|---|
| 1 | **错误（P0）** | 信号产出与执行节拍错位：扫描 `:00` → AI 分析 85–300s/币（并发 3–4，最多 `ai_max_per_scan` 币）→ 执行 `:01` 已跑完且不再回头 | `celery_app.py` L85；`ai_tasks.py` L198-202 仅 `group.apply_async()`；`trade_tasks.py` L231 仅 beat/手动触发 | 信号最晚等 1h 才可能下单；叠加「入场偏离 ≤1%」「RR 按现价复算」→ 大量单被打成 `blocked` 或改挂 pending。等效于**用 1h 收盘信号在下一根 1h K 线后才追** |
| 2 | **冲突（P0）** | 浮盈保护被降频：巡检 30min → 60min，且 `EVENT_EXEC_ENABLED=False`（`.env` 未覆盖） | `celery_app.py` auto-trade `minute=1`；`config.py` `EVENT_EXEC_ENABLED=False`；`event_executor.py` L154 注释即写明「默认关」 | TP1 成交→移保本 / HL 移动止损最长滞后 60min。而实盘最大单项漏损正是「曾浮盈→回撤全损」（-16.4R，浮盈回撤 -108.6U 占亏损 42%） |
| 3 | **冲突（P0）** | 4h 门收紧到仅 `aligned_*` 放行，与「1h 回踩入场」语义互斥 | `technical.py htf_context`：`block_both = st not in STATE_ALIGNED`；`direction_flip` 要求 1h 多数决与 4h 同向 | 4h 强趋势中的回踩，1h MACD/RSI/布林必然转空/转弱 → 被方向门否掉。已知 ema21_4h 84%、ema144 87% 死于此门；过闸样本亦为负期望（docs/05） |
| 4 | **错误（P0）** | 风险闸门 fail-open：4h 拉取失败/异常 → `htf=None` 全链降级放行；`range_score` 数据不足（n<200）返回 0 | `scanner.detect_indicator_candidate` 的降级分支；`ema_gate.range_score` | 系统唯一的方向依据在最需要时**静默失效**，且无 `degraded` 标记、无仓位折扣 |
| 5 | **冲突（P1）** | 两套评分互不相干：`quality_score` 只排序无硬地板；实际放行门槛是 AI `rec≥60` | `scanner.run()` 仅 `sort`；`config.TRADING_MIN_RECOMMENDATION=60`；实盘 rec 60–69 胜率 12.5%/avg -0.659R，70+ 41.4%/+0.324R | 资本配置由**已验证为负期望的分数段**决定；质量分的结构/量能/ADX/OBV 加减分对开单集合几乎无影响（配额绑定度 1.9%~5.8%） |
| 6 | **不足（P1）** | 组合级风控三件套默认关闭 | `PORTFOLIO_HEAT_CAP=0`、`SAME_DIRECTION_CAP=0`、`CONSECUTIVE_LOSS_DERATE=False` | 同向集中、总热度无约束；仅有当日 -9% 熔断（UTC 日、且只停开仓、不处理存量浮亏） |
| 7 | **不足（P1）** | TP 结构留有保护真空 | `TP1_MAX_ATR=2.0`、`TP1_MAX_R=0`、`TP1_QTY_PCT_NO_TP2=80`、TP1 后仅保本不移动 | TP1 中位 2.2R → 0–2.2R 段无任何锁定机制；`tp1_then_sl` 每笔泄漏约 0.7R |
| 8 | **不足（P2）** | pending 确认窗口依赖「每小时扫描不中断」 | `pending_confirm_window_bars=4`；`scan_tasks` 10min 防重 + 小时锁 + running 跳过 | 任一小时扫描失败/被跳过，pending 直接过期，回踩单整段丢失 |
| 9 | **冲突（P2）** | 文档与代码口径漂移 | 文档 §4.1 写 TP1 50%/TP2 25%；代码 `60/20` + 无 TP2 时 `80`；文档称同向上限 6，代码默认 0 | 复盘口径不可复现，RULE_VERSION 分组失真 |
| 10 | **不足（P2）** | P0/P1 双管线 | `ai_tasks` 仅 `force`（手动单币）才走 Agent | 生产自动链路永不使用 P1；两套提示词长期漂移 |

---

## 1. 筛选链（scanner + technical + ema_gate + trigger）

### 1.1 逻辑冲突（确定性）

**C1. 「4h aligned」与「1h 回踩」在判据上互斥。**
- 4h 侧：`STATE_ALIGNED = (aligned_bull, aligned_bear)`，其余 9 态一律 `block_both`。
- 1h 侧：`direction_flip` 要求 1h 方向票（MACD/布林/RSI 多数决 ≥2）与 4h 方向一致。
- 现实：一个健康的 4h 上涨趋势中，1h 回踩 EMA21/55/144 时，MACD 往往已死叉、RSI 跌破 50、价格贴近布林中轨下沿 → 1h 多数决转空 → 拒绝。
- 系统因此只允许「插针式瞬时回踩」通过（方向票来不及翻转），而这恰恰是最难挂单、最容易被 1% 偏离门打掉的形态。
- **这是设计层面的自相矛盾，不是参数问题。** 调 `DIR_VOTE_MIN` 或 `direction_conflict_gate_enabled` 只能改变「拒绝比例」，不能消除「通过者形态偏移」。

**C2. `range_score` 与回踩/突破两类触发均存在对抗。**
- 项 1「1h 三线交错」：回踩 EMA55 时 EMA21 下穿 EMA55 是常态 → 判震荡拒绝；而 `ema55` 触点自身又有「EMA21 在 EMA55 同侧」的排列门，两处判据近似重复但阈值不同，等于同一现象被罚两次、且罚则不一致。
- 项 4「波动过度扩张（Donchian/BBW 分位 ≥0.80）」：突破型触发（T1a/T1b）天然发生在扩张段 → 与突破语义对抗。
- 项 3「1h ADX<18」：与 T1b 趋势突破的「ADX≥25 才加分」方向一致，属可接受。

**C3. 筛选层与 AI 兜底层的 4h 口径不一致（同一套状态机两种放行标准）。**
- 筛选：`block_both = st not in STATE_ALIGNED`（仅 2 态放行）。
- AI 兜底：`enforce_htf_constraint` 仍用 `STATE_BLOCK_BOTH`（3 态禁）+ `STATE_LOCK_CONTRA`（锁逆势），即 `macro_cross_*`、`*_recover`、`transition` 全部放行。
- 结果：筛选在前已经干掉，AI 层判断实际只影响手动单币分析；一旦将来放松筛选门，两层会立刻打架。

### 1.2 错误

**E1. fail-open 方向门。**
- `htf_context` 在 4h 数据缺失/异常时返回可放行结果 → 全链失去 4h 约束。
- `range_score` 在 `n<200` 时返回 0（放行）。
- 新上币、数据源抖动、限频都会命中这条路径。系统没有任何「降级信号」标记，事后无法在复盘里区分「真通过」与「降级通过」。

**E2. 质量分不作为过滤条件。**
- `compute_quality_score` 输出 0–100，仅作为排序键；`ai_max_per_scan` 配额取 top-N 后才进入 AI。
- 当命中数 < 配额时（常态），**所有命中都进 AI**，质量分对开单集合零影响。

### 1.3 不足

**N1.** 筛选层没有「形态可用性」判据：不区分「可挂单的回踩」与「滑点不可接受的插针」，把问题全部推给执行层的 1% 偏离门。
**N2.** `range_score` 的 4 项合取（`RANGE_SCORE_REJECT=1`，任一命中即拒）过严，且无分档；无法表达「轻微震荡可降仓」。
**N3.** 无「信号年龄」字段。命中时刻、AI 完成时刻、下单时刻三者无显式约束。

---

## 2. AI 分析链（ai_tasks + ai_analyzer + risk_guard）

### 2.1 错误

**E3. 执行不链式（本报告第一优先级）。**
- `run_ai_analysis_task` 只 `group.apply_async()`（`ai_tasks.py` L198-202），无「分析完成后触发一次 `run_auto_trade_task`」。
- `auto-trade` 每小时 `:01` 跑一次；扫描 `:00` 起跑，10 币 × 3 并发 × 85–300s → 尾部落在 `:05`–`:15`，`:01` 的执行必然错过它们。
- 后果不是「少赚」，而是**入场模型被系统性扭曲**：
  - 到下一小时 `:01` 时，价格已走完 1 根 1h → 结构突破/回踩的最优价位已不存在；
  - 1% 偏离门把「已走远」的单子打成 `blocked`（属正确保护，但等于该信号作废）；
  - 未走远的改挂 `PENDING_ENTRY`（`PENDING_ENTRY_ENABLED=True`），TTL 60min，成交率与胜率与「即时入场」不是一个分布。
- 因此现有 74 笔样本的胜率/期望，**不能代表策略本身**，也不能用于评价筛选质量。

**E4. AI 层与筛选层 4h 约束口径不一致**（见 C3）。

### 2.2 不足

**N4. 单一放行门槛 rec≥60 与已证伪的分数段绑定。** 60–69 段 74 笔里胜率 12.5%、avg -0.659R。门槛应分方向/分触发类型/分状态设定，而不是全局单值。
**N5. 指纹缓存按 0.5% 价格桶复用结论，但未校验「4h 状态是否变化」。** 同一价格桶内若 4h 状态从 `aligned` 掉到 `turn`，仍会复用旧结论。
**N6. P1 Agent 管线在生产链路不生效**，提示词双份维护。
**N7. 超时重试 90s×2 放大了尾部延迟**，与 E3 叠加。

---

## 3. 止盈止损链（risk_guard + trade_engine + event_executor）

### 3.1 冲突

**C4. 保护机制与巡检频率冲突（第二优先级）。**
- 现状：`SL` 为 `STOP_MARKET/closePosition/MARK_PRICE`，`TP1/TP2` 为 `TAKE_PROFIT_MARKET/reduceOnly/CONTRACT_PRICE`（交易所侧，安全）。
- 但**移保本、HL 结构移动止损是程序侧动作**，只在 `settle_trades` 里执行 → 依赖巡检频率。
- 2026-10-20 把巡检从 30min 改回 60min，同时 `EVENT_EXEC_ENABLED=False`，等于把 P-09 的保护效果**回退**。
- 归因数据已明确：浮盈回撤占亏损 42%，65% 的亏损单经历过「曾浮盈→全损」。这条修复是**不改判据、只改执行时延**的纯工程改动，风险最低、期望最高。

**C5. TP 结构与「锁盈」目标冲突。**
- `TP1_MAX_ATR=2.0` 且 `TP1_MAX_R=0`（R 腿关闭）→ TP1 只受 ATR 约束；TP1 中位 2.2R。
- `tp2_enabled` 关闭时 TP1 出 80%，剩余 20% 只等保本/移动止损。
- 结果：**0 → TP1 之间没有任何减仓或收紧机制**，唯一的保护是初始 SL。价格走出 1R 后回落，全额回到 -1R。
- `tp1_then_sl` 每笔泄漏 0.7R 是这条结构的直接产物。

**C6. 组合风控缺失与「单笔 3% 风险」冲突。**
- `RISK_BUDGET_PCT=3.0`，`max_open_trades` 5 → 理论最大同时敞口 15% 权益，且可全部同向。
- `PORTFOLIO_HEAT_CAP=0`（关）、`SAME_DIRECTION_CAP=0`（关）→ 这个 15% 不受任何组合约束。
- `DAILY_LOSS_STOP_PCT=9.0` 按 UTC 日、按账号，且只停新开仓，不处置存量浮亏。

### 3.2 不足

**N8. 无「时间止损」。** 1h 周期的单子若 N 根未走出结构，仍占用仓位与 `max_open_trades` 名额，间接压缩新机会。
**N9. 移动止损只有 HL 结构一种口径**（`TRAIL_MODE_HL` 新开单默认），无 ATR 分档备选；`ATR_TRAIL_K=3.0` 存在但未与 HL 组合使用。
**N10. 成本未进入 R 口径。** 移保本时的往返费缓冲是有的（`_breakeven_stop_price`），但 `risk_amount`、RR 校验、TP1 上限均按毛价计算，滑点/手续费在复盘里是单独归因，不在开单决策里。

---

## 4. 整改方案（分批，全部「先影子、后生效」）

> 纪律：本项目已有 `shadow` 打标与 `veto_original` 机制，**所有判据类改动必须先用影子模式跑满 2 周或 ≥30 笔再决定是否生效**；执行层改动（不改开单集合）可直接生效。

### 批次 A：执行层（不改任何判据，可直接做）

| ID | 改动 | 位置 | 说明 |
|---|---|---|---|
| A1 | 分析分发完成后触发一次 `run_auto_trade_task` | `ai_tasks.run_ai_analysis_task` 末尾（`group.apply_async()` 之后，用 `chord` 回调或独立 `apply_async(countdown=…)`） | 解决 E3；不改判据 |
| A2 | 巡检恢复 30min（或打开 `EVENT_EXEC_ENABLED`） | `celery_app` auto-trade beat；`.env` | 解决 C4；二者取其一即可，同时开更好 |
| A3 | 打开 `EVENT_EXEC_ENABLED=true` 前先确认 user-data stream 稳定性 | `event_executor.react_to_fill` | 该路径与巡检互斥已实现（L93） |

### 批次 B：闸门可测量性（不改行为，只加观测）

| ID | 改动 | 位置 |
|---|---|---|
| B1 | 信号落 `signal_age_s`（命中→AI 完成→下单）三段时间戳 | `scanner`、`AIAnalysis`、`TradeRecord` |
| B2 | 4h 数据降级时落 `htf_degraded=true` 标记（仍放行，但可统计） | `scanner.detect_indicator_candidate` |
| B3 | 复盘按 `htf_degraded` 分组出期望值 | 复盘任务 |

### 批次 C：判据类（必须影子验证）

| ID | 假设 | 影子口径 | 上线门槛 |
|---|---|---|---|
| C1 | 放开 `macro_cross_*` / `*_recover` 可增加有效样本 | `veto_original=htf_block` 影子单 | 影子 30 笔期望 > 0 且回撤不劣于基线 |
| C2 | `direction_flip` 改为「按触发类型分级」：突破需同向，回踩允许 1h 与 4h 背离 | 影子放行回踩背离单 | 同上 |
| C3 | `range_score` 改分档（1 项=降仓，≥2 项=拒） | 影子标 `range_score=1` | 同上 |
| C4 | 质量分设硬地板（如 ≥55 才进 AI） | 只影响配额，先 shadow | 配额命中率 & 期望对比 |
| C5 | `rec` 门槛分段（突破 ≥70，回踩 ≥60） | 复盘分层 | 分层样本 ≥20 笔/段 |

### 批次 D：组合与 TP 结构（影子/小仓）

| ID | 改动 |
|---|---|
| D1 | `SAME_DIRECTION_CAP=6`（文档口径）先影子，观察同向簇亏损 |
| D2 | `PORTFOLIO_HEAT_CAP=0.06` 影子 |
| D3 | TP1 上限与减仓比：新增「1R 处减 30% + 移保本」作为**可选结构**，与现结构并行影子 |
| D4 | 时间止损：持仓 N 根（如 12 根 1h）未达 0.5R 则走 `time_stop` 影子标记 |

---

## 5. 验证方法（可执行）

1. **执行时延**：`ScanRecord.started_at` → `AIAnalysis.created_at` → `TradeRecord.opened_at`，取 P50/P90；目标 P90 ≤ 5min（A1+A2 后）。
2. **保护时延**：`tp1` 成交回报时间 → SL 修改时间（`task_log` / 交易所订单历史），目标 P90 ≤ 2min。
3. **fail-open 频率**：按天统计 `htf_degraded` 占比；>1% 需排查数据源。
4. **样本可比性**：A 批次上线后，用 `RULE_VERSION` 切分，**不得跨版本合并统计**。
5. **判据改动**：一律以影子单（不进真实资金）计算期望、最大回撤、盈亏比；影子样本 <30 笔不下结论。

---

## 6. 风险

- **A1/A2 会提高开单频率**，在判据未改的前提下可能放大负期望样本。缓解：A 批次上线同时把 `DAILY_LOSS_STOP` 与同向上限打开（先影子）。
- **C2 放开逆势回踩**是最危险的一项：1h 与 4h 背离时入场，本质是逆 4h 趋势。必须严格限制为「回踩触点 + 金K确认」且仓位打折。
- **QPS/限频**：A1 会让 AI 分析与下单在分钟级内接连发生，需确认交易所下单接口与 LLM 并发不互相拖累。

## 7. 明确不做

- 不新增技术指标、不改 `detect_trigger` 的形态判定（无样本支撑）。
- 不在未做影子验证前调整任何阈值。
- 不合并跨 `RULE_VERSION` 的统计。
