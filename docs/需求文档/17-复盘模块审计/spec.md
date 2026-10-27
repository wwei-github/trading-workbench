# 复盘模块审计（2026-10-28）

> 对象：前端「复盘」Tab 及其四条后台链路（建议回放 / AI 错误归因 / 规则补丁 / 影子评估）。
> 口径：正式网优先；数据窗口除标注外为 **近 14 天（2026-10-13 ~ 10-28 00:00 UTC）**。
> 数据来源：线上库 `trade_reviews` / `trade_records` / `ai_analyses` / `prompt_patches` / `task_records` / `app_logs`，配套代码通读（行号为 2026-10-28 提交口径）。
> 相关：[16-筛选链路审计与优化](../16-筛选链路审计与优化/spec.md)（D1/D4/D6 已上线，D2/D5/D7/D8 挂起）、[15-实盘止损归因与上线包](../15-实盘止损归因与上线包/spec.md)。

---

## 0. 结论摘要

**复盘模块的「统计」部分是健康的，「闭环」部分断了两处，输入的维度比筛选层落后一代。**

1. **数据健康**：961 条回放、14 天 257 笔已平仓**全部完成 AI 归因（100%）**、近 7 天建议覆盖率 627/654 = **96%**。任务调度正常。
2. **规则补丁闭环从未跑通过**：`prompt_patches` **0 行**；14 次触发全部「热度未达标（门槛 0.5）」，实测最高标签热度只有 **0.40**。前端「规则补丁」子页永远空态。
3. **回放行缺入场诊断**：`TradeReview` 没有 `pos / room_r / stop_atr / planned_rr / quality_score`。而筛选侧最近全部结论（`pos≥0.6` 22 笔 +18.42R、1–1.5ATR 唯一正档）都只在**实盘侧**成立——回放侧无法复验，两套口径永远对不上。
4. **"没结论"被记成 `expired` 并从胜率分母剔除**：961 条里 284 条撞满 24 根窗口、160 条 expired（16.6%），而实盘最长持仓 35.7h → 约 5% 的单在窗口外，不确定性被当成好消息。
5. **维度落后**：金K形态保留 12 种（现役 8 种）→ 4 行恒为 0；信号族 9 个里 6 个已无法产生新行；「排列」桶把多空合并，失去判别力。

**因此这批工作分两块**：**P0 修闭环与口径**（补丁门槛、窗口/expired 语义、回放落 `entry_diag`），**P1/P2 补维度与性能**（形态对齐、mae/mfe、时段维度、GET 去副作用）。

---

## 1. 模块地图

### 1.1 前端入口

`frontend/src/pages/ScanResult.tsx:176-214`——扫描结果页的「复盘」Tab 内嵌 5 个子页：

| 子页 | 组件 | 数据源 |
|---|---|---|
| 实盘归因（默认） | `ReviewActualPanel` | `GET /api/scans/review/actual` |
| AI建议质量 | `ReviewStatsPanel` | `GET /api/scans/review/stats`、`/review/funnel` |
| 规则补丁 | `ReviewPatchesPanel` | `GET /api/scans/review/patches`、`POST .../trigger` |
| 币种黑名单 | `BlacklistPanel` | `GET /api/scans/blacklist` |
| 影子实验 | `ShadowPanel` | `GET /api/shadow/report` |

另有两处「复盘」语义分散在别的 Tab：交易记录页的**K线复盘弹窗**（`TradesPanel`，`tradeId` 模式）、待确认池页签内的**命中历史**（`HitHistoryPanel`，`PendingPoolPanel.tsx:239`）。

### 1.2 四条后台链路

| 任务 | 调度 | 实现 | 现状（14 天） |
|---|---|---|---|
| 建议复盘 | 每小时 :00 | `review_tasks.run_trade_review_task` | 307 次运行、961 条回放行 |
| AI 错误归因 | 每小时 :14 | `review_tasks.run_ai_attribution_task` | 218 次运行、257 笔全部归因 |
| 补丁触发 / 评估 | 每日 11:40 / 11:45（北京） | `review_tasks.run_patch_trigger_task` / `run_patch_evaluate_task` | 各 14 次，**0 条草稿** |
| 影子评估 / 再入场回填 | 08:00 / 08:25 | `shadow_tasks.*` | 每日标记 ≈9,983 条、回填 8 行 |

### 1.3 回放口径（先讲清它到底算了什么）

`backend/app/tasks/review_tasks.py`：

- 常量：`OBSERVATION_HOURS = 24`（:28）、`REVIEW_BATCH_SIZE = 20`（:29）、`REVIEW_LOOKBACK_DAYS = 7`（:30）
- 对象：`trade_decision='suggest'` 的 AI 建议 + 带 `veto_original` 快照的否决单（后者用于验证双评委否决是否有正贡献）
- 窗口：`_review_window(signal_time)` = 信号完成后固定 24h；只回放**完全落在窗口内**的已收盘 1h K
- 裁决：`replay_suggestion()`（:112）——入场价落在某根 OHLC 内才算成交；入场根触止损按保守记亏；之后止损优先于同根止盈；先触 TP1 再触止损仍计 `win_tp1`
- 四态：`win_tp1 / win_tp2 / loss / expired`；跑满窗口仍未触发由 `open` 改写为 `expired`（:306-307）
- 数据不完整（`_has_complete_window` 不过，:298）→ 保留待补偿、不计结论

**关键后果**：24h 是硬窗口，持仓更久的单得不到结论；且"没走出结果"与"数据缺失"最终都汇入 `expired`，而 `win_rate_note`（`api/scan.py:1251`）把 expired 排除出分母。

---

## 2. 过时的（4 条）

| # | 问题 | 证据 | 位置 |
|---|---|---|---|
| **O-1** | **规则补丁闭环从未跑通** | `prompt_patches` **0 行**；14 次触发全部「热度未达标（门槛 0.5）：追高追空 **0.40**、区间中段开单 0.27、止损过宽 0.21、近锚被扫 0.20、浮盈回撤 0.16、执行偏差 0.13、止损过窄 0.04」；补丁评估 14 次「评估 **0** 条补丁」 | 门槛 `HEAT_THRESHOLD = 0.5`（`backend/app/services/patch_service.py:49`）、heat=标签笔数÷已归因笔数（:105）、`MIN_SAMPLE = 8`（:50） |
| **O-2** | 金K形态维度保留 12 种，现役白名单 8 种 | `GOLDEN_PATTERN_ORDER` 含 `bullish_harami / dragonfly_doji / gravestone_doji / bearish_harami`，这 4 行在实盘归因表里**恒为 0 笔** | `backend/app/services/actual_review.py:33-40` |
| **O-3** | 信号族 9 个里 6 个已无法产生新行 | `TRIGGER_FAMILY` 含 `trigger_candlestick`（注释「纯金K下线，仅历史行」）、`uptrend_pullback`、`downtrend_breakout`、`trigger_trend_pullback`；加上 A1 已下线的 `trigger_breakout` / `trigger_trend_breakout` | `actual_review.py:101-111` |
| **O-4** | 文档与注释滞后 | `docs/复盘归因/06-复盘归因.md` 头部写「现行版本 RULE_VERSION **v2026-10-18**」，实际 14 天内 **17 个版本**（最新 `v2026-10-27`）；`ReviewStatsPanel.tsx:26` 注释仍写「24h 后逐K回放」，实际已含 veto 单与 4h/触点/版本维度 | 文档 + `frontend/src/components/ReviewStatsPanel.tsx:26` |

**O-1 的量化**：heat 是「该标签出现在多少笔已归因单上」。近 7 天已归因约 128 笔，即使某错误标签是系统性缺陷、覆盖 40% 的单，也只有 0.40 < 0.5。**门槛与样本量不匹配，不是"数据还不够"的问题**——即使积累到 1000 笔归因，只要单标签覆盖率不到一半，它依旧触发不了。

---

## 3. 缺失的（6 条）

### M-1 回放行不落入场诊断（结构性缺口，优先级最高）

`TradeReview`（`backend/app/models/scan.py:226-266`）现有维度：`signal_type / touch_ref / position / ema_state / htf_phase / htf_degraded / recommendation / is_veto / ab_bucket`；**没有** `pos / room_r / stop_atr / planned_rr / quality_score`。

后果：`/review/stats` 只能按"老的"维度分组。筛选侧这几天得出的结论（`pos≥0.6` 22 笔 63.6% +18.42R；1–1.5ATR 18 笔 +9.90R；`room<1R` 26 笔 −4.92R）全部来自**实盘成交**，回放侧一条都无法复验；而 **D2/D5/D7/D8 四个挂起项的恢复判据都需要回放侧也有这几个维度**。

### M-2 24h 硬窗口 + `open→expired` 的合并语义

- 961 条中 **284 条 `bars_to_exit ≥ 24`**（撞满窗口）、**160 条 expired（16.6%）**
- 实盘持仓：均值 8.1h、中位 6.0h、**最长 35.7h** → 约 5% 的单在窗口外
- `open` 与"数据完整但没结论"被合并改写为 `expired`（`review_tasks.py:306-307`），而 expired 被排除出胜率分母 → **胜率被系统性抬高**

### M-3 没有「错过的好单」

漏斗（`/review/funnel`）只给逐级计数（suggest 未开单 579、未上架 82、满仓 10、余额 5…），没有逐笔假想回放。veto 单虽已纳入回放，但 `is_veto` 全库**只有 1 条**——双评委几乎从不否决，这个对照组等于不存在。

### M-4 没有「同一笔的计划 vs 实际」逐笔对照

「实盘归因」（`/review/actual`）与「AI建议质量」（`/review/stats`）是两个互不引用的子页。同一笔单的"AI 计划三价 vs 实际成交价/出场价"没有并排视图；滑点、出场差异只以交易记录列的形式存在。

### M-5 没有 MAE / MFE

只记 `bars_to_exit`，不记持仓期间最大不利/最大有利偏移。于是"止损该紧还是松""止盈该 1.5R 还是 3R"这类问题，只能在模块外用 K 线重算（D3 那轮就是手算了两遍 MFE 才定论）。

### M-6 没有导出

复盘结论只能屏幕看。本轮全部归因都是登服务器写 SQL 重算的，没有任何离线核对/分享通道。

---

## 4. 需要优化的（5 条）

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| **A-1** | `GET /review/actual` 带写副作用 | 每次请求都跑 `backfill_costs(db, bt, limit=20)`（`api/scan.py:1621`），两网两次；实测 `closed_no_fee = 0` 即常态空跑，一旦积压会卡首屏 | 挪进定时任务（`run_ai_attribution_task` 里已有同一调用） |
| **A-2** | `/review/stats` 全量载入 + 内存聚合 | 窗口内所有 `TradeReview` 拉到 Python，再算 EV/置信区间/6 个维度分组；`days` 上限 180（`api/scan.py:1130`） | SQL 聚合或按期缓存；长期数据量下会线性变慢 |
| **A-3** | 「排列」桶合并多空 | `_HTF_PHASE_LABELS`（`actual_review.py:191-198`）把 `aligned_bull` / `aligned_bear` 都映射为「排列」 | 拆成「排列·多」「排列·空」——多头 ema21 44 笔 −16.67R 与空头 +8.17R 的差异正被这个合并抹掉 |
| **A-4** | 版本分组碎片化 | 14 天 17 个 `rule_version`，最大组 601 条、最小组 4 条 | 回放统计默认按「近 N 天 + 触点 + 4h 状态」看，版本分组降为审计视图 |
| **A-5** | 回放统计缺时段维度 | 实盘归因有「开仓时段（北京 4h 桶）」，回放没有 | 补同口径时段分组——10-26 的坏日子正是 07:00Z / 13:00Z 两批同向单 |

---

## 5. 方案与优先级

| 级别 | 编号 | 动作 | 处理什么问题 | 成本 |
|---|---|---|---|---|
| **P0-1** | 对应 **M-1** | `TradeReview` 加 1 个 JSON 列 `entry_diag`（`pos/room_r/stop_atr/planned_rr/quality_score`），回放时从 `ScanResult` + `AIAnalysis` 取；`/review/stats` 增加对应分档分组 | 回放侧无法复验筛选结论；D2/D5/D7/D8 判据缺数据 | 1 列 + 写入 + 分组，约 60 行 |
| **P0-2** | 对应 **M-2** | ① `OBSERVATION_HOURS` 改可配置（默认 24、可设 48）；② 新增 `censored` 语义与 `expired` 分开；③ `win_rate_note` 明确分母 | 撞窗口的单被当"没结论"且排除出分母，胜率被抬高 | 约 40 行 + 1 次数据回填说明 |
| **P0-3** | 对应 **O-1** | 补丁门槛改双阈值：`heat ≥ 0.30` **或** `该标签绝对笔数 ≥ 20`（取"或"），保留 `MIN_SAMPLE=8` 与冷却 | 门槛与样本量不匹配，闭环从未触发 | 约 15 行 + 文档 |
| **P1-1** | 对应 **O-2** | `GOLDEN_PATTERN_ORDER` 由 12 收到现役 8（历史行按 `pattern` 兜底进「未记录」） | 4 行恒为 0，表格噪音 | 2 行 |
| **P1-2** | 对应 **O-3** | `TRIGGER_FAMILY` 的已下线键标 `legacy` 并在前端折叠 | 9 个族里 6 个不再产生新行 | 小改 |
| **P1-3** | 对应 **M-5** | 回放时同时算 MFE/MAE 落两列 | "止损/止盈该多宽"只能手算 | 约 30 行 |
| **P2-1** | 对应 **A-1** | `backfill_costs` 从 GET 移出 | 首屏写副作用 | 10 行 |
| **P2-2** | 对应 **A-3/A-5** | 排列桶拆多空、补时段维度 | 维度判别力 | 30 行 |
| **P2-3** | 对应 **M-6** | `/review/*` 加 CSV 导出 | 无离线核对通道 | 50 行 |

**为什么 P0-3 在 P0**：补丁闭环是"复盘 → 规则演进"的唯一自动化出口，空转会让人误以为"AI 在自我改进"，实际所有规则变更都是人工改代码。要么修门槛让它真能跑，要么在 UI 上明确标"本次未触发（门槛未达）"并给出最近 30 天最高热度——**不能让空页面看起来像"一切正常"**。

---

## 6. 复现口径

```sql
-- 回放规模与四态
SELECT count(*) total, max(created_at) latest,
       count(*) FILTER (WHERE outcome='win_tp1') tp1,
       count(*) FILTER (WHERE outcome='win_tp2') tp2,
       count(*) FILTER (WHERE outcome='loss') loss,
       count(*) FILTER (WHERE outcome='expired') expired,
       count(*) FILTER (WHERE is_veto) veto
FROM trade_reviews;

-- 撞窗口比例（bars_to_exit ≥ 24 = 跑满 24 根仍未触发）
SELECT count(*) FROM trade_reviews WHERE bars_to_exit >= 24;

-- 实盘持仓时长 vs 24h 窗口
SELECT round(avg(EXTRACT(EPOCH FROM (closed_at-opened_at))/3600)::numeric,1) avg_h,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (closed_at-opened_at))/3600) p50_h,
       max(EXTRACT(EPOCH FROM (closed_at-opened_at))/3600) max_h
FROM trade_records WHERE status='CLOSED' AND closed_at >= now() - interval '14 days';

-- 归因覆盖
SELECT count(*) closed, count(*) FILTER (WHERE attributed_at IS NOT NULL) attributed
FROM trade_records WHERE status='CLOSED' AND closed_at >= now() - interval '14 days';

-- 补丁闭环是否产出过
SELECT count(*) FROM prompt_patches;

-- 版本碎片化
SELECT rule_version, count(*) FROM ai_analyses
WHERE created_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 2 DESC;
```

补丁触发的卡点直接看任务记录（无需查库）：

```sql
SELECT task_name, count(*), max(created_at), max(summary)
FROM task_records WHERE task_type='review' GROUP BY 1 ORDER BY 3 DESC;
```

---

## 7. 明确不做

| 项 | 理由 |
|---|---|
| 删掉「AI建议质量」子页 | 它回答的是"AI 建议本身好不好"，与实盘执行质量是两个问题；只是需要标清是**参考口径** |
| 回放改用 tick/秒级数据 | OHLC 保守裁决的误差方向已知（同根止损优先），换数据源的收益远小于成本 |
| 引入完整回测引擎 | 本模块定位是"事后归因"，不是"事前回测"；完整回测需要独立工程（P1-1 那轮已证明快照类回放不可靠） |
| 自动合入补丁 | 现有设计是"AI 出草稿 + 人工确认"，这条不能破 |

---

## 8. 待观察

- **D6 每日归因快照**（`docs/16 §21`，08:10 北京）已在跑：它按 `closed_at` 给当日结果分档，与 `/review/actual` 的 R 口径一致但维度更全（含 `pos/room_r/stop_atr`）。**P0-1 落地后，回放侧也应能对齐这几个维度**，届时 `docs/16 §22` 的四项挂起判据才有回放侧证据。
- **A1 之后复盘样本结构变了**：突破族不再产生新信号，近 7 天回放里 47+24 条突破类是存量。样本量下降本身不是问题，但要观察"回踩单样本/天"是否还能支撑 14 天窗口的分组统计（目前日均约 30 条）。
