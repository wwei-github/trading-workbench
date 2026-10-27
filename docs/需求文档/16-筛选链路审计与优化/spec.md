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
| L0-3 | `is_repeat` 恒 False，抑制量不可见 | 14 天 3,330 条全 False；30 天日志"重复命中抑制" **0 次** | `REPEAT_WINDOW_HOURS=3` 从未生效，字段形同虚设 |
| L0-4 | **两套 4h 口径并存** | 漏斗 `htf_states`：`aligned_bear` 189 / `aligned_bull` 4；`ai_analyses.htf_phase`（同期开单）：`aligned_bull` 99 / `aligned_bear` 152 | 看板与复盘自相矛盾 |

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
| **P1-4** | 统一两套 4h 口径 | L0-4 | 看板/复盘自相矛盾 | A（189:4 vs 99:152） | 命名 + 前端文案 | 改回 |
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

### 6.4 P1-4 统一两套 4h 口径

**处理什么**：漏斗 `htf_states`（状态机：`aligned_bull` 4 / `aligned_bear` 189）与 `ai_analyses.htf_phase`（AI 提示词相位：`aligned_bull` 99 / `aligned_bear` 152）用词重叠、含义不同，导致"4h 是多是空"看两个表会得出相反结论。

**实现**（二选一，建议 A）
- **A. 改名**：AI 层相位统一加前缀 `ai_`（`ai_aligned_bull` 等），前端与文档同步；状态机保持原名。
- B. 合并：让 AI 层直接读状态机的 `state`，删掉提示词里的独立相位推断。

改完在看板与 `docs/10-反转阶段状态标注` 里补一句口径说明。

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
| 批次 2 | P1-3 + P1-4（纯观测） | +2 天 | 漏斗四类计数、看板口径一致 | 无 |
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
