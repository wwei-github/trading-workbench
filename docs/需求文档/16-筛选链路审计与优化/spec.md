# 筛选链路审计与优化方案（2026-10-27）

> 对象：全市场 → 候选命中 → AI 分析 → 开单 的完整筛选链路。
> 口径：正式网（`testnet=false`），统计窗口 2026-10-13 ~ 10-27（14 天、346 轮扫描），除非另行标注。
> 数据来源：线上库 `trade_records` / `scan_results` / `scan_records.funnel_json` / `ai_analyses` / `pullback_pendings` / `shadow_marks` / `app_logs`，配套 1h K 线重算。
> 相关：[15-实盘止损归因与上线包](../15-实盘止损归因与上线包/spec.md)（A1/A2/A3/B1/A6/A7 均已上线）。
> 相关（2026-10-28 增）：[17-复盘模块审计](../17-复盘模块审计/spec.md)——复盘侧的 P0（补丁门槛、expired 语义、回放落 entry_diag）与本文件的 D2/D5/D7/D8 判据同源。

> **当前状态（2026-10-28 更新）**：P0 三项、P1-1/P1-3/P2-2/P2-4、D1/D4/D6 已上线；
> **D2/D5/D7/D8 四项已按下暂停**，恢复条件统一写在 [§22 挂起清单](#22-挂起清单2026-10-28)——
> 四项都缺同一样东西：**连续多日的自动归因快照**（D6，08:10 北京产出）。
> 暂停不等于否决，是"证据不够、改了容易改反"。

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

| 编号 | 名称 | 层 | 处理什么问题 | 证据强度 | 成本 | 回退 | **状态** |
|---|---|---|---|---|---|---|---|
| **P0-1** | 质量分下限 80 | L5-3/L6 | 低质量命中占用 AI 配额、进开单池 | B（n=185，单期） | 配置 + 30 行 | 置 0 | ✅ 代码上线（`e0d1112`）／⏸ 开关 **2026-10-27 开至 80**，观察 7 天 |
| **P0-2** | AI 前剔除不可交易合约 | L7-1 | 白烧 8% LLM 配额 | A（82 次，机制清楚） | 20 行 | 删判断 | ✅ 上线即生效（`e0d1112`） |
| **P0-3** | 单币 AI 冷却 | L6-5 | 同币当日反复分析反复拒 | A（机制清楚） | 30 行 | 参数置 0 | ✅ 上线即生效（`e0d1112`） |
| **P1-1** | 4h 门反事实回放 | L0-1/L2 | 判断 `tangle` 门是否划算 | 素材 33,637 条 | 1 个离线脚本 | 只读 | ✅ 结案（§15）：整体 +0.025R，不放宽 |
| **P1-2** | 配额按池分配（= **D5**） | L6-1/L6-3 | 回踩被结构性挤出、48% 饿死 | A（机制清楚） | 40 行 | 改回全局 limit | ⏸ **挂起**（A1 下线突破类后突破池为空；替代方案 C 也见 §22） |
| **P1-3** | `no_trigger` 细分漏斗 | L4-5 | 改哪层靠猜 | 机制清楚 | 25 行 | 删细分键 | ✅ 上线（`6962ba9`），15:00 轮已见细分 |
| ~~P1-4~~ | ~~统一两套 4h 口径~~ → 降级为文档项 | L0-4 | 复核后确认是分母不同而非算法冲突 | A（同源函数） | 注释 + 文档 | — | ✅ 结案：非缺陷，不改名 |
| **P2-1** | 摆动 band 改 ATR 归一（= **D2**） | L4-4 | 高波动币门虚设、低波动币永不入带 | C（需回放定 k） | 20 行 | 改回 0.3 | ⏸ **挂起**（§22：等 D6 快照给出 band 分档与 ATR 分档的对照） |
| **P2-2** | 复合金K 路径处置 | L4-2 | 1/3,330 的死路径 | A（计数确凿） | 删 60 行 或 排查 | 恢复 | ✅ 结案（§13.1）：可达但罕见，加 `combo_confirm` 观测 |
| **P2-3** | 止损锚 / TP 上限冲突（= **D3**） | L7-3 | 宽结构币反复回炉后必 skip | B（skip 文本，n≈20） | 参数或前置判定 | 改回 | ✅ **结案（§20.6）**：拍板 A——`TP1_MAX_ATR` 维持 2.0，只保留解耦 `STOP_ANCHOR_MAX_ATR` |
| **P2-4** | 黑名单阈值复核 | L1-2 | 14 天只自动加 4 个 | B（n=4） | 参数 | 改回 | ✅ 代码上线（`9275122`）／⏸ 开关 **2026-10-27 开至 N=2**，观察 14 天 |
| **A7** | 命中标注按单笔归属 | 展示层 | 扫描结果标全量 3 天、交易复盘一条不标 | 需求 | 前后端 | 前端回滚 | ✅ 上线（`afda1f7`），三处待人工验收 |
| **D7** | 同向簇风险约束（新增） | L8 | 同小时同向 5 笔同时被扫 | C（10-25 那簇 +7.6R，10-26 那簇亏） | 30 行 | 删判断 | ⏸ **挂起**（§22） |
| **D8** | `STATE_ALIGNED` 加 `bull_dip`（新增） | L2 | bull_dip 整批被 4h 状态锁拒 | C（顺侧 +0.117R / 逆侧 −0.534R，方向口径未理顺） | 1 行 + 口径确认 | 删该状态 | ⏸ **挂起**（§22） |

**状态图例**：✅ 已上线并验证 ／ ⏸ 挂起或已开开关待观察 ／ ❌ 否决（见 §4）
**D 系列编号**是 2026-10-28 对话里按"剩余工作"排的顺序，与本表编号的对应：
D1=§19 probe、D2=P2-1、D3=P2-3、D4=§18.5、D5=P1-2 替代方案、D6=§21、D7/D8=本表新增。

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

结论：**抑制器按设计工作，当前基本空闲**；`is_repeat` 恒 False 属正常（被抑制的行压根不会落库，字段留给"同轮内重复"等其它场景）。日志 0 次的真因是 §12.1 的级别过滤，与抑制器无关。

**2026-10-27 15:00 UTC 实证**（日志落库修复后的第一轮）：`重复命中抑制 #cdb9bf3e…：1 条已在 3h 内命中过，不再落库`，随后 `扫描完成：扫描290个, 命中1个（抑制重复1个）`——抑制器确实会拦，且拦完计数正确。

### 12.3 更正二：4h"两套口径"是同源不同分母

原文判定："漏斗 `htf_states` 与 `ai_analyses.htf_phase` 两套口径并存、看板与复盘自相矛盾"。

复核：两者都出自 `technical.analyze_htf_background` 的 `state`，词表相同；差异纯来自分母——漏斗统计**被 4h 门拦下**的币次（`tangle`/`flat` 为主），`htf_phase` 只统计**已出 suggest** 的候选（排列态为主）。属幸存者差异，**不是两套算法**。已降级为文档项（§6.4），并在 `scanner._htf_funnel` docstring 写明"禁止跨分母对比"。

### 12.4 仍未解释的一项

`golden_combo=True` 在 3,330 条命中里只有 **1 条**、`entry_lag_pct` 只有 1 条非空（L4-2 / P2-2）。**已在 §13.1 结案**：离线复算 10,200 根 K 证明规则可达（复合 24 次 / 直通 3,927 次），生产端 1/3,330 是"当根无同向金K + 当根趋势K + 前根锤/流星"三重合取天然罕见所致，不是死代码。已改为 `funnel_json.combo_confirm` 计数观测。

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

---

## 14. 生产验证记录：2026-10-27 15:00 UTC 扫描

### 14.1 两项观测上线即生效

| 观测 | 结果 |
|---|---|
| `funnel_json.combo_confirm` | 已出现，本轮 `0`（符合预期：复合确认≈次根趋势K确认，天然罕见） |
| 扫描 INFO 落库 | `app_logs` 首次出现 `logger_name='app.services.scanner'` 的 INFO 行，单轮 10 条 |

单轮日志实证（修复前完全不可见）：

```
15:00:01  24h成交额 ≥ 2,000,000 USDT 过滤：518 → 297
15:00:01  新币冷静期过滤（上市 <14 天）：297 → 296
15:00:02  风险档位过滤（最大杠杆 <10×）：296 → 296
15:00:02  黑名单过滤（9 个生效）：296 → 290
15:00:11  重复命中抑制 #cdb9bf3e…：1 条已在 3h 内命中过，不再落库
15:00:11  写入 1 条扫描结果 #cdb9bf3e…
15:00:11  写入 163 条被拦信号影子 #cdb9bf3e…
15:00:12  扫描完成 #cdb9bf3e…：扫描290个, 命中1个（抑制重复1个）
```

### 14.2 P1-3 漏斗细分在生产生效

同一轮 `stages`（对比 14:00 部署前）：

| 14:00（旧） | 15:00（新） |
|---|---|
| `no_trigger: 24` | `no_touch: 28`、`pending_waiting: 14`、`leg_gate: 2`、`window_expired: 1` |

即：24 条"无触发"里，**28 条其实是"根本没碰到任何触点"**、14 条在等次根确认、2 条被反向腿门拒、1 条确认窗过期。这在旧口径下是一坨 `no_trigger`，看不出该改哪层。

### 14.3 一个必须记住的运营事实：手动开单会绕过 `entry_pos` 闸

同一轮命中的 `ELSAUSDT` long（`retest` / `piercing_line` / 质量分 99）走完了全链路：

```
15:00:58  已分发即时开仓任务: ELSAUSDT（评分 60.00）
15:01:00  已发送正式网开关关闭提醒 … 按钮=live-1
15:01:02  开仓跳过 ELSAUSDT：下单环境未上架该合约（exchangeInfo 校验）
15:01:12  开仓 ELSAUSDT：手动开单市价入场（现价 0.0804 对 AI 入场 0.08038 偏离 0.02%…）
15:01:12  开仓 ELSAUSDT：入场位置 pos=0.31 < 0.60，手动开单按用户意图放行（entry_pos 闸）
```

落库 `raw.entry_diag`：`pos=0.3059 / room_r=1.224 / stop_atr=1.413 / entry_path=market`。

**含义**：`force_market=True`（页面/飞书按钮手动开单）按设计跳过 `entry_pos` 闸，只写一行 INFO 日志。**当双网自动开单都关闭时，实盘每一笔都是手动单 → B1 实际上永远不会拦任何单**。这不是 bug，是 B1 的适用边界：

- 要 B1 真正生效，必须让自动开单通道跑起来（至少一侧开启）
- 若坚持全手动，则 B1 只有"记录"价值；此时更合适的做法是**在手动开单前就把 pos 打进确认弹窗**（前端改动，未做）
- 另注意：该单是"manual"成交，因此在 §15 归因里属于"人工决策"样本，不应用来评价 B1

### 14.4 顺带观察到的开关状态变化

`system_config.trading_enabled_testnet` 在 10:00 后由 `f` 变为 `t`（`trading_enabled_live` 仍为 `f`），这使 `_dispatch_immediate_open` 的 `auto_open_on` 成立并正常分发（属于预期行为）。当日测试网仍无新开仓（`testnet=true` 的今日开仓数 = 0）。

---

## 15. P1-1：4h 门反事实回放（2026-10-27）

### 15.1 目的与方法

**要回答的问题**：被 4h 前置门（`gate_4h` 的 tangle/flat + `htf_state_lock` 的非 aligned 状态）拦下的那些时刻，如果放行去做，是**省了亏损**还是**砍了利润**？这个结论决定 P2-1（band 改 ATR 归一）、P2-3（止损锚 1.33ATR 与 TP 上限冲突）要不要做——它们都以"要不要放宽 4h 门"为前提。

**素材**：`shadow_marks.feature_key='htf_blocked'`（14 天 33,964 条 / 473 币）。取被拦次数最多的 40 个币、近 10 天，**6,112 条**拦截记录，逐条回放。

**回放口径**（脚本 `/tmp/p11_replay.py`，可复跑）：

1. 按 `value_json.signal_bar_open_ms` 定位信号根（`klines` 最后一根已收盘 1h K）；
2. 方向：`technical.compute_indicator_votes(klines[:idx+1])` 的 `net`，`|net| ≥ 2` 才算"会下单"（与扫描的 votes 门同阈值）——**注意方向取自动量票，不是 4h 状态方向**，这正是"如果没有 4h 门，系统会怎么下"的真实含义；
3. 入口 = 信号根收盘价；止损 = **1.33 × ATR14**（与 AI 卡 `stop_anchor` 的锚定倍数一致）；止盈 = 1.5R（`AI_RR_MIN`）；
4. 前向 48 根 1h K 逐根判定，同根内止损优先（保守）；超时按最后一根收盘折算 R；
5. 按 `state` 与 `state_dir × 票方向`（顺侧/逆侧）归组。

**能力边界（必须写在前面）**：这是"动量票触发的裸信号"回放，**没有叠加触点/金K/腿门/质量分**，所以它的样本集比真实链路宽，绝对值不能直接当收益预期；可用的是**同一口径下的组间对比**（省了还是砍了）。

### 15.2 总量：4h 门拦下的是一个"期望≈0"的群体

| 指标 | 值 |
|---|---|
| 拦截记录 | 6,112 条 |
| 其中"会下单"（`\|net\|≥2`） | **4,466 条（73%）** |
| 均 R | **+0.025R** |
| 胜率 | 41.6%（1.5R:−1R 的盈亏平衡点是 40%） |
| 出场构成 | 止盈 1,710 / 止损 2,502 / 超时 254 |

→ **放行这批交易的期望与零无法区分**。4h 门既不是亏损来源，也不是被埋没的利润来源；它实际决定的是**交易量**（73% 的合格动量时刻被它挡掉）。

### 15.3 按状态拆：拦对的和拦错的规模相当

| 状态 | 可下单 | 均 R | 胜率 | 总 R | 判定 |
|---|---|---|---|---|---|
| `tangle`（缠绕） | 2,668 | −0.004 | 40.9% | −10.2 | 掷硬币（占全部拦截 61%） |
| `bull_dip`（牛内回调） | 482 | +0.011 | — | +5.4 | 中性 |
| `bear_rally`（熊内反弹） | 532 | +0.081 | — | +42.9 | 略砍利润 |
| `turn_bull`（多头拐头） | 129 | +0.31 | — | +39.7 | 砍利润 |
| `macro_cross_bull` | 91 | **+0.71** | 69.0% | +64.3 | **明显砍利润** |
| `turn_bear`（空头拐头） | 92 | +0.20 | — | +18.6 | 略砍利润 |
| `macro_cross_bear` | 281 | **−0.40** | 29.3% | −112.5 | **拦对了** |
| `bull_dip`/`bear_rally` 的 flat 子档 | — | ±0.13 | — | — | 噪声 |
| 状态缺失（`?`） | 105 | +0.762 | 70.5% | +80.0 | 数据质量异常，不计入结论 |

### 15.4 按顺逆侧拆：结论与直觉相反

| 组 | n | 均 R | 总 R |
|---|---|---|---|
| `bull_dip` 顺侧（牛市回调做多） | 383 | **+0.117** | +44.6 |
| `bull_dip` 逆侧 | 99 | **−0.534** | −52.9 |
| `bear_rally` 顺侧 | 211 | −0.171 | −36.0 |
| `bear_rally` 逆侧（熊反弹做空） | 321 | **+0.245** | +78.7 |
| `macro_cross_bear` 顺侧 | 164 | **−0.497** | −81.4 |
| `macro_cross_bear` 逆侧 | 117 | −0.254 | −29.8 |
| `macro_cross_bull` 顺侧 | 44 | +0.193 | +8.5 |
| `macro_cross_bull` 逆侧 | 47 | **+1.186**（89%） | +55.7 |
| `turn_bull` 逆侧 | 128 | +0.318 | +40.7 |
| `turn_bear` 逆侧 | 83 | +0.213 | +17.7 |
| `aligned_bear` 顺侧 | 32 | −0.141 | −4.5 |

三条可读结论：

1. **"顺侧"不等于"好"**：`macro_cross_bear` 顺侧（死叉初期做空）−0.497R 是全表最差组之一——死叉确认时价格已跌完一段，此时做空等于接在末端；而 `macro_cross_bull` 逆侧 +1.186R 是最强组（金叉初期逆着做空反而赚，同样的"末期"效应）。
2. **过渡态里的逆侧反而有效**：`bear_rally` 逆侧 +0.245（n=321）、`turn_*` 逆侧 +0.32/+0.21，都是"反弹到结构位再顺势"的形态，与 pullback 信号的语义一致。
3. **`bull_dip` 顺侧（+0.117R / n=383）被判"非 aligned"整批拦掉**，是目前规模最大、方向最明确的一处"拦错"。

### 15.5 裁定

- **不放宽 4h 前置门**（P2-1 band 归一、P2-3 止损锚冲突**不以此为前置**，降为独立小项）。理由：整体期望≈0，改它只是放大交易量、不改善期望；而分状态重组（放 `bull_dip` 顺侧 + 拦 `macro_cross_bear` 顺侧）需要单个状态样本 ≥300 且跨期复现，当前只有单期 10 天、部分组 n<100。
- **P2-1 / P2-3 的处理**：不再等 4h 门结论，但它们自身证据也不足（`STRUCTURE_TOUCH_BAND_PCT` 与振荡周期的耦合需另一组实验），维持"待办、不排期"。
- **值得下一批做的唯一一条**：把 `bull_dip`（牛内回调）与 `bear_rally`（熊内反弹）纳入**观察维度**——在 §9 的 7 天验收里按 `ai_analyses.htf_phase` 分组统计 pullback 单的实际 R，若 `bull_dip` 组持续为正，再讨论是否把它加入 4h 放行白名单（代码位置：`backend/app/services/strategy/technical.py` 的 `STATE_ALIGNED`，当前只含 `aligned_bull/aligned_bear`）。
- **不做**：按本次模型直接改门槛。样本单期、模型未叠加触点与质量分、47~383 的组样本量均不足以支撑行为变更。

### 15.6 复现

```bash
# 1) 导出被拦样本（40 个高频币、近 10 天）
ssh root@<host> 'docker exec trading-workbench-db-1 psql -U postgres -d trading_workbench -q -f /tmp/p11_export.sql'
# 2) 本地回放（需 backend 依赖）
PYTHONPATH=backend ~/.pyenv/versions/3.11.9/bin/python /tmp/p11_replay.py
```

判读要点：看 `均 R` 与 `总 R` 的符号是否与样本量匹配；`tangle` 占 61% 但均 R≈0，说明"缠绕不交易"这条设计既没赚也没亏，只是让系统少交易。

---

## 9bis 观察维度：按 4h 阶段分组验收（2026-10-27 补）

§15 的回放给出一个明确信号：**`bull_dip`（牛内回调）顺侧做多 383 笔 +0.117R 被“非 aligned 一律拦”整批挡掉**，而 `macro_cross_bear` 顺侧（死叉初期做空）−0.497R 拦得对。但那是单期模型回放，不能直接改成行为，所以先把它变成**验收里的一个分组维度**。

### 9bis.1 分组 SQL（7 天验收时跑）

```sql
-- 按 4h 阶段分组：真实成交的 pullback 单表现
SELECT COALESCE(a.htf_phase, '(未记录)') AS htf_phase,
       COUNT(*) AS n_trades,
       ROUND(AVG(t.pnl_pct / 100.0), 3) AS avg_r,
       ROUND(SUM(t.pnl_pct / 100.0), 2) AS sum_r,
       ROUND(COUNT(*) FILTER (WHERE t.pnl_pct > 0) * 100.0 / COUNT(*), 1) AS win_pct
FROM trade_records t
LEFT JOIN ai_analyses a ON a.id = t.ai_analysis_id
WHERE t.testnet = FALSE
  AND t.signal_type = 'trigger_pullback_confirm'
  AND t.pnl_pct IS NOT NULL
  AND t.opened_at >= now() - interval '7 days'
GROUP BY 1
ORDER BY sum_r DESC;
```

```sql
-- 被 4h 门拦下的量（判断放行会带来多少额外样本）
SELECT value_json->'htf'->>'state' AS state,
       COUNT(*) AS blocked,
       COUNT(DISTINCT symbol) AS symbols
FROM shadow_marks
WHERE feature_key = 'htf_blocked' AND created_at >= now() - interval '7 days'
GROUP BY 1 ORDER BY 2 DESC;
```

### 9bis.2 判据与动作

| 观察 | 含义 | 动作 |
|---|---|---|
| `aligned_bear` 组 avg_r ≥ +0.15R（与 10-25~27 的 46% 胜率一致） | 现役放行集合健康 | 继续 |
| `aligned_bear` 组 avg_r < 0 | 放行集合本身有问题 | 回看 `entry_pos` 与触点在 `aligned_bear` 内的分布 |
| `bull_dip` 的影子样本（`shadow_marks` 该 state 的拦截量）≥ 300/7 天 | 有足够样本讨论放行 | 把 `bull_dip` 加入 `STATE_ALIGNED`（`backend/app/services/strategy/technical.py`）做 7 天 A/B |
| `bull_dip` 拦截量 < 100/7 天 | 样本不足 | 维持观察，不动作 |

### 9bis.3 现状（2026-10-27）

- 生产 4h 放行白名单仍是 `STATE_ALIGNED = ("aligned_bull", "aligned_bear")`，`bull_dip`/`bear_rally` 全部走 `htf_state_lock` 拒绝
- 因此 **`bull_dip` 的真实成交组在当前配置下恒为 0**，上表第二行只能靠 `shadow_marks` 的拦截量来衡量"要不要放"
- `htf_phase` 列自 2026-10-10 起有值，更早的行是 NULL（分组时会落到「(未记录)」桶，判读时忽略该桶）

---

## 16. P1-1 加深：完整链路回放（2026-10-27，结论：不可用）

### 16.1 做了什么

在 §15 的粗模型（动量票 + 1.33ATR/1.5R）之上，把真实触发链路串进回放：`_detect_retest` / `_detect_swing_touch` / `_detect_ema_touch(21/55/144)` + `_pullback_leg_gate` + `resolve_golden_evidence`，脚本 `/tmp/p11_full.py`。

**对照组**（可信度检验）：取近 10 天线上真实产出的 `trigger_pullback_confirm` 信号 **620 条**（40 个高频币），用同一套函数重放，看能否复现线上信号。

### 16.2 结论：复现率不够，回放不可用于决策

| 检验 | 结果 |
|---|---|
| 触点复现（`bar = floor(signal_created_at) - 1h`，禁用表按历史默认不生效） | **66.7%**（8 币子样本 132 条中 88 条） |
| 触点复现（`bar = floor(signal_created_at)`，即错一小时） | 45.5% |
| 完整信号复现 | **2.4%**（620 条中 15 条） |

三条已知原因：

1. **retest 的事件派生路径无法复现**：线上 `retest` 来自扫描器的 `breakout_events` 状态机快照，回放只能用结构推导兜底（`_structural_retest_level`），两者命中集合差很多。而 `retest` 在 331 笔样本里是**期望最高**的触点（+33.07R）。
2. **`ema21_4h` 无法复现**：需要 4h K 线，回放只喂 1h。
3. **历史信号用的是历史配置**：现在多头禁用 `ema21`，10-17~10-27 的信号当时是允许的；按现配置回放必然漏掉多×ema21 那一批。

**因此 §15 的 `+0.025R` 仍是当前唯一可用的数字口径**（粗、但复现性不依赖这些环节）。本节的价值是**划清边界**：想让"完整链路回放"成为可信工具，必须先让线上**落盘输入**，而不是事后从 Binance 重取。

### 16.3 若要做成可信回放，缺的是三样数据（不是代码）

| 缺口 | 现状 | 代价 |
|---|---|---|
| 每次扫描的 K 线快照 | 只有实时拉取，无落盘 | 每轮 518 币 × 1h×240 根 ≈ 30MB/轮，需采样或只存信号币 |
| `breakout_events` 历史序列 | 只有"当前活跃"快照 | 每次状态变更落一条事件日志（已有 `scan_records` 可挂） |
| 4h 上下文快照 | 只存 `htf` 摘要（状态/斜率/ADX），不存 4h K 线 | 只存状态机输出即可（`_detect_htf_ema21_touch` 需要 4h EMA21 值，可在扫描时一起落） |

**建议**：若后续确实要靠回放做参数决策，优先落"被 4h 门拦下的 bar 的触点/金K解析结果"（在 `shadow_marks.value_json` 里加 `touch_ref`/`golden` 两个字段即可），这样下次不用重算链路，直接按 group 统计——成本约 20 行代码，远低于快照方案。**（2026-10-27 已实施，见 §19）**

### 16.4 本次加深顺带得到的下限数据（仅供参考，不作决策）

在"完整链路会出信号"的 289 条被拦 bar 上（占 6,041 条的 4.8%）：均 R −0.113R、胜率 36.3%（96 TP / 178 SL / 15 超时）。因复现率不足，该数字**系统性偏向**能被回放检出的触点（swing 146 / retest 122 / ema21 25 / ema55 5），不代表全部被拦信号。

---

## 17. 开关启用记录（2026-10-27 20:00 UTC）

| 开关 | 值 | 方式 | 回退 |
|---|---|---|---|
| `QUALITY_FLOOR`（P0-1） | **80** | `system_config.scan_filters.quality_floor = 80`（DB，下轮扫描即生效） | 页面改 0 或 SQL `jsonb_set(...,'{quality_floor}','0')` |
| `BLACKLIST_REPEAT_STOP_N`（P2-4） | **2**（窗口 14 天 / 冷却 14 天） | 服务器 `backend/.env` + `IMAGE_TAG=ccd738a docker compose up -d --no-build --force-recreate backend celery_worker celery_beat` | 改回 0 后同一条命令 |

验证（生产容器内实测）：
```
REPEAT_STOP_N= 2 days= 14 cool= 14 pos= 0.6
/api/scans/config → quality_floor 80.0 ; allowed_triggers ['trigger_pullback_confirm']
```

**观察期**：
- `QUALITY_FLOOR=80`：看 7 天候选量（预期从 ~9.6/轮 降到 ~5/轮）与 `scan_records.funnel_json.stages.quality_floor` 计数；若单日成交 <2 笔且 `quality_floor` 拦截 >70% 候选 → 降回 70 或 0。
- `REPEAT_STOP_N=2`：看 `blacklist` 表是否新增 `origin='auto'` 且理由含"第 2 次止损出场"；若 7 天内新增 >5 个 → 阈值偏松（改为 3）。

---

## 18. 手动开单入场位置提示（方案 B，2026-10-27）

**问题**：`ENTRY_POS_MIN_RATIO=0.6`（B1）只拦自动开仓；手动开单 `force_market=True` 按设计跳过。双网自动开仓都关闭时，实盘每笔都是手动单 → **B1 实际不拦任何单**（`ELSAUSDT` 案例：pos=0.31 仍开单）。

**做法**：不改变"人做最终决定"的语义，把同一个 `pos` 提前显示在开单确认弹窗里。

- 后端 `GET /api/trades/entry-preview?symbol=&direction=`（`backend/app/api/trades.py`）：复用 `entry_diagnostics.bar_position` 与 `closed_bars`，返回 `pos / pos_min / blocked / price / bar_low / bar_high / bar_open_ms / note`；算不出全部返回 `null`，绝不影响开单主链路。
- 前端 `frontend/src/components/ResultTable.tsx`：与 `openAvailability` 并行拉取；弹窗新增「入场位置」一行，低于门槛时红色提示「自动开仓会被拦下，手动开单不拦，请自行判断」，并附该区间的实测区间数据。
- 测试 `backend/tests/test_entry_preview.py` 6 条（算数/超阈/未超/开关关/空K线/拉取失败）。

**生产验证**（ccd738a）：
```json
{"symbol":"ELSAUSDT","pos":0.2968,"pos_min":0.6,"blocked":true,
 "note":"入场位置 0.30 低于门槛 0.6：2026-10-25~27 正式网同区间 34 笔，胜率 17.6%、合计 −14.30R；pos≥0.6 的 22 笔 63.6%、+18.42R"}
```

**注意**：飞书卡片「开单」按钮路径**没有**这个提示（卡片在点击前发出，无法预知当时的 pos）。若需要，可在卡片里嵌一句"当前 pos 见页面"或改成点击后二次确认——未做。

---

## 19. D1 实施：被拦候选当场落触点/金K解析（2026-10-27）

### 19.1 为什么必须先做这个

§16 已证明：完整链路回放的复现率只有 2.4%，根因不是代码写错，而是**被拦候选的输入没落盘**——`shadow_marks` 只有 4h 证据（state / reject_reason / slope / ADX），触点和金K要在事后按 1h K 重算，而 `retest` 的事件派生路径、`ema21_4h` 的 4h K 线都重建不出来。

### 19.2 改了什么

| 文件 | 改动 |
|---|---|
| `backend/app/services/strategy/trigger.py` | ① 把 `evaluate_pullback_trigger` 内联的触点探测抽成 `_probe_touches(klines, direction, flt, *, retest, htf, htf_dir, diag)`（逐行等价，真实信号与被拦候选共用同一套判定）② 新增 `probe_blocked_context(klines, flt, retest=None) -> dict`：用 `compute_indicator_votes` 复算方向票取符号当方向（`\|net\|<1` 时不猜，留 None），再跑 `_probe_touches` + `resolve_golden_evidence`，返回 `{votes_net, touch_ref, touch_price, golden{pattern,offset,note}}`；异常全吞 |
| `backend/app/services/scanner.py` | `blocked_shadows[symbol]` 快照新增 `"probe": _blocked_probe(klines, flt, retests.get(symbol))`；`_blocked_probe` 是容错包装（异常返回 `{}`），保证影子/诊断绝不中断扫描主循环 |

开销：每轮约 42 个被拦币 × （票数 2ms + 触点 3ms + 形态 5ms）≈ 0.5 秒，相对扫描均值 14.8s 可忽略。

### 19.3 口径边界（写清楚免得后续误读）

- **方向是复算的**，不是线上当时的候选方向（4h 门先于票数，线上没有那个方向）。所以 `probe.votes_net` 与真实信号的 `votes_json.net` 可能不同，回放时以 `probe` 为准。
- **触点走同一套禁用表**：`flt.touch_refs_for(direction)` 生效，即多头被禁的 `ema21` 不会出现在被拦候选里——这保证了"被拦候选"与"真实信号"的候选池口径一致。
- **白名单全禁时回落通用白名单**（防停摆语义），因此会出现"白名单明明只配了 ema21、却标出 ema21"的情况，属设计。

### 19.4 测试

`backend/tests/test_blocked_probe.py` **9 条**：`net=0` 不猜方向、`net<0` 走 bearish、触点与位价落库、无触点留 None、金K只留三字段、异常返回结构完整 dict、禁用表在其他触点存在时生效、全禁回落、空头侧不受多头禁用影响。全量 `pytest tests/` **1303 passed**。
实盘口径抽查（ETHUSDT 240 根 1h）：`{"votes_net": 3, "touch_ref": "swing", "touch_price": 2317.48, "golden": {"pattern": "piercing_line", "offset": -2}}`。

### 19.5 下一步（D1 之后才有意义的事）

1. 攒 2~3 天数据后，用 `shadow_marks.value_json->'probe'->>'touch_ref'` 直接分组统计被拦候选的触点分布——**不再需要重算链路**。
2. 届时可以回答 §15 没能回答的问题：被 `gate_4h` 拦下的那批，`touch_ref` 分布是否与真实信号一致；若某类触点（如 retest）被 4h 门系统性拦掉，那 P2-1（band 归一）就该排到 P2-3 前面。
3. 真正的"可信回放"仍缺 `breakout_events` 历史序列与 4h K 线快照，但触点/金K这一层现在已经不再是瓶颈。

### 19.6 首批实测（16:00 UTC 扫描，上线后第一轮）

`shadow_marks` 近 90 分钟：16:00 那轮 **158 条被拦候选，`probe` 落地率 100%**（15:00 那轮 163 条为 0，符合"部署前无此字段"）。

| 指标 | 数量 | 占被拦 |
|---|---|---|
| 有任一触点命中 | 40 | 25.3% |
| 有同向金K证据 | 27 | 17.1% |
| **触点 + 金K 同时成立（本会出回踩信号）** | **10** | **6.3%** |

`would_signal` 的构成：`tangle` 5 + `tangle/flat` 2 + `bear_recover` 1 + `turn_bear` 1 + `turn_bull` 1；触点 × 方向 = `ema21×空 6` / `retest×多 3` / `swing×多 1`。

**这组数字修正了 §15 的读法**：§15 的"73% 会下单"回答的是"若只按方向票就下单"，与"会不会出真实回踩信号"不是同一个问题。真实口径下被 4h 门拦下的候选里**只有 6.3% 本来会出信号**（与 §16.4 的 4.8% 深回放估计吻合）。结论方向不变（门主要在控量），但代价比 §15 读起来更小：被拦的绝大多数本来就不会出信号。

**下一步（1~2 天数据后）**：按 `probe.touch_ref` 分组看"被系统性拦掉的触点类型"——若 `retest`（期望最高触点）在 `would_signal` 里占比持续偏低，才需要动 P2-1 的 band。

## 20. D3（P2-3）：止损锚与止盈上限的耦合（2026-10-28）

### 20.1 问题是什么

`TP1_MAX_ATR=2.0`、`AI_RR_MIN=1.5`，而提示词里"结构位距入场 > X×ATR 就不作止损锚
（降级金K极值锚）"的 X 是**现算**的：`X = TP1_MAX_ATR ÷ rr_min = 2.0 ÷ 1.5 ≈ 1.33`。

这意味着两个本该独立的旋钮被焊死在一起：

| 想做的事 | 直接后果 |
|---|---|
| 想让止盈一挂得更远（提高 `TP1_MAX_ATR`） | 止损锚门槛同步放宽，更多单用上更远的结构位止损 → 止损整体变宽 |
| 想收紧止损锚 | 止盈上限同步收紧，TP1 被压到 `1.5R` 兜底价 |

而 14 天正式网实测**宽止损更差**：止损 1–1.5ATR 18 笔 **+9.90R**（唯一强正档）、
1.5–2ATR 11 笔 −2.44R、≥2ATR 13 笔 **−4.06R**。所以"放宽止盈"这个动作会顺着
耦合偷偷把策略推向表现最差的那一档，而表面上只是改了一个止盈参数。

### 20.2 实测：TP1 到底被钉住了多少

样本：正式网 `trigger_pullback_confirm`、`opened_at ≥ 2026-10-15`、已平仓
**218 笔**（`ΣR=+51.19`、胜率 45.4%；出场 `sl 74 / manual 56 / tp1_trail 49 /
trail_sl 22 / breakeven_sl 10`）。

以**初始止损**（`ai_snapshot->>'stop_loss'`）折算 TP1 的距离：

| TP1 距离 | 笔数 | 占比 |
|---|---|---|
| 恰 `1.5R`（被下限兜底价钉住） | 83 | 38% |
| `≥2.4R` | 7 | 3% |
| 其余（1.5R 与 2.4R 之间/结构位决定的更近值） | 128 | 59% |

→ **38% 的回踩单，止盈一是被"下限"钉出来的 1.5R，不是结构位**。这批单的
止盈选择权实际上在程序手里，不在 AI 手里。

### 20.3 实测：更远的止盈一是否更优

以初始止损为准、入场后 96 根 1h K 内谁先到（同根双触按止损在先，保守）：

| 止盈档 | 先到 | 先被止损 | 都没到 | 单目标 EV* |
|---|---|---|---|---|
| 1.5R | 104（47.7%） | 108（49.5%） | 6 | **+0.221R** |
| 2.0R | 91（41.7%） | 119（54.6%） | 8 | **+0.288R** |
| 2.5R | 83（38.1%） | 126（57.8%） | 9 | **+0.375R** |
| 3.0R | 72（33.0%） | 132（60.6%） | 14 | **+0.384R** |

\* EV = 命中率×止盈 − 止损率×1R，"都没到"记 0（实际会被移动止损/超时平掉）。

MFE（最大有利幅度）分位：p25 0.17R / p50 1.04R / p75 3.25R / p90 6.98R；
`MFE<0.2R` 的 58/218 笔（26%）属"入场即逆行"，任何止盈档都救不回来。

**方法论教训**：第一次跑同样的模拟用了 `trade_records.stop_loss`（TP1 成交后
会被移损改写），得出"89% 先被止损"的荒谬结论——**做前向模拟必须用
`ai_snapshot->>'stop_loss'` 里的初始止损**，否则是在用"事后已经被移动过的止损"
回测"事前该不该挂 1.5R"。

### 20.4 已实现：解耦（行为零变化）

| 文件 | 变更 |
|---|---|
| `backend/app/config.py:455` | 新增 `STOP_ANCHOR_MAX_ATR: float = 1.33`（注释写清耦合风险与宽止损证据） |
| `backend/app/services/ai_analyzer.py:488` | `_RULE_SL` 的 `far_atr` 改用 `settings.STOP_ANCHOR_MAX_ATR` |
| 同上（止盈规则） | `compose_rule_tp(...).format(far_atr=TP1_MAX_ATR/_rr)` **保持不变**——它描述的是止盈上限 |
| `_RULE_SL` 文案 | 由"（=止盈一上限÷盈亏比…）"改为"（止损锚分档阈值，与止盈上限独立；超过即视为更大级别）" |
| `backend/tests/test_stop_anchor_decouple.py` | 新增 4 条：默认 1.33；`TP1_MAX_ATR=3.0` 时止损锚仍渲染 1.33；止盈侧仍随上限变成 `max(3×ATR, 1.5×止损距离)`；显式改 `STOP_ANCHOR_MAX_ATR=2.0` 立即反映 |

默认值 1.33 与旧行为完全一致，**上线后不改变任何决策**；它只是把"放宽止盈"
从"会顺带放宽止损"里解放出来。全量测试 **1307 passed**。

### 20.5 待拍板：`TP1_MAX_ATR` 是否 2.0 → 3.0

| 口径 | 现状 2.0 | 提到 3.0 |
|---|---|---|
| 止盈一上限 | `max(2×ATR, 1.5×止损距离)`；典型止损 1.33ATR 时等于 1.5R（被钉） | `max(3×ATR, 1.5×止损距离)`；典型止损时上限 2.25R |
| 被钉住的单 | 83/218（38%） | 预计降到少数 |
| 单目标 EV | +0.221R | 约 +0.38R（按 3R 档外推，实际落在 2~2.25R 之间，介于 +0.29~+0.38R） |
| 风险 | 半仓在 1.5R 落袋的频率更高（47.7%） | 落袋频率降到 ~40%，单笔利润更依赖移动止损跟住 |

保留意见（**这条很重要**）：上表是"单目标"模型，而系统实际是"TP1 半仓 + 余仓移动止损"。
TP1 设在 1.5R 的本意不是最大化期望，而是**先落袋一半、把剩余仓位的风险降到 0**
（`breakeven_sl` 10 笔、`trail_sl` 22 笔就是这条逻辑在起作用）。单目标模型天然
低估"落袋"的价值，所以上表只能当"上限松松看"的参考，不能当"必须改"的证据。

### 20.6 拍板结果（2026-10-28）

**用户选 A：`TP1_MAX_ATR` 维持 2.0，不上调。**

理由归档：单目标 EV 表虽是 +0.22R → +0.29~0.38R 的正向，但系统实际结构是
「TP1 半仓落袋 + 余仓移动止损」，1.5R 落袋的价值（`breakeven_sl` 10 笔、
`trail_sl` 22 笔）不被单目标模型计入；把止盈推远等于用"更高的名义期望"换
"更少的确定落袋"，在样本仅 218 笔、且 D1 刚揭示 6.3% 真实触达率的当下不值得动。

因此本项**只保留解耦**（`STOP_ANCHOR_MAX_ATR`），不改任何数值。若将来要动，
前提是先有 7 天以上、含 `tp1_trail`/`trail_sl` 分档 R 分布的验收数据。

回退：解耦本身是纯重构，`STOP_ANCHOR_MAX_ATR` 默认 1.33 与旧值等价，无需回退。

## 18.5 D4：飞书卡片补「入场位置 pos」（2026-10-28）

### 18.5.1 为什么非补不可

§14.3 的 ELSAUSDT 时间线暴露了一个闭环缺口：双网自动交易全关时，**每一笔都是手动单**，
而手动单走 `force_market=True`，**按设计跳过 `entry_pos` 闸**（日志原文：
`入场位置 pos=0.31 < 0.60，手动开单按用户意图放行（entry_pos 闸）`）。

也就是说 B1 上线后，实际拦到的单是 **0 笔**——闸门只在自动开仓路径生效。
用户在飞书卡片上点「开单」时，看到的只有方向/评分/盈亏比/三价，**不知道这一单
如果走自动开仓是会被拦下的**。这不是 bug，但会让 B1 的数据彻底失真：将来复盘
"pos 闸有没有用"时，手里的样本全是"用户手动决定开的单"。

### 18.5.2 做法

| 位置 | 变更 |
|---|---|
| `backend/app/services/trade_engine.py` 新增 `_live_off_pos(a, cfg)` | 拉 60 根当周期 K 线 → `closed_bars()` 去掉未收盘根 → `bar_position()`；返回 `(pos, pos_min)`，`pos_min` 取 `settings.ENTRY_POS_MIN_RATIO`；任何异常返回 `(None, 0.0)` 且只 warning |
| `_live_off_card(...)` 新增参数 `pos: Optional[float] = None, pos_min: float = 0.0` | 在价格四列之后插一行：低于门槛时 `<font color='red'>入场位置 **pos 0.31**（低于自动开仓门槛 0.60，**自动开仓会被拦下**；手动开单不会被拦）</font>`；不低于门槛时只报数值与门槛；`pos is None` 时整行省略 |
| `notify_live_auto_open_off` | 调 `_live_off_pos` 并透传给卡片 |

不加这行的代价：用户在不知情下开了一笔系统本不认的单；加了之后，卡片本身就是
「这一单的自动开仓判决书」。

### 18.5.3 测试与验证

`backend/tests/test_off_card_pos.py` 6 条：低于门槛显示红色行、≥门槛显示灰行、
`pos=None` 整行省略、闸门关闭（`pos_min=0`）只报数值不给结论、`_live_off_pos`
在 K 线拉取异常时返回 `(None, pos_min)`、已收盘根过滤正确（未收盘根 88 的价格
不应参与 `bar_position`）、`pos` 取四位小数。

全量 `pytest tests/` **1313 passed**（1307 + 6）。

### 18.5.4 回退

纯展示层加法，不参与任何决策：`_live_off_pos` 返回 `None` 即回到旧卡片形态；
整批回退走 `scripts/release.sh rollback`（镜像回上一版）。

## 21. D6：每日归因快照（2026-10-28）

### 21.1 为什么做这个

这一整轮的每条结论（4h 门砍没砍利润、pos 闸有没有用、质量分下限该不该开、
止损锚与止盈上限的耦合）都是**临时写脚本从生产库现算**的：脚本散在 `/tmp`、
口径各写一遍、彼此还打架。最典型的一次是把"延迟成交"用 `opened_at` 分钟数
代理，得出"延迟单 5.9% 胜率"的结论，后来用 `limit_order_id` 一核就翻案了。

结论不可复算 = 结论不可信。所以每天固定一个时点，用**同一套口径**算一遍、
**原样落库**，把"当时的数字"冻住。

### 21.2 快照里有什么

`backend/app/services/attribution.py::build_snapshot(db, day)`（UTC 日窗口
`[当天 00:00, 次日 00:00)`）返回：

| 块 | 内容 |
|---|---|
| `funnel` | 当日扫描轮数、币池、扫描数、errors，以及 `funnel_json.stages` / `htf_states` 的**累加**（关卡分布就是漏斗真相） |
| `candidates` | 命中条数、按信号族、按触点、均质量分 |
| `ai` | AI 分析条数与 `suggest/skip` 分布 |
| `trades` | **按 `closed_at` 归入当天**（当日结算口径，与"按开仓日归因"不是同一件事——同一批单在两种口径下会落到不同日期）的已平仓（正式网、`pnl_pct` 非空、`risk_amount>1`）总量/胜率/ΣR、按信号族/触点/出场原因/方向，以及 `stop_atr`/`pos`/`room_r`/`entry_path` 四个分档；附当日最差五笔 |
| `blocked_probe` | 被 4h 门拦下的候选里，真实触点数、同向金K数、两者同时成立数（D1 落库的 `probe` 字段），即"4h 门的真实代价" |
| `logs` | 当日 `app_logs` 各级别条数（异常面） |

分档口径写死在模块顶部常量（`STOP_ATR_BUCKETS` / `POS_BUCKETS` / `ROOM_BUCKETS`），
并与 `tests/test_attribution.py` 的断言绑定——**改口径必须同时改文档与测试**。

### 21.3 落库与推送

- 任务：`app/tasks/attribution_tasks.py::run_daily_attribution_task`，`task_type="review"`、
  `task_name="每日归因快照"`，`detail` = 整份快照 JSON（`task_records` 已有 `detail JSON`
  列，**无需迁移**），`summary` = `2026-10-27：平仓 21 笔 ΣR -9.95 胜率 19.0%`
- 调度：`celery_app.beat_schedule["daily-attribution"]`，**每日 08:10 北京**
  （错开 08:00 shadow-evaluate、08:05 daily-report、08:08 spot-daily-check）
- 飞书：`format_card(snap)` 一张卡，头部颜色按 ΣR 红/绿/灰，正文六行以内
  （平仓与 ΣR、漏斗一行、被拦候选真实触达率、最差三笔、ERROR 数）
- 补跑：`run_daily_attribution_task(day="2026-10-26")`

### 21.4 设计约束（为什么这么写）

1. **只读**：业务表一行不写，唯一写入是 `task_records` 那一行；
2. **分档在 Python**：不写 `CASE WHEN` 瀑布，口径才能被单测固定；
3. **单块失败不拖垮整份**：`funnel/candidates/ai/trades/blocked_probe/logs` 各自
   `try/except`，失败置 `None`——半份快照比没有快照有用；
4. **不参与决策**：快照只统计，不写任何开关、不发任何交易指令。

### 21.5 测试

`backend/tests/test_attribution.py` 9 条：分档左闭右开边界（`1.0` 落在
`1-1.5ATR`、`0.6` 落在 `pos≥0.6`）、`summarize_r` 的 None 剔除与"0 不算赢"、
`group_by` 跳过 None 键、UTC 半开窗口、卡片红/绿/灰三态与缺块容错。

全量 `pytest tests/` **1322 passed**（1313 + 9）。

### 21.6 回退

`celery_app.beat_schedule` 删掉 `daily-attribution` 一项即停止（改配置需重建
celery_beat，`IMAGE_TAG=<sha> docker compose up -d --no-build --force-recreate celery_beat`）；
已落库的历史快照留在 `task_records` 里不受影响，不删数据。

---

## 22. 挂起清单（2026-10-28）

**为什么集中挂起**：这四项（D2/D5/D7/D8）都不是"没想到怎么做"，而是"证据只够
指出方向、不够决定参数"。前四轮里已经踩过一次同类坑——用 `opened_at` 分钟数
代理"延迟成交"，结论是"延迟单 5.9% 胜率"，换成 `limit_order_id` 一核就翻案了。
所以这四项统一等 **D6 每日归因快照**（§21，每日 08:10 北京自动落库）攒够
连续样本再动，而不是再用一次性脚本估。

**挂起 ≠ 否决**：四项都保留了完整方案，恢复时不需要重新调研。

| 编号 | 挂起项 | 会改什么 | 现在缺什么 | 恢复条件（可判定的） | 风险 |
|---|---|---|---|---|---|
| **D2**（P2-1） | 摆动 band 改 ATR 归一 | `STRUCTURE_TOUCH_BAND_PCT`（0.3%）

→ ATR 倍数（约 0.15–0.25×ATR） | 现在的 0.3% 是**绝对百分比**，高波动币几乎必然入带、低波动币可能永不入带；但改成 ATR 倍数后整条信号流的数量会结构性变化，需要"改动前 band 分档 × 结果"的基线 | D6 快照里 `trades.buckets` 已含 `stop_atr`/`room_r`，还缺 **band 触及深度** 分档；补齐后要求：`band 触及深度` 与 R 之间出现**单调关系**才动手 | 弄反会让信号量骤增或骤减，且两种都会污染 D1 的 probe 基线 |
| **D5**（P1-2 替代） | AI 配额排序键改「触点优先级 → 质量分」 | `ai_tasks.run_ai_analysis_task` 的 `order_by` | 原方案（按池分配额）因 A1 下线突破类而失效；替代方案 C 的证据只有"触点期望差异"（retest +0.323R / swing +0.226R / ema21 +0.184R），而**质量分与结果的相关性更强**（80 分水岭），换排序键是拿强信号换弱信号 | D6 快照给出"同日同配额下，触点优先级 vs 质量分谁更能挑出赢单"的对照（至少 5 天）；判据：按触点优先排序的那批，日 ΣR 连续 3 天 ≥ 按质量分排序 | 排序键是配额分配的唯一杠杆，改错会让 AI 预算流向错误的一类信号 |
| **D7** | 同向簇风险约束 | 新增：同一 UTC 小时内同方向多单的合计风险上限（如 ≤2×单笔风险） | 证据两向都有：10-25 02h 五笔同向 **+7.6R**（当时该砍吗？不砍才对）；10-26 07h 八笔同向、13h 七笔同向，簇内多为亏损。**目前无法区分"行情对"与"行情错"**——这正是簇约束要解决的问题，但没有前置判据就是拍脑袋 | D6 快照按小时聚合出"同向簇规模 × 簇内 ΣR"的分布；判据：出现"簇规模 ≥4 时簇内 ΣR 显著为负"且样本 ≥15 簇 | 会直接砍掉盈利日的连续单（10-25 是主线收益来源），代价极高 |
| **D8** | `STATE_ALIGNED` 加 `bull_dip` | `htf_context` 的状态白名单（1 行） | §15 回放给出 `bull_dip` 顺侧 383 条 **+0.117R**、逆侧 99 条 **−0.534R**，但"顺侧/逆侧"与 `_STATE_DIR["bull_dip"]=-1` 的方向语义还没理顺——现在放行有可能放的是逆侧那 99 条 | 用 D1 的 `probe` 数据（已含 `votes_net` 与 `touch_ref`）统计 `bull_dip` 下真实信号的**实际下单方向 × 结果**，样本 ≥30 笔；判据：该方向 ΣR > +0.1R/笔 | 4h 状态锁是第 2 大关卡（14 天拦 22,672 条），白名单一旦放宽就难收回 |

### 22.1 每项的"如果永远不恢复"

- **D2 不恢复**：band 保持绝对百分比。代价是高波动币的摆动触点偏松，可接受（swing 触点整体 +0.226R 仍是正的）。
- **D5 不恢复**：配额继续按质量分全局排序。代价是部分高期望触点被挤出，但 P0-1（质量分下限 80）已先把最差的那批挡在 AI 之前。
- **D7 不恢复**：不设簇上限。代价是坏日子里同向批量同时被扫（10-26 那两簇），收益是好吃到的连续单不会被砍（10-25）。
- **D8 不恢复**：`bull_dip` 继续整批走 `htf_state_lock`。代价是放弃 +0.117R/笔的那批，但避免了放行 −0.534R/笔那批。

### 22.2 恢复时的固定动作

1. 先看 D6 快照里对应判据是否成立（每条都写了"可判定的恢复条件"）；
2. 成立才改代码，改完必须先跑 `pytest tests/`；
3. 上线走常规 CI，但**在 `docs/16` 里补一节记录改动前后的分档数据**；
4. 回退路径：
   - D2 → `STRUCTURE_TOUCH_BAND_PCT` 改回 0.3
   - D5 → `order_by` 改回质量分
   - D7 → 上限键删掉/置 0
   - D8 → 从 `STATE_ALIGNED` 删除 `bull_dip`
   四项都只需重建容器，分钟级。
