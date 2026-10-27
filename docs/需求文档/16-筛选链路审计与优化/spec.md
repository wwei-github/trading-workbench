# 筛选链路审计与优化方案（2026-10-27）

> 对象：全市场 → 候选命中 → AI 分析 → 开单 的完整筛选链路。
> 口径：正式网（`testnet=false`），统计窗口 2026-10-13 ~ 10-27（14 天、346 轮扫描），除非另行标注。
> 数据来源：线上库 `trade_records` / `scan_results` / `scan_records.funnel_json` / `ai_analyses` / `pullback_pendings` / `shadow_marks` / `app_logs`，配套 1h K 线重算。
> 相关：[15-实盘止损归因与上线包](../15-实盘止损归因与上线包/spec.md)（A1/A2/A3/B1/A6/A7 均已上线）。

---

## 0. 结论摘要

**筛得不算松，但筛完之后有三处漏水。**

1. **命中率 1.8%**（3,330 命中 / 174,000 币次），4h 门砍掉 53%、无触发再砍 17%，筛选层的"准入"并不宽松。
2. **52% 的命中从未被 AI 看过**（近 7 天：候选 1,601 → 分析 835；67/167 轮被 `AI_MAX_PER_SCAN=10` 截断，累计 766 个候选饿死）。
3. **13% 的 LLM 预算花在必然开不了的单上**（suggest 1,015 中 133 条推荐分 <60 门槛），另有 **82 次**分析了未上架合约（占 8%）。
4. **质量分有明确分水岭（80），但没有设门槛**：已平仓按质量分 `<70` −0.056R/笔、`70–79` −0.050R/笔、`80–89` **+0.260R**、`90+` **+0.218R**。
5. **元问题：没有回放能力**，所有阈值类改动只能上线后拿实盘验证。`shadow_marks.htf_blocked` 已存 **33,637 条**被 4h 门拦下的候选快照，是现成的第一块拼图。

**方案分层**：P0 三项（配置/小改，7 天内可上）、P1 四项（先补观测再做决策）、P2 四项（必须先有回放）、明确不做五项。

---

## 1. 漏斗全景（14 天）

| 层 | 每轮 | 14 天累计 | 占进入该层 |
|---|---|---|---|
| 全市场 USDT 永续 | 518 | 174,000 | — |
| 24h 成交额 ≥ 300 万（`MIN_VOLUME_24H`） | −157 | −54,309 | 砍 30% |
| 黑名单 / 风险档位 / 新币冷静期 | −6 / −1 / −0.3 | −1,845 / −350 / −90 | 共 1.3% |
| **进指标管线** | **365** | **126,400** | — |
| 4h 背景门（`gate_4h` + `htf_state_lock`） | **−193** | −66,698 | **砍 53%** |
| 无触发（`no_trigger`） | −63 | −21,920 | 17% |
| 震荡否决 / 方向翻转 / 票不足 | −25 / −22 / −16 | −8,793 / −7,755 / −5,709 | 18% |
| **候选命中** | **9.6** | **3,330** | **占 universe 1.8%** |
| 送 AI 分析 | 4.8 | 1,666 | **占命中 52%** |
| AI suggest | 2.9 | 1,015（skip 651 / 39%） | 占分析 61% |
| 实际开单 | 0.76 | 263 | 占命中 7.9% |

### 1.1 4h 门的拒因（10-21 起落库，6.5 天）

| 拒因 | 次数 | 占比 |
|---|---|---|
| `tangle`（缠绕） | 13,991 | 64.6% |
| `flat`（走平） | 5,583 | 25.8% |
| `undetermined`（方向不定） | 1,882 | 8.7% |
| `degraded`（数据缺失） | 193 | 0.9% |

4h 状态样本 43,459 条中：`tangle` 17,396（40%）、`turn_bear` 7,131、`bear_rally` 2,839、`bull_dip` 1,691、`macro_cross_bear` 1,259、`turn_bull` 1,187、`macro_cross_bull` 1,106、`bear_recover` 424、`bull_recover` 232、**`aligned_bear` 189、`aligned_bull` 4**、`none` 179。

### 1.2 命中质量与后续结果

| 质量分 | 命中数 | 开单数 | 开单率 | 已平仓 | 平均收益 |
|---|---|---|---|---|---|
| <70（两档合并） | 562 | 46 | 8.2% | 46 笔 | **−0.056R** |
| 70–79 | 416 | 32 | 7.7% | 31 笔 | **−0.050R** |
| 80–89 | 617 | 40 | 6.5% | 40 笔 | **+0.260R** |
| 90+ | 1,735 | 145 | 8.4% | 145 笔 | **+0.218R** |

**注意**：质量分对"开单率"没有区分度（6.5%~9.5%），说明开不开由 AI/执行层决定；但质量分对"结果"有区分度，阈值在 80。这是**已开单样本上的关联**，存在选择偏差，需按 §5.1 的方式上线后验证。

### 1.3 触点的实测表现（14 天已平仓）

| touch_ref | 笔数 | 平均收益 |
|---|---|---|
| `retest` | 65 | **+0.323R** |
| `swing` | 87 | **+0.226R** |
| `ema21` | 33 | +0.184R |
| `ema144` | 6 | +0.076R |
| `ema55` | 12 | −0.115R |
| `ema21_4h` | 24 | **−0.150R** |
| 无触点（突破族） | 35 | −0.122R |

信号族：`trigger_pullback_confirm` 227 笔 **+0.186R**；`trigger_breakout` 19 笔 +0.066R；`trigger_trend_breakout` 16 笔 **−0.344R**。

---

## 2. 逐层问题清单

### L0 观测与验证能力（元问题）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L0-1 | **没有回放/反事实工具**，阈值改动只能实盘试 | 33,637 条 `htf_blocked` 快照躺着未评估 | 每个优化都是一次实盘实验 |
| L0-2 | 影子特征只采不判 | `cvd_div` 3,269 / `r2_slope` 3,269 / `macd_div` 3,185 / `sweep_reclaim` 1,342 / `oi_conf` 331 / `rec70_shadow` 614 / `be_retrace` 173，无任何评估闭环 | 存储成本 + 误以为"在观测" |
| L0-3 | ~~`is_repeat` 恒 False，抑制从未生效~~ → **更正：抑制器正常，只是当前无重复可抑**；真问题在日志不可见 | `is_repeat` 14 天 3,330 条全 False、日志"重复命中抑制" 0 次；但复核发现近 6 天同币同信号 3h 内重复 **0 条**，14 天内的 58 条重复**全部落在 10-21 之前**（突破类还开着、Donchian 每小时创新高时） | 抑制器按设计工作；0 次日志的真因是 **扫描 INFO 不落库**（见 §12.1） |
| L0-4 | ~~两套 4h 口径并存~~ → **更正：非缺陷，是不同的分母** | 两者都来自同一个 `analyze_htf_background` 状态机，但漏斗 `htf_states` 的分母是**被 4h 门拦下**的币次（以 `tangle`/`flat` 为主：`aligned_bear` 189 / `aligned_bull` 4），`ai_analyses.htf_phase` 的分母是**已出 suggest 的候选**（以排列为主：`aligned_bull` 99 / `aligned_bear` 152）。属幸存者差异 | 只看原始数字会误判为"算法打架"；已加代码注释与本章口径说明，禁止跨分母对比 |

### L1 准入门

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L1-1 | 3M 成交额门槛偏低 | 只砍 30%，剩 365 币/轮 | 池子偏脏，风险转嫁给执行层的深度/滑点兜底 |
| L1-2 | **自动拉黑几乎不生效** | 14 天 `symbol_blacklist` 仅新增 4 个 `auto` + 5 个 `stop_hard` | 连亏币（CTR/ERA/MORPHO/AIOT 各 2 笔全亏）仍在池里反复命中 |
| L1-3 | 筛选层不感知组合 | 无相关性/同向敞口约束，`beta` 有记录但未使用 | 同小时批量同向（10-26 03h 4 笔全亏） |

### L2 4h 背景门

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L2-1 | 门很强（设计如此） | 砍 53%，`tangle` 占 65% | 不是缺陷，是"大部分时间不动手" |
| L2-2 | **数据缺失会误杀 pending** | 7 天 `4h拉取失败` 50 次、`429` 87 次；失败当轮该币活跃 pending 被 `expire(ranging)` | 数据抖动 = 真实回踩被终止 |
| L2-3 | `htf_states.none` 179 次无定义 | 无文档、无注释 | 归因黑盒 |
| L2-4 | 4h 门拒绝样本无方向 | 快照里 `htf.direction` 多为 `null`（门在方向票之前就拒了） | 反事实回放必须双向评估（见 §6.1） |

### L3 方向票层

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L3-1 | 三票高度共线 | MACD/BOLL/RSI 都是同一价格序列派生，"≥2 票同向"≈1 个指标 | `votes` 拒 5,709、`direction_flip` 7,755（6%） |
| L3-2 | 量价/持仓/资金费率不参与方向 | 只在 AI 提示词里当文字 | 缺一类独立证据 |

### L4 触发层

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L4-1 | 触点业绩分层清楚（见 §1.3） | `retest`/`swing` 明显好于 `ema55`/`ema21_4h` | 可继续按触点收敛（A2 已禁多头 ema21） |
| L4-2 | **复合金K 路径几乎不可达** | 3,330 条命中中 `golden_combo=True` **1 条**、`entry_lag_pct` 非空 **1 条** | "双流星""前根锤+当根趋势K"两条规则写了 4 个月，等于未启用 |
| L4-3 | pending 确认率极低 | expired 5,458 / superseded 1,992 / confirmed 602 / 活跃 62 → **7.4%** | Stage-1 触点 92% 是噪声；每轮约 21 个 pending 长期占用评估 |
| L4-4 | 摆动 band 未按波动归一 | `STRUCTURE_TOUCH_BAND_PCT=0.3` 为绝对百分比 | 高波动币必入带（门虚设）、低波动币永不入带 |
| L4-5 | `no_trigger` 不可细分 | 21,920 次拒绝，无法区分"无触点/有触点无金K/腿门不过" | 改哪层都靠猜 |

### L5 震荡与质量层

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L5-1 | `range` 只扣分不否决 | 3 日窗口 11 笔合计 −6.79R（约 −0.62R/笔） | 已登记为 A5，需与 A4 配对，暂不动 |
| L5-2 | 质量分维度退化 | `htf` 恒 10、`volume` 有值样本恒 5 | 有效维度只剩 base/zone/stabilize |
| L5-3 | **质量分没有下限** | 见 §1.2 | 60/70 分的单照样进 AI、照样开 |

### L6 配额层

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L6-1 | **48% 命中未被分析** | 近 7 天 1,601 候选 → 835 分析；67/167 轮被截断；766 饿死 | 筛选的一半工作白做 |
| L6-2 | 候选极度尖峰 | >20 个候选的 40 轮（11%）贡献了 50% 的候选 | 固定配额在尖峰轮严重不足 |
| L6-3 | 分池配额只记录不生效 | 代码注释自认"回踩被结构性挤出"；`quota_pool/quota_rank` 只落库 | 回踩（质量分基数低）长期吃亏 |
| L6-4 | 13% 预算花在门槛外的单 | suggest 1,015 中 133 条 <60 | 直接浪费 |
| L6-5 | 单币反复烧配额 | MAGMA 14 次/10 skip（均分 34）、SOPH 17/9、Q 13/9、MUBARAK 10/9、XLM 16/8 | 同一币每天重复被拒 |
| L6-6 | 满仓/连损进一步压缩样本 | 满仓跳过 AI 10 次；连损 ≥2 配额减半 | 坏日子里看得更少，归因更难 |

### L7 执行层（筛选的直接后果）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L7-1 | AI 分析了不可交易合约 | `下单环境未上架该合约` **82 次**（14 天，占 suggest 8%） | C0 撤销（2026-10-11）的代价，白烧配额 |
| L7-2 | 同币已有持仓仍分析 | 74 次 | 重复分析 |
| L7-3 | **止损锚与止盈上限数学冲突** | skip 原因反复出现同一句式："止损须挂结构位外侧 → 止损 >1.33×ATR → TP 受 2×ATR 封顶 → RR 数学上不可能 ≥1.5"，每条回炉 2 次后放弃 | 不是个币问题，是 `1.33×ATR` / `2×ATR` / `1.5R` 参数组在宽结构币上的必然结果 |
| L7-4 | 579 条 suggest 从未尝试开单 | 双网关闭 + 手动模式 | 预期行为，但"AI 说可开"与"真的开"无闭环追踪 |

### L8 数据质量

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| L8-1 | 数据源偶发抖动 | 7 天 `429` 87 次、单币扫描失败 82 次、4h 拉取失败 50 次；10-21~23 曾单日 61/41/29 次错误 | fail-closed 静默丢币（0.6%），叠加 L2-2 误杀 pending |
| L8-2 | 扫描性能正常 | 平均 14.8s、最大 27.9s | 无需优化 |

---

## 3. 解决方案总表

| 编号 | 名称 | 层 | 处理什么问题 | 证据强度 | 成本 | 回退 |
|---|---|---|---|---|---|---|
| **P0-1** | 质量分下限 80 | L5-3/L6 | 低质量命中占用 AI 配额、进开单池 | B（n=185，单期） | 配置 + 30 行 | 置 0 |
| **P0-2** | AI 前剔除不可交易合约 | L7-1 | 白烧 8% LLM 配额 | A（82 次，机制清楚） | 20 行 | 删判断 |
| **P0-3** | 单币 AI 冷却 | L6-5 | 同币当日反复分析反复拒 | A（机制清楚） | 30 行 | 参数置 0 |
| **P1-1** | 4h 门反事实回放 | L0-1/L2 | 判断 `tangle` 门是否划算 | 素材 33,637 条 | 1 个离线脚本 | 只读 |
| **P1-2** | 配额按池分配 | L6-1/L6-3 | 回踩被结构性挤出、48% 饿死 | A（机制清楚） | 40 行 | 改回全局 limit |
| **P1-3** | `no_trigger` 细分漏斗 | L4-5 | 改哪层靠猜 | 机制清楚 | 25 行 | 删细分键 |
| ~~P1-4~~ | ~~统一两套 4h 口径~~ → 降级为文档项 | L0-4 | 复核后确认是分母不同而非算法冲突 | A（同源函数） | 注释 + 文档 | — |
| **P2-1** | 摆动 band 改 ATR 归一 | L4-4 | 高波动币门虚设、低波动币永不入带 | C（需回放定 k） | 20 行 | 改回 0.3 |
| **P2-2** | 复合金K 路径处置 | L4-2 | 1/3,330 的死路径 | A（计数确凿） | 删 60 行 或 排查 | 恢复 |
| **P2-3** | 止损锚 / TP 上限冲突 | L7-3 | 宽结构币反复回炉后必 skip | B（skip 文本，n≈20） | 参数或前置判定 | 改回 |
| **P2-4** | 黑名单阈值复核 | L1-2 | 14 天只自动加 4 个 | B（n=4） | 参数 | 改回 |

---

## 4. 明确不做

| 项 | 理由 |
|---|---|
| 放宽 4h `tangle` 门 | 砍 53% 是设计意图；要放必须先有 P1-1 的反事实证据 |
| 抬高 3M 成交额门槛 | 风险已由执行层（深度/滑点/`max_stop_pct`）承担；抬门槛会砍掉小币行情（10-25 是主线） |
| 新币 14 天冷静期 | NIGHT 案例（出场滑点 1.98%）定的，不动 |
| 每币每轮多候选 | 会放大同向批量问题 |
| `range` 改否决 | 需与 A4（关等待路径）配对，单独上无增量（已登记 A5） |

---

## 5. P0 详细方案

### 5.1 P0-1 质量分下限 80

**处理什么**：质量分是筛选层唯一的"预期质量"信号，但它只用于排序与配额，不做准入。`<80` 的命中与 `≥80` 的结果差异（−0.056R / −0.050R vs +0.260R / +0.218R）说明该分有分水岭，而 60/70 分的单照样消耗 AI 配额与开单额度。

**实现**
1. `backend/app/config.py`：`QUALITY_FLOOR: float = 0.0`（0=关闭，注释载本表证据）。
2. `backend/app/services/scan_filters.py`：`SCAN_FILTER_KEYS` 增 `quality_floor`；dataclass 字段 `quality_floor: float`；`resolve_scan_filters` 里 `quality_floor=_num("quality_floor", float(settings.QUALITY_FLOOR), minimum=0.0, maximum=110.0)`。
3. `backend/app/services/scanner.py`：`hits` 收集完、排序前过滤
   ```python
   if flt.quality_floor > 0:
       before = len(hits)
       dropped = [h for h in hits if (h.get("quality_score") or 0) < flt.quality_floor]
       hits = [h for h in hits if (h.get("quality_score") or 0) >= flt.quality_floor]
       for h in dropped:
           funnel_rejects.setdefault(h["symbol"], "quality_floor")
   ```
   放在 `hits.sort(...)` 之前、`_save_results` 之前；**不影响 pending 的 upsert**（pending 是触点事实，不是候选）。
4. `backend/app/api/scan.py`：`scan_filters_effective` 增加该键；`_validate_scan_filters` 加分支（0~110，0=关闭）。
5. 前端筛选配置页增加一个数字输入（"质量分下限（0=不过滤）"）。

**上线**：部署后默认 0（零行为变化）→ 页面设 80 或 `backend/.env` 加 `QUALITY_FLOOR=80`。

**判读（7 天）**
```sql
-- 被拦量
select sum(coalesce((funnel_json->'stages'->>'quality_floor')::int,0)) n
from scan_records where created_at > now()-interval '7 days';
-- 命中质量分分布（应左截断在 80）
select case when quality_score>=90 then '90+' when quality_score>=80 then '80-89' else '<80' end b, count(*)
from scan_results where created_at > now()-interval '7 days' group by 1 order by 1;
-- 开单结果（与上线前 14 天对比 avg(pnl_pct)）
select count(*) n, round(avg(pnl_pct)::numeric,2) avg_pct, round(100.0*count(*) filter (where pnl_pct>0)/count(*),1) win
from trade_records where testnet=false and opened_at > now()-interval '7 days' and pnl_pct is not null;
```

**停手/回退条件**：命中数掉到 <5 条/天（样本被饿死）→ 降到 75；7 天 avg(R) 未改善 → 置 0（说明是选择偏差而非真信号）。

**风险**：n=40 的单档 + 已开单样本有选择偏差；质量分维度本身退化（L5-2），门槛可能只是"顺手砍掉低分区"。

### 5.2 P0-2 AI 分析前剔除不可交易合约

**处理什么**：82 次/14 天（占 suggest 8%）的配额花在"下单环境未上架该合约"上。C0 预过滤在 2026-10-11 被撤销（换取全市场可见性），但代价是 LLM 预算。

**实现**
1. `backend/app/services/scanner.py`：复用已有的 `BinanceTrader(False).risk_tier_all()` 缓存（24h），额外取一次 `exchangeInfo` 的 symbol 集合，落进程级缓存（TTL 24h）。
2. 在 `run()` 送 AI 之前（`run_ai_analysis_task.delay` 之前）把命中里"两个下单环境都未上架"的币剔除，计数写进任务 summary 与 `funnel["skipped_unlisted"]`。
3. **只影响"是否送 AI"**，不影响落库与展示（用户仍能在命中历史看到）。

**判读**
```sql
select count(*) from ai_analyses a join trade_records t on t.ai_analysis_id=a.id
where a.created_at > now()-interval '7 days' and a.trade_decision='suggest'
  and t.open_block_reason like '%未上架%';  -- 应归零
```
**回退**：删掉该过滤调用（或 `AI_SKIP_UNLISTED=false`）。

### 5.3 P0-3 单币 AI 冷却

**处理什么**：MAGMAUSDT 14 天被分析 14 次、skip 10 次、均分 34；SOPH 17/9、Q 13/9、MUBARAK 10/9、XLM 16/8。同币当日反复分析反复拒，纯浪费。

**实现**（用 DB 判定，不引 Redis，天然持久）
1. `backend/app/tasks/ai_tasks.py`：配额排序后、`group(...)` 前，对每个候选查
   ```sql
   select count(*) filter (where trade_decision='skip'), coalesce(avg(recommendation),0)
   from ai_analyses
   where symbol=:sym and created_at >= date_trunc('day', now() at time zone 'utc')
   ```
2. 命中 `skip_count >= settings.AI_REANALYZE_SKIP_LIMIT(=2)` 且 `avg_rec < settings.AI_REANALYZE_MIN_REC(=50)` → 跳过该候选，计入任务 summary `冷却跳过 N 个`。
3. 手动单币重分析（`only_scan_result_id` 非空）**不受限**。

**判读**
```sql
-- 每轮送 AI 数不应显著下降（冷却只砍"已反复被拒"的）
select date_trunc('day',created_at)::date d, count(*) ai_rows from ai_analyses
where created_at > now()-interval '7 days' group by 1 order by 1;
```
**回退**：`AI_REANALYZE_SKIP_LIMIT=0`（=关闭）。

---

## 6. P1 详细方案

### 6.1 P1-1 4h 门反事实回放

**处理什么**：4h 门砍掉 53%，是全链路最大的过滤器，但从未验证"被拦下的那些候选后来怎么了"。这决定 P2 的所有阈值改动能否有依据。

**素材**：`shadow_marks`（`feature_key='htf_blocked'`）33,637 条，`value_json` 形如
```json
{"stage":"gate_4h","htf":{"state":"tangle","reject_reason":"flat","direction":null,"dir":0,
 "evidence":{"slope_pct":0.00077,"wraps":1,"adx":14.2,"close_vs_ema144":-0.03711},"degraded":false},
 "detail":null,"signal_bar_open_ms":1793102400000,"close":0.008664}
```
**方法**
1. 取近 60 天快照，按 `htf.state`（tangle/flat/undetermined）、`degraded` 分组。
2. 每条用 `signal_bar_open_ms` 之后 48 根 1h K，计算：
   - `dir_hint = sign(close_vs_ema144)`（`htf.direction` 为 null 时用它替代；再跑一遍双向对照组）
   - 顺 `dir_hint` 的 8h / 24h 收益，用 `ATR14` 归一（单位 R 的近似）
   - MFE / MAE（±24h）
3. 输出 `scripts/replay_htf_gate.py`（只读 DB + 交易所 openapi，`--limit` 可抽样）+ CSV。

**判据**
- `tangle` 组前瞻期望 ≈ 0 或为负 → 门有效，维持；
- `tangle` 组前瞻期望显著正（> +0.2R）且样本 ≥500 → 考虑放宽（例如仅"ADX < 20 且 wraps ≥2"才拦）。

**注意**：这是"能不能进候选"的评估，不是"能不能赚钱"——不叠加 AI 与开单闸门，结论只能用于决定是否**减少拦截**。

### 6.2 P1-2 配额按池分配

**处理什么**：`AI_MAX_PER_SCAN=10` 是全局按 `quality_score` 排序（`run_ai_analysis_task` 中 `order_by(quality_score.desc()).limit(quota)`），回踩类质量分基数天然低于突破类 → 被结构性挤出（代码注释已承认 F4）。而近 7 天 48% 命中未被分析。

**实现**
1. 保留一次全局查询，改为窗口函数分池取前 N：
   ```sql
   select * from (
     select sr.*, row_number() over (partition by coalesce(quota_pool,'breakout') order by quality_score desc nulls last, volume_24h desc) rn
     from scan_results sr where scan_record_id=:id and symbol not in (:live_running)
   ) t where rn <= :per_pool
   ```
   `per_pool = ceil(quota / 2)`，或新增 `ai_quota_pullback` / `ai_quota_breakout` 两个配置键（更可控）。
2. 保留"正式网在跑跳过""连损降速"两段现有逻辑。

**判读**：`select quota_pool, count(*) from ai_analyses a join scan_results s on s.id=a.scan_result_id ...` 看两池是否都拿到配额。

**回退**：改回单次 `limit(quota)`。

### 6.3 P1-3 `no_trigger` 细分漏斗

**处理什么**：21,920 次 `no_trigger` 拒绝不可细分，导致"到底该修触点、修金K、还是修腿门"无法回答。

**实现**：`backend/app/services/strategy/trigger.py` 里 `_diag_note` 已有文本，但只进 `funnel_details`。把原因归一成代码，随 `reject_out["stage"]` 一起返回，scanner 记为 `no_touch` / `touch_no_golden` / `leg_gate` / `window_expired` 四类，`stages` 分别计数；前端漏斗 tab 增加这四类。

**判读**
```sql
select t.k stage, sum(t.v::int) n from scan_records s, jsonb_each_text(s.funnel_json->'stages') t(k,v)
where s.created_at > now()-interval '7 days' group by 1 order by 2 desc;
```

### 6.4 P1-4 4h 口径说明（复核后降级，不改代码）

**复核结论（2026-10-27）**：漏斗 `htf_states` 与 `ai_analyses.htf_phase` **来自同一个函数** `technical.analyze_htf_background`，词表相同；差异纯粹来自分母——前者统计**被 4h 门拦下**的币次（`gate_4h` + `htf_state_lock`，天然以 `tangle`/`flat` 为主），后者只统计**已出 suggest** 的候选（天然以排列态为主）。

所以**没有两套算法，也不需要改名**（改名会让 4 个月历史数据对不上，收益为零）。要做的是把口径写清楚：

- 已在 `backend/app/services/scanner.py::_htf_funnel` docstring 写入该口径与"禁止跨分母对比"的提示；
- 本章 §2 L0-4 与 §3 表格同步更正；
- 若将来要在看板同时展示两者，必须各带标签（"被拦下" vs "已分析"），不得合并成一个饼图。

---

## 7. P2（必须先有 P1-1 再决策）

| 编号 | 内容 | 需要先知道什么 |
|---|---|---|
| P2-1 | 摆动 band 改 ATR 归一（`band = max(0.3%, k×ATR/price)`） | k 取多少能让"命中数不爆炸、`retest/swing` 的平均 R 不退" |
| P2-2 | 复合金K 路径：删掉规则 3（前根锤+当根趋势K）与双流星，或排查为何 1/3,330 | 删掉会不会让某些信号消失（回放确认 0 影响即可删） |
| P2-3 | 止损锚 `1.33×ATR` 与 TP `2×ATR` 上限的冲突 | skip 文本已证冲突存在；需回放判断"放宽锚"与"前置放弃"哪个更省 |
| P2-4 | 黑名单自动加黑阈值 | 14 天只加 4 个，阈值偏严；需看"被自动拉黑却后来盈利"的比例 |

---

## 8. 执行顺序与判读

| 批次 | 内容 | 时间 | 判读指标 | 停手条件 |
|---|---|---|---|---|
| 批次 1 | P0-1 + P0-2 + P0-3（可一次部署，全部默认关闭，逐项开启） | 部署当天 | 命中数/天、送 AI 数/轮、avg(R)、开单率 | P0-1 命中 <5/天 → 降到 75 或关 |
| 批次 2 | P1-3（漏斗细分，已实现）+ P1-4（口径说明，文档项） | 已上线 | 漏斗各族计数、`no_trigger` 占比变化 | 无 |
| 批次 3 | P1-2 配额分池 | +3 天 | 两池各拿到的分析数、回踩命中占比 | 回踩 avg(R) 未改善 → 回退 |
| 批次 4 | P1-1 回放脚本 + 报告 | +5 天 | tangle/flat 组前瞻期望 | 无（只读） |
| 批次 5 | 按回放结论选 P2 单项 | +7 天 | 单项上线后 7 天 avg(R) | 任一指标恶化 → 回退 |

**统一的成功判据**（7 天窗口，与上线前 14 天基线比较）：
- `avg(R)/笔` 提升 ≥ +0.05R，或
- 胜率提升 ≥ 5pp 且笔数不低于基线的 70%，或
- 总 R 不变但最大回撤变小。

---

## 9. 附录：复现 SQL

```sql
-- ① 漏斗每日
select date_trunc('day',created_at)::date d, count(*) scans,
 sum((funnel_json->>'universe')::int) u, sum((funnel_json->>'scanned')::int) scanned,
 sum((funnel_json->>'candidates')::int) cand, sum((funnel_json->>'pending_pool')::int) pend
from scan_records where funnel_json is not null and created_at > now()-interval '14 days' group by 1 order by 1;

-- ② 关卡累计
select t.k stage, sum(t.v::int) n from scan_records s, jsonb_each_text(s.funnel_json->'stages') t(k,v)
where s.funnel_json is not null and s.created_at > now()-interval '14 days' group by 1 order by 2 desc;

-- ③ 4h 状态与拒因
select t.k state, sum(t.v::int) n from scan_records s, jsonb_each_text(s.funnel_json->'htf_states') t(k,v)
where s.funnel_json is not null and s.created_at > now()-interval '14 days' group by 1 order by 2 desc;
select t.k reason, sum(t.v::int) n from scan_records s, jsonb_each_text(s.funnel_json->'gate_4h_reasons') t(k,v)
where s.funnel_json is not null and s.created_at > now()-interval '14 days' group by 1 order by 2 desc;

-- ④ 转化（14 天）
select (select count(*) from scan_results where created_at > now()-interval '14 days') hits,
       (select count(*) from ai_analyses  where created_at > now()-interval '14 days') analyzed,
       (select count(*) from ai_analyses  where trade_decision='suggest' and created_at > now()-interval '14 days') suggest,
       (select count(*) from trade_records where opened_at > now()-interval '14 days') opened;

-- ⑤ 质量分 × 结果
select case when s.quality_score>=90 then '90+' when s.quality_score>=80 then '80-89'
            when s.quality_score>=70 then '70-79' when s.quality_score>=60 then '60-69' else '<60' end b,
       count(*) hits, count(t.id) opened,
       count(*) filter (where t.pnl_pct is not null) closed,
       round(avg(t.pnl_pct)::numeric,2) avg_pct
from scan_results s left join trade_records t
  on t.scan_result_id=s.id and t.opened_at > now()-interval '14 days'
where s.created_at > now()-interval '14 days' group by 1 order by 1;

-- ⑥ 触点 × 结果
select s.touch_ref, count(*) trades, round(avg(t.pnl_pct)::numeric,1) avg_pct
from trade_records t join scan_results s on s.id=t.scan_result_id
where t.opened_at > now()-interval '14 days' and t.pnl_pct is not null group by 1 order by 2 desc;

-- ⑦ 配额饿死（近 7 天定时扫描）
with s as (select id, (funnel_json->>'candidates')::int c from scan_records
           where funnel_json is not null and scan_type='scheduled' and created_at > now()-interval '7 days'),
a as (select sr.scan_record_id sid, count(*) n from ai_analyses ai
      join scan_results sr on sr.id=ai.scan_result_id
      where ai.created_at > now()-interval '7 days' group by 1)
select sum(c) cands, coalesce(sum(a.n),0) ai_rows, count(*) scans,
       count(*) filter (where s.c > coalesce(a.n,0)) scans_truncated,
       sum(greatest(s.c - coalesce(a.n,0),0)) starved
from s left join a on a.sid=s.id;

-- ⑧ 未开单原因
select coalesce(nullif(open_block_reason,''),'(未拦截/未尝试)') r, count(*)
from ai_analyses where trade_decision='suggest' and created_at > now()-interval '14 days'
group by 1 order by 2 desc limit 12;

-- ⑨ 单币反复分析（Top 8）
select symbol, count(*) n, count(*) filter (where trade_decision='skip') skips,
       round(avg(recommendation)::numeric,0) avg_rec
from ai_analyses where created_at > now()-interval '14 days'
group by 1 having count(*) filter (where trade_decision='skip') >= 4 order by 3 desc limit 8;

-- ⑩ 观测字段空转核查
select count(*) n, count(*) filter (where golden_combo) combo_true,
       count(*) filter (where entry_lag_pct is not null) lag_notnull
from scan_results where created_at > now()-interval '14 days';
select is_repeat, count(*) from scan_results where created_at > now()-interval '14 days' group by 1;
select feature_key, count(*) from shadow_marks where created_at > now()-interval '14 days' group by 1 order by 2 desc limit 8;
```

---

## 10. 实施记录（批次 1：P0-1 / P0-2 / P0-3）

**状态**：代码已实现并部署，**三项默认全部关闭**（零行为变化），逐项开启、逐项可回退。

### 10.1 改动

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | 新增 `QUALITY_FLOOR: float = 0.0`、`AI_REANALYZE_SKIP_LIMIT: int = 2`、`AI_REANALYZE_MIN_REC: float = 50.0`（含依据注释） |
| `backend/app/services/scan_filters.py` | `SCAN_FILTER_KEYS` 增 `quality_floor`；`ScanFilters.quality_floor: float = 0.0`；`resolve_scan_filters` 接线 `_num("quality_floor", float(settings.QUALITY_FLOOR), minimum=0.0)`（DB 键 > env > 默认） |
| `backend/app/services/scanner.py` | 新增纯函数 `apply_quality_floor(hits, floor) -> (kept, dropped_symbols)`；`run()` 在「质量分排序 / 配额分池 / 重复抑制 / 落库」之前过滤，被剔币写 `funnel_rejects[symbol]="quality_floor"`（前端「筛选漏斗」可见数量），日志记剔除比 |
| `backend/app/api/scan.py` | `scan_filters_effective` 暴露 `quality_floor`；`_SCAN_FILTER_RANGES["quality_floor"] = (0, 110, ...)` |
| `backend/app/tasks/ai_tasks.py` | 新增 `_filter_ai_targets(db, results) -> (kept, unlisted_n, cooled_n)`：① 两网 `listed_symbols()` 并集判定，**任一环境上架即保留**，两网都空（拉取失败）放行；② 单币冷却查当日 `ai_analyses`（`count(skip) >= N` 且 `avg(recommendation) < M` 才剔）；查询异常放行。只作用于批量分析（`only_scan_result_id is None`），命中全被过滤时 summary 写明「未上架 x / 冷却 y」，任务 detail 增 `unlisted_skipped`/`cooldown_skipped` |
| `backend/tests/conftest.py` | 新增 autouse `_no_exchange_info`：默认把 `BinanceTrader.listed_symbols/onboard_dates` 桩成空集合（实现里「空即放行」），保证单测不触网 |
| `backend/tests/test_scan_quality_floor.py` | 新增 11 条：默认 0 / DB 覆盖 env / env 回落 / 脏值回落；过滤函数（0 不过滤、等于下限保留、缺分按 0、全剔、空输入）；API（0~110 接受、111 拒绝、非数值拒绝） |
| `backend/tests/test_ai_dispatch_filters.py` | 新增 10 条：两网都无 → 剔；仅测试网上架 → 留；两网都失败 → 放行；单环境失败 → 用另一环境判定；冷却（达阈值且均分低才剔、均分高保留、未达阈值保留、阈值 0 不查库、查询异常放行）；两道过滤叠加 |
| `frontend/src/components/ScanConfigPanel.tsx` | 筛选配置页新增「质量分下限（0=不过滤）」数字输入（0~110），tooltip 载 14 天分档证据 |

测试：新增 21 条全绿；全量 `pytest tests/` 见 §10.4。

### 10.2 三项开关与回退

| 项 | 开启 | 回退（分钟级） |
|---|---|---|
| P0-1 质量分下限 | 页面「筛选配置 → 质量分下限」填 80，或 `backend/.env` 加 `QUALITY_FLOOR=80` | 填回 0 / 删该行 + `IMAGE_TAG=<当前sha> docker compose up -d --no-build --force-recreate backend celery_worker celery_beat` |
| P0-2 剔除不可交易合约 | 部署即生效（无开关，逻辑本身即「两网都没上架才剔」） | `_filter_ai_targets` 内删该段调用，或回滚镜像 |
| P0-3 单币冷却 | 部署即生效（阈值 `AI_REANALYZE_SKIP_LIMIT=2`）；关闭 = 该值设 0 | `.env` 加 `AI_REANALYZE_SKIP_LIMIT=0` + 重启三容器 |

> P0-2 / P0-3 属于「删除纯浪费」，不改变选币期望，因此部署即开；P0-1 会改变候选集合、且证据为已开单样本上的关联（§1.2 已标注选择偏差），按 §8 批次 1 的口径**先观察再开**。

### 10.3 开启后的判读

见 §5.1 / §5.2 / §5.3 的 SQL 与停手条件。三项共同的 7 天基线对比：

```sql
-- 上线前后 7 天的平均收益与开单率
select date_trunc('day', opened_at)::date d, count(*) n,
       round(avg(pnl_pct)::numeric,1) avg_pct,
       round(100.0*count(*) filter (where pnl_pct>0)/count(*),1) win_pct
from trade_records
where testnet=false and pnl_pct is not null and opened_at > now()-interval '14 days'
group by 1 order by 1;
```

### 10.4 上线记录

| 项 | 值 |
|---|---|
| commit | `e0d1112 feat(scan): 质量分下限 + AI 前剔除不可交易合约 + 单币冷却（docs/16 批次 1）` |
| 上一版 | `afda1f7`（`.deploy_state` `PREV_SHA`，回滚目标） |
| CI | run `40511277567` **success**（scope=all，前端一并重建） |
| 容器 | backend / celery_worker / celery_beat / frontend 全部 `e0d1112` Up |
| 部署时间 | 2026-10-27 13:20 UTC |
| 实测开关 | `settings.QUALITY_FLOOR=0.0`（关）、`settings.AI_REANALYZE_SKIP_LIMIT=2`、`settings.AI_REANALYZE_MIN_REC=50.0` |
| API 生效值 | `/api/scans/config` → `scan_filters_effective.quality_floor = 0.0`、`ai_max_per_scan = 10` |
| 回滚 | `cd /home/ubuntu/github/trading-workbench && scripts/release.sh rollback`（回 `afda1f7`） |

**当前生效范围**：P0-2（剔除两网都未上架的合约）与 P0-3（单币冷却 2 次 / 均分 50）**已开**；P0-1（质量分下限）**保持关闭**，等 §1.2 的选择偏差在实盘再验证一轮后由使用者决定是否设 80。

**当日冷却预演**（部署时点的数据）：符合条件的仅 `JCTUSDT`（今日 skip 2 次、均分 25）与 `NMRUSDT`（2 次、均分 25）——量级很小，符合"只砍反复被拒的币"的设计意图；若某天出现大量冷却，说明行情本身在反复否决策略，需看 §8 的停手条件。

**首批验证点**：下一个整点扫描（14:00 UTC）的 worker 日志应出现 `未上架合约跳过 AI 分析 N 个` 与/或 `单币冷却跳过 AI 分析 N 个`，任务 detail 里带 `unlisted_skipped` / `cooldown_skipped`。

---

## 11. 实施记录（批次 2：P1-3 漏斗细分 / P1-4 口径更正）

### 11.1 P1-3 `no_trigger` 细分（已实现）

**问题**：21,920 次 `no_trigger` 拒绝塌在一个标签里，无法区分"无触点 / 腿门不过 / 有金K无语境 / 待确认超窗"，导致改哪一层只能靠猜。

**实现**

| 文件 | 改动 |
|---|---|
| `backend/app/services/strategy/trigger.py` | 新增 `_diag_stage(diag, code)`（**首个写入者生效**，避免下游覆盖上游卡点）；在 8 个决策点写归一化代码：`insufficient_bars` / `no_touch` / `leg_gate` / `golden_no_context` / `pending_waiting` / `window_expired` / `pending_flip` / `no_golden`。**有活跃 pending 时不写 `no_touch`**——此刻卡点是窗口状态，交给下游分支记录 |
| `backend/app/services/scanner.py` | 无触发分支的 `_reject("no_trigger")` 改为 `_reject(diag["stage"] or "no_trigger")`，关卡代码直接落 `funnel_rejects` → `funnel_json.stages` 自动按类计数；`detail` 文本原样保留 |
| `frontend/src/components/FunnelPanel.tsx` | `FUNNEL_STAGES` 增 8 个细分关卡（含中文标签/颜色/悬停说明），旧 `no_trigger` 标注为"旧轮次汇总"；新增 `NO_TRIGGER_FAMILY` 关卡族，待确认池「无触发」页签按**族**过滤（否则拆分后页签会看起来变空） |
| `backend/tests/test_no_trigger_stage.py` | 新增 10 条：各分支 stage 代码、`_diag_stage` 首写生效、`diag=None` 静默、触点成立但无金K **不算被拒**（走 upsert） |

**兼容性**：旧轮次数据仍是 `no_trigger`，前端按族过滤后历史轮次照常可见；`stages` 里新增键不影响既有读取方（无 schema 变更）。

**判读（7 天）**
```sql
select t.k stage, sum(t.v::int) n
from scan_records s, jsonb_each_text(s.funnel_json->'stages') t(k,v)
where s.funnel_json is not null and s.created_at > now()-interval '7 days'
group by 1 order by 2 desc;
```
预期：`no_trigger` 归零（历史轮次除外），被 `no_touch` / `leg_gate` / `window_expired` 等取代。若 `leg_gate` 占比过高（>25%），说明反向腿门在收紧候选，需回到 §6.1 的回放框架评估；若 `window_expired` 占绝对多数，说明 Stage-1 触点噪声大（与 7.4% 确认率一致），优先考虑收窄触点而不是加金K条件。

### 11.2 P1-4 4h 口径（复核后降级为文档项）

复核发现两个统计**同源**（`technical.analyze_htf_background`），差异只在分母：漏斗统计被门拦下的（tangle/flat 为主），`ai_analyses.htf_phase` 只统计已出 suggest 的（排列态为主）。**不改代码、不改名**，仅：

- `backend/app/services/scanner.py::_htf_funnel` docstring 写明分母与"禁止跨分母对比"；
- §2 L0-4 / §3 P1-4 两处更正（原文的"看板与复盘自相矛盾"是误判）。

### 11.3 测试与上线

- 新增 10 条；因细分改变了 3 条既有断言（`test_scan_funnel` ×2、`test_htf_ranging_filter` ×1 从 `no_trigger` 改为细分代码），属**有意的行为变更**，已在断言处注明日期与依据。
- 全量 `pytest tests/` **1272 passed**；前端 `tsc --noEmit` 通过。
- 上线记录见 §11.4（部署后回填）。

### 11.4 上线记录

| 项 | 值 |
|---|---|
| commit | `6962ba9 feat(scan): 漏斗 no_trigger 细分为 8 类关卡（docs/16 批次 2 P1-3）` |
| 上一版 | `e0d1112`（回滚目标） |
| CI | run `40518838429` **success**（scope=all） |
| 容器 | backend / celery_worker / celery_beat / frontend 全部 `6962ba9` Up |
| 部署时间 | 2026-10-27 14:15 UTC（容器 StartedAt 14:14:56Z，CI 完成 14:15:50Z） |
| 容器内实证 | `detect_trigger` 合成样本：无触点 → `stage=no_touch`；K 线不足 → `stage=insufficient_bars`（细分链路在位） |
| 回滚 | `scripts/release.sh rollback`（回 `e0d1112`） |

**首个真实验证点**：15:00 UTC 整点扫描。届时 `scan_records.funnel_json.stages` 里 `no_trigger` 应消失，取而代之是 `no_touch` / `leg_gate` / `pending_waiting` / `window_expired` 等；前端「筛选漏斗」与待确认池「无触发」页签按族展示。部署前最后一轮（14:00 UTC）的对照基线：`no_trigger=24`、`gate_4h=123`、`htf_state_lock=41`、`range_score=17`。

---

## 12. 观测缺口与更正记录（2026-10-27 事后复核）

本轮审计有两条结论在后续复核中被推翻。写在这里是为了让后人不重复踩：**"日志里没有"不等于"代码没跑"**。

### 12.1 扫描链路日志此前完全不落库（已修）

**事实**：`backend/app/services/log_sink.py::_AppLoggerFilter` 只放行 **WARNING 及以上**（`app.spot.*` 是唯一例外）。而 `scanner.py` 的 13 处 `logger.info` 全是诊断汇总行：

| 日志 | 内容 |
|---|---|
| `扫描完成 #%s: 扫描%d个, 命中%d个（抑制重复%d个）` | 每轮总账 |
| `重复命中抑制 #%s: %d 条已在 %dh 内命中过` | 抑制器是否工作 |
| `质量分下限 %.0f 拦截 #%s: %d/%d` | P0-1 效果 |
| `写入 %d 条扫描结果 #%s` / `写入 %d 条被拦信号影子` | 落库量 |
| `定时扫描后跳过本轮 AI 分析 #%s` | 满仓跳过 |

**实测佐证**（修复前）：
- `app_logs` 近 2 天 5,320 条中「扫描完成」= **0**、「重复命中抑制」全库 = **0**
- worker / beat / backend 三个容器 3 小时日志里出现「扫描」= **0**（worker 只有 1 条 celery 内部的 `run_scan_task received`）
- 而 `scan_records` 每小时都在新增 → **扫描在跑，过程不可见**

**后果**：任何"这轮为什么只出 3 条候选""抑制器有没有生效""质量分下限拦了多少"的问题，都只能靠 `funnel_json` 的计数反推，看不到过程，也无法判断是"没触发"还是"触发后被拦"。

**修复**：`log_sink` 新增 `_INFO_LOGGER_PREFIXES = ("app.spot", "app.services.scanner")`，扫描 INFO 与现货域一样直接落库。量级：每轮 6~8 条汇总行（逐币日志本就是 warning/debug），约 +150 条/天。测试 `backend/tests/test_log_sink_filter.py` 6 条覆盖：框架日志不落库、普通 `app.*` 仅 WARNING+、`app.spot` 与 `app.services.scanner` INFO 放行、同前缀兄弟 logger 不被顺带放行。

### 12.2 更正一：重复命中抑制**没有**失效

原文判定："`REPEAT_WINDOW_HOURS=3` 从未生效，字段形同虚设"。

复核：
- 近 **6 天**：同 `(symbol, signal_type)` 在 3h 内重复的行数 = **0**（窗口内无重复可抑）
- 近 14 天：反事实统计有 **58 行**会被抑制，但**全部落在 10-21 之前**——那是突破类还开着的时候（Donchian 突破每小时都在创新高），也正是 `_suppress_repeat_hits` 注释里写明的存在理由
- A1 下线突破类、A2 禁用多头 ema21 之后，回踩确认要求"新触点 + 新金K"，同一币种同一信号 3h 内再出现几乎不可能

结论：**抑制器按设计工作，当前处于空闲状态**；`is_repeat` 恒 False 属正常（被抑制的行压根不会落库，字段留给"同轮内重复"等其它场景）。日志 0 次的真因是 §12.1 的级别过滤，与抑制器无关。

### 12.3 更正二：4h"两套口径"是同源不同分母

原文判定："漏斗 `htf_states` 与 `ai_analyses.htf_phase` 两套口径并存、看板与复盘自相矛盾"。

复核：两者都出自 `technical.analyze_htf_background` 的 `state`，词表相同；差异纯来自分母——漏斗统计**被 4h 门拦下**的币次（`tangle`/`flat` 为主），`htf_phase` 只统计**已出 suggest** 的候选（排列态为主）。属幸存者差异，**不是两套算法**。已降级为文档项（§6.4），并在 `scanner._htf_funnel` docstring 写明"禁止跨分母对比"。

### 12.4 仍未解释的一项

`golden_combo=True` 在 3,330 条命中里只有 **1 条**、`entry_lag_pct` 只有 1 条非空（L4-2 / P2-2）。这项**尚未**结论：既可能是复合条件（前根锤/流星 + 当根趋势K 同向且 `body/rng≥0.60`）确实极罕见，也可能是 `resolve_golden_evidence` 的复合分支没被走到。查法见 §7 P2-2——用历史 K 线离线跑一遍即可判定，不需要回放工具。

---

## 13. 批次 4：P2-2 复合金K 与 P2-4 黑名单阈值（2026-10-27）

### 13.1 P2-2 复合金K：路径可达，但不该删——改成可观测

**现象**：14 天 3,330 条命中里 `golden_combo=True` 只有 **1 条**、`entry_lag_pct` 只有 1 条非空，看起来像"死路径"。

**离线复算**（30 个主流币 × 340 根 1h K = 10,200 根，直接调用 `resolve_golden_evidence`，脚本 `/tmp/p2_2.py`）：

| 分支 | 次数 | 占金K确认 |
|---|---|---|
| 直通（非单影线，`offset=-2`） | 3,927 | 99.1% |
| 双流星组合 | 10 | 0.25% |
| 复合（前根锤/流星 + 当根趋势K，`offset=-3`） | 24 | 0.61% |

note 分布：`前根流星线+当根趋势K` 15、`前根锤形线+当根趋势K` 9、`双流星组合` 10。**规则本身完全可达**——生产端 1/3,330 不是"从来没走到"，而是走到的概率被两件事压到极低：

1. **复合 ≈「次根趋势K确认」这一条分支**。规则 3 要求：当根**没有**同向金K + 当根是趋势K（`body/rng≥0.60`）+ 前根是锤（多）/流星（空）。在回踩语境里，前一条件与"当根是趋势K"天然互相排斥——回踩触点的当根往往是长影线小实体，而趋势K是"实体占 60% 以上"。
2. 它能发生，基本只有一条路：**pending 建立后，下一根用趋势K确认**（行3 次根确认）。而 pending 的确认率本身只有 7.4%，确认里绝大多数又是"当根直接出金K"。

**裁定：不删**。n=1 无法证伪也无法证实；删掉等于悄悄去掉一整条确认分支，而它的实现成本为零。改为**可观测**：`funnel_json` 增 `combo_confirm` 计数（`sum(1 for h in new_hits if h.get("golden_combo"))`），先积累 1~2 周数据再定去留。判据：若持续为 0 且 `pending` 里也没有 `confirm_pattern` 走趋势K的，则确认为死分支再删。

### 13.2 P2-4 黑名单：1.5R 档不可达是"修好了"，不是"阈值错了"

**14 天正式网实测**（`trade_records` 已平仓亏损单 151 笔）：

| 指标 | 值 |
|---|---|
| 实亏/计划风险 最大值 | **1.33R**（PLAYUSDT -2.80/2.10） |
| ≥1.2R 的笔数 | 11 |
| **≥1.5R 的笔数** | **0** |
| ≥1.8R 的笔数 | 0 |

现有 9 条黑名单全部创建于 **10-15 ~ 10-23**，理由都是"计划风险 10.08 / 84 / 90 / 105 / 119.99 → 实亏 -15.92 / -167.99 / -94.42 / -1148.74 / -2330.51"——那是 **10-20 修仓位算法（docs/14）之前**的巨风险单，不是现在的风险口径。

**结论**：`AUTO_RISK_OVERSHOOT=1.5` 在当前 3 USDT 固定风险 + 1–1.5ATR 止损下几乎不可能触发（要滑点吃掉 50% 以上止损距离）。**这说明仓位算法和止损质量已经修好了，不是阈值设错了**——若把阈值降回 1.2，14 天会拉黑 10 个币，而那些单全是 -1.2R 左右的正常止损（SLIPPAGE + 手续费），属误杀。

**真缺口：反复止损没人管。** 同一币 14 天内多次按止损出场（每次都在 1R 以内，因此永远不触发倍数档）：

| 币种 | 止损次数 | 累计亏损 | 平均超预算 |
|---|---|---|---|
| ICNTUSDT | 4 | -12.28 | 1.11 |
| LIGHTUSDT | 3 | -8.65 | 1.20 |
| DUSKUSDT | 3 | -7.54 | 1.20 |
| SUSDT | 3 | -7.05 | 1.01 |

单币 3~4 次就是 -7 ~ -12 USDT（≈2~4R），比一次"滑点撑爆"更具持续性。

**改动：新增「重复止损」自动档（默认关闭）**

| 配置 | 默认 | 含义 |
|---|---|---|
| `BLACKLIST_REPEAT_STOP_N` | **0（关）** | 窗口内止损次数达到 N 次即冷却拉黑；0=关闭 |
| `BLACKLIST_REPEAT_STOP_DAYS` | 14 | 统计窗口（天） |
| `BLACKLIST_REPEAT_COOLDOWN_DAYS` | 14 | 冷却天数 |

实现要点：
- `blacklist_service._repeat_stopouts()` 只统计 `exit_reason in ("sl","breakeven_sl","trail_sl")` 且 `realized_pnl<0` 且 `testnet=false` 的笔数
- **`exclude_trade_id` 必须排除本次结算的这一笔**：结算路径上 `rec.realized_pnl/closed_at` 已写好，同一 session 的 autoflush 会让它出现在查询里，不排除就会"提前一格拉黑"（这个问题是被单测 `test_repeat_excludes_current_trade` 抓出来的）
- 与倍数档的关系：**倍数档优先**，只有 `overshoot < 1.5` 时才轮到重复档；重复档永远进冷却档（`auto`），永不进 `stop_hard`
- 手动永久拉黑的币不会被覆盖降级（`_upsert` 里 manual 分支保持原样）

**为什么默认关**：只有 4 个币种命中，n 太小；且"反复止损"有时是币种在趋势里正常呼吸（回踩就洗）。建议先开 `N=3` 观察 1~2 周，看被拉黑的币是否真的继续亏——若拉黑后价格反弹到可盈利位置，说明按次数拉黑是错的。

### 13.3 测试与上线

- 新增 `backend/tests/test_blacklist_repeat_stop.py` 10 条（阈值、窗口、只计止损出场、测试网不参与、不降级 manual、倍数档优先、`exclude_trade_id` 语义）
- 既有 `tests/test_blacklist_settle.py` 9 条不受影响
- 全量 **1288 passed**
- 部署后 `BLACKLIST_REPEAT_STOP_N=0` → **零行为变化**；开档只需在 `backend/.env` 写 `BLACKLIST_REPEAT_STOP_N=3` 并重启三容器
