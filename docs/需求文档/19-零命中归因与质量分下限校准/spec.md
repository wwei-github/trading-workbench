# 零命中归因与质量分下限校准（2026-10-28）

> 对象：一轮扫描「518 币池 → 0 命中」的完整归因。
> 触发场景：用户在「待确认池 · 最新筛选」看到最近一轮 `0 出候选 / 0 命中`，问「原因是什么、是否正常、有没有逻辑 bug」。
> 相关：[16-筛选链路审计与优化](../16-筛选链路审计与优化/spec.md)（19 关口径、`quality_floor` 立项依据 §5.1、开关记录 §17）、[18-最新筛选展示](../18-最新筛选展示/spec.md)（漏斗 Tab）。
> 代码基线：`be69d31`（`docs(16) §23.5`）。量级复核：2026-10-28 生产库 9 轮 + 单族/交叉表（见 §1）。

---

## 0. 结论摘要

**这一轮不是「筛不到」，是「筛到了 2 个、被质量分下限全部砍掉」；而这条下限在当前行情下等价于「把系统整体关掉」。**

| 结论 | 证据 |
|---|---|
| 0 命中的**直接原因**是 `quality_floor=80`（2026-10-27 20:00 开启），本轮 2 个原始候选全部 <80 | `funnel_json.stages.quality_floor = 2`，`candidates = 0` |
| 0 命中的**结构性原因**是三者叠加：绝对阈值 80 ＋ 回踩基础分上限就是 80 ＋ tangle 期 4h 不给分 | 4h 给 +10 的子集里 base 50 过线率 7.2%、base 80 为 97.5%；`tangle` 时 `dir=0` 根本不进 htf 分支，同等 base 直接少 10 分 |
| 叠加震荡期两项折价（`range −8`、`r2_low −5`）后，震荡期「三票全同向」的满分候选也只有 67 分，需再拿 13 分加分才能过线 | `technical.py:1484-1505` |
| **不是**「10-21 口径变更导致刻度漂移」：回踩基础分从 `net` 改 `same_n` 是**抬高**（`same_n ≥ net`），代码注释原文即为「与原刻度一致」 | `technical.py:1393-1402` |
| A1 下线突破族后得分上尾整体消失，单族基准应从 **48.6%** 起算，不是 14 天混合口的 29.4% | 14 天均值 pullback 83.8 / breakout 88.8 / trend_breakout 93.9；<80 占比 35.4% / 15.2% / 4.0% |
| floor 实测砍 **83.3%**（9 轮、raw 96 条），远高于单族基准 48.6% | 砍 80 / 过 floor 16 / 落库 8（差额 8 疑为重复抑制） |
| 0 命中的前置压力也真实存在：290 进管线，4h 门 + 拐头锁 + 震荡共砍 227，只剩 63 进触发层 | `gate_4h 126 / htf_state_lock 82 / range_score 19` |
| **计数逻辑没有错**，漏斗四项自洽（见 §2.2） | 504 拦截 + 14 进池 + 228 池过滤 = 518 |
| 单轮 0 命中在统计上正常；「连续多轮接近 0」不正常，且**拦截率半条**已触发 `16 spec §17` 的停手条件（成交数那半条未取） | 实测 83.3% ≫ 70%；单日成交 <2 笔未核 |

**待修三处、待观测一处**：质量分下限的阈值与归因口径、待确认池的口径、漏斗缺 `raw_candidates`/`suppressed`/`pending_active` 观测；pending 续命机制先观测、暂不改（缺前置数据）。

---

## 1. 数据来源与口径

| 项 | 值 |
|---|---|
| 单轮明细 | 2026-10-28 **02:00 UTC**（= 北京 10:00），`scan_type=scheduled` |
| 单轮数据取自 | `frontend/public/funnel-demo.html` 内嵌的真实一轮 `funnel_json`（commit `97bbc66` 落盘，为「最新筛选」示例页取数） |
| 量级复核 | 2026-10-28 生产库：floor 上线后 **9 轮**（raw 96 / 砍 80 / 过 floor 16 / 落库 8）；单族基准（10-21 起、floor 前、**n=1508**）；base×htf 交叉表（**n=1463**） |
| 字段口径 | `universe / scanned / candidates / pending_pool / stages`，与 `scanner.py::run` 组装一致 |
| 未使用的口径 | 不涉及 `hit_count` 列（该列在抑制后才写），本文件所有"命中"均指 `candidates`（落库条数） |

**边界声明**：单轮逐关数值以 `funnel-demo.html` 内嵌轮次为准；量级复核数来自生产库实测，二者若轮次不同不可逐关对齐，但**归因路径与结论口径不变**——下面每一关的判定逻辑都与轮次无关。取数时须锁定同一 `scan_record_id`。

---

## 2. 逐层拆解

### 2.1 漏斗全量

| 关卡 | 数量 | 关卡性质 |
|---|---|---|
| 全市场 USDT 永续 | 518 | — |
| 成交额 < 300 万（`liquidity`） | 218 | 池过滤 |
| 黑名单（`blacklist`） | 8 | 池过滤 |
| 新币冷静期（`new_coin`） | 1 | 池过滤 |
| 风险档位（`risk_tier`） | 1 | 池过滤 |
| **进指标管线** | **290** | — |
| 4h 背景门（`gate_4h`，缠绕 83 / 走平 28 / 其他 15） | 126 | 前置否决 |
| 拐头锁（`htf_state_lock`，非排列态） | 82 | 前置否决 |
| 震荡计分（`range_score`） | 19 | 回踩只扣 8 分，此处为突破路径残留 |
| 无触点（`no_touch`） | 11 | 触发层 |
| 腿门（`leg_gate`） | 8 | 触发层 |
| 待确认等待中（`pending_waiting`） | 26 | 触发层（仍在池内，不算被拒） |
| 确认窗过期（`window_expired`） | 2 | 触发层终态 |
| 本轮进待确认池（`pending_pool`） | 14 | **本轮 upsert（新建或刷新）条数**，不是池内活跃总数 |
| **产生候选** | **2** | 回踩确认成立 |
| 质量分 < 80（`quality_floor`） | **2** | 候选池准入 |
| **最终命中** | **0** | — |

### 2.2 自洽校验（这一步排除了"计数写错"）

```
池过滤：218 + 1 + 1 + 8 = 228           518 − 228 = 290  ✓ scanned
管线内：126 + 82 + 19 + 8 + 11 + 2 + 2 + 26 = 276
        290 − 276 = 14                   ✓ = pending_pool（进池不算被拒）
总拦截：228 + 276 = 504                  ✓ 与「最新筛选」吸顶条一致
过前置门：290 − 126 − 82 − 19 = 63
        63 = 11 + 8 + 26 + 2 + 2 + 14    ✓ 全部去向可解释
```

链条末端：**63 个币里只有 2 个走完触发**（`11 + 8 + 26 + 2 + 14 = 61` 分流掉了），这 2 个又都被 `quality_floor` 砍掉。

### 2.3 为什么只有 2 个走完触发

不是某一关突然失效，而是三种状态并列：

1. **26 个在等确认**（`pending_waiting`）：触点已命中、金K未出现，窗口还剩 1~3 根。这是活着的，会在后续 1~3 轮里转成 confirm 或 window_expired。
2. **14 个本轮进池/刷新**（`pending_pool`）：本轮触碰触点、当根无金K，等待下一根。
3. **11 个压根没碰到任何触点**（`no_touch`）+ **8 个碰到但前置反向腿不够深**（`leg_gate`）。

即：**40 个活跃 pending（26 等待 + 14 新建/刷新）对应 2 个当根确认**，这个比例与历史「pending 确认率 7.4%」完全一致（`16 spec §2 L4-3`：expired 5,458 / superseded 1,992 / confirmed 602）。

---

## 3. 0 命中：正常还是异常

### 3.1 单轮 0 是常态

- 全链路命中率 1.8%（3,330 / 174,000 币次，14 天口径）；
- 北京时间 19:00–02:00 时段「常为 0–1」（`18 spec §6.5`）；
- 现在 `allowed_triggers` 只剩 `trigger_pullback_confirm`（突破类 2026-10-27 A1 下线），四条触发路径只剩一条。

### 3.2 但"被质量分砍到 0"是新的、非预期现象

`quality_floor=80` 于 **2026-10-27 20:00 UTC** 开启（`16 spec §17`）。开启后 9 轮实测（生产库复核）：

| 口径 | 原始候选 | 过 floor | 落库 | 拦截率 |
|---|---|---|---|---|
| floor 上线后 9 轮 | 96 | 16 | 8 | **83.3%** |
| 单族基准（10-21 起、floor 前、n=1508，仅回踩） | 1,508 | — | — | **48.6%** |

真实差距是 **48.6% → 83.3%（约 1.7 倍）**。9 轮里 16 条过 floor、只有 8 条落库，差额 8 条疑为重复抑制（待 `suppressed` 落盘确认，见 §4.3）。原文用的 14 天混合口径「应只砍 29.4%」不能作为对照：A1 下线突破族后，得分分布的上半截整体消失（14 天实测均值 `breakout 88.8`、`trend_breakout 93.9`、`pullback 83.8`；<80 占比分别为 15.2% / 4.0% / **35.4%**），混合分布只适用「突破还在跑」的旧样本。

**判定**：单轮 0 命中正常；但 floor 的实测杀伤（83.3%）已明显偏离单族基准，且 `16 spec §17` 的停手条件写的是「**单日成交 <2 笔 且** `quality_floor` 拦截 >70% 候选 → 降回 70 或 0」。当前只验了后半条（拦截率），**单日成交数未取**，因此本文只表述为「拦截率已触发」，不写成「已命中停手条件」。

---

## 4. 需要修的逻辑/口径问题

### 4.1 `quality_floor=80` 在当前刻度下等价于「震荡期整体停盘」（最关键）

**代码事实一：回踩基础分上限就是 80**（`technical.py:1393-1402`）：

```python
if trigger_type == "trigger_pullback_confirm":
    same_n = (votes["bull_n"] if direction == 1 else votes["bear_n"]) if votes else 0
    score = 50 + same_n * 10          # 0/1/2/3 → 50/60/70/80，与原刻度一致
else:
    score = 50 + direction * (votes["net"] if votes else 0) * 10
```

`same_n` 最多 3 张（MACD / 布林带 / RSI）。80 分等价于「1h 三票全同向且零扣分」的满分线。

**代码事实二：10-21 的口径变更不是原因，方向是反的。** 对回踩而言 `same_n ≥ net`（多 2 空 1：旧口径 `net=1`→60，新口径 `same_n=2`→70），新刻度对深回撤是**抬高**，不会让分数更低。把裁掉变多归给这次口径变更，与代码语义相反。

**真实机制有三层**：

1. **A1 下线突破族后，上尾整体消失。** 回踩单族 <80 占 35.4%，而突破 / 趋势突破只有 15.2% / 4.0%；只剩回踩时，同一阈值天然砍得更多。
2. **`tangle` 期 4h 不给分。** `htf` 分支要求 `dir != 0`（`technical.py:1404-1414`），`tangle` 时 `dir=0`，**根本不进分值矩阵**——不是扣分，是拿不到同向排列的 +10 / 宏观交叉的 +12。同等 base 的候选在 tangle 期直接少 10~12 分。
3. **震荡期两项折价与 tangle 共线。** `range −8`（`range_score` 超阈，`technical.py:1487-1490`）与 `r2_low −5`（24 根 1h R²<0.4，`technical.py:1492-1505`）都只在震荡行情触发，与 tangle 高度同时出现。三项叠加的相对拖累最多约 23 分。

**交叉表（10-21 起、floor 前、n=1463，4h 给 +10 的子集）**：

| base | htf | n | 过 80 |
|---|---|---|---|
| 50 | +10 | 557 | 40（**7.2%**） |
| 60 | +10 | 236 | 110（46.6%） |
| 70 | +10 | 386 | 334（86.5%） |
| 80 | +10 | 284 | 277（**97.5%**） |

要过 80，基本得「base≥70 **且** 4h 给 +10」。而 tangle 期 htf=0：base 70 → 70 分，需再拿 10 分加分；base 60 → 60 分，需 20 分；只有 base 80 能裸分过线。若同期再吃 `range −8`、`r2_low −5`，满分候选也只有 67 分，需 13 分加分。

**结论**：`floor=80` 的实际语义不是「要求高质量」，而是「**要求 4h 明确方向 + 至少两票同向**」，即在 tangle 期把系统整体关掉。复核显示近三日 4h 长期处于 `tangle`，这正是「连续多轮接近 0」的机制来源。

**加分/扣分全表**（补上原文漏掉的一项）：

| 项 | 分值 | 代码 |
|---|---|---|
| 4h 同向排列（`aligned_*`） | +10 / 宏观交叉 +12 | `technical.STATE_QUALITY` |
| 放量（量比 ≥1.2） | +5 | `compute_quality_score` volume |
| 金K价格带结构点密度 | +3×n，封顶 +12 | zone |
| 回踩企稳 | +2 ~ +5 | `pullback_stabilize_bonus` |
| **震荡段折价（`range_score` 超阈）** | **−8** | `range_penalty` |
| **趋势质量折价（24 根 R²<0.4）** | **−5** | `r2_low`（2026-10-27 新增） |
| **1H 结构门未过** | **−15** | `STRUCTURE_GATE_PENALTY` |
| RSI 追高 / OBV 背离 | −8 / −6 | rsi / obv |

典型组合：`base 70（2 票同向）+ htf 10 = 80` 刚刚过线；在 tangle 期同一候选是 70，再吃一次震荡 −8 就变 62，被砍。TAUSDT（`16 spec §23`）正是 `range=−8` 的样本。

**这不是算错，是"绝对阈值被用在了随行情整体平移的刻度上"**——属于决策依据失效，不是代码 bug。

### 4.2 「待确认池 14」与池子真实状态不一致

**代码事实**（`scanner.py:1138-1139`）：

```python
pending_pool_n = sum(1 for op in pending_ops.values() if op.get("op") == "upsert")
```

`pending_pool` 语义是**本轮 upsert 条数（含同向触碰刷新）**，不是池内活跃总数。同一轮：

- 吸顶条 KPI「待确认池 **14**」（`LatestFunnelPanel.tsx:251` 直接渲染 `pending_pool`）
- 阶段柱「待确认 **28**」（`pending_waiting 26 + window_expired 2 + pending_flip 0`；原文写 30，与自身括号加法不符）
- 实际活跃 pending ≈ **40**（14 新建/刷新 + 26 等待中）；生产库复核时真实活跃为 **38**，而每轮 KPI 在 22–52 之间波动

三个数同屏并存，用户会读成"池子里只有 14 个"，而真实池子是 38~40 个。

同一处还有一个语义问题：**26 个「等待中」被计入「共 504 次拦截」**（`scanner.py:622-623` 把它们写进 `funnel_rejects`，前端 `LatestFunnelPanel.tsx:40-53` 的「待确认」组再并入总拦截），但它们并没有被拒——它们还在池里等待确认。这会让"拦截总数"虚高，也让"0 命中"看起来比实际更"堵"。

### 4.3 pending 同向触碰"续命"，且漏斗看不见被抑制的命中

**续命**（`scanner.py:1627-1634`）：

```python
if row:
    # 同方向触碰刷新：锚点换新触碰根，等待窗口重新计时
    row.stage1_candle_open = op["stage1_candle_open"]
```

窗口按 `stage1_candle_open` 计算（`trigger.py:931-947`），而每次触碰都会把它换成当根（`trigger.py:1241-1247`）。每根贴位触碰都会重置窗口，`pending_confirm_window_bars` 重新计时。贴着均线横盘震荡的币可以**永久留在待确认池**，既不过期也不确认；连"孤儿清扫"也因为这个锚点被刷新而绕开（`scanner.py:1566-1595`）。这是"26 个长期等待"的机制来源，也是历史确认率只有 7.4% 的一部分原因。

叠加 `quality_floor` 后还有一层浪费：**pending 建立时不看 floor**（有意设计，`apply_quality_floor` 只作用于 `hits`），但确认后立刻又会被 floor 砍——等于白等 1~4 根。注意这不必然发生：确认根的结构点密度、影线、放量等加分可补 10~30 分，只有低 base 且无加分的 pending 才是真"白等"。

**观测缺口**（`scanner.py:1143-1165`）：`funnel_json` 原先只存 `stages / rejects / details`，**不存 `raw_candidates`（floor 之前的候选名单与分数）、`suppressed`（重复命中抑制数，原只进日志 `scanner.py:1167-1172`）和 `pending_active`（本轮币池内活跃数）**——2026-10-28 已由 `_funnel_observability` 补齐（见 §5 #1）。缺口期间的后果：

- `candidates=0` 时无法区分「本来就没信号」「分不够」和「信号被重复抑制」；
- 复核 9 轮：raw 96 → floor 砍 80 → 落库 8。**差额 8 条（16 条过 floor − 8 条落库）疑为重复抑制**，若成立，则"通过 floor 的候选里一半被抑制"，抑制与 floor 同等重要——这一点必须靠 `suppressed` 落盘确认，不能靠猜。
- §4.1 的"差多少分"同样无法从库里精确复盘：被砍候选不落 `scan_results`，只留 `rejects` 的关卡代码，没有分数与分项。

---

## 5. 建议动作（按依赖排序，不按代价排序）

| # | 动作 | 代价 | 回退 |
|---|---|---|---|
| 1 | `funnel_json` 补 `raw_candidates`（symbol / score / parts）、`suppressed`、`pending_active`（本轮币池内） | 约 15 行 + 单测 | 删键 |
| 2 | `quality_floor` 从 **80 降到 60**（不是 70；70 在 tangle 期照样过不去） | 一条 SQL / 页面改数，不发版 | 改回 80 |
| 3 | 确认阈值形态：固定低护栏（60）+ 配额排序，**不做分位**；按触点分档仅在有样本后评估 | 决策项，无代码 | 维持 60 |
| 4 | 前端 KPI「待确认池」改「本轮进池」并增渲染 `pending_active`；`pending_waiting` 从「拦截」口径拆出 | 前端 2 处 + 依赖 #1 的新字段 | 改回 |
| 5 | pending 续命加限制（最多刷新 1 次，或再次触碰只记不重置窗口） | 约 20 行 | **暂不做**，缺 §7-2 的前置数据 |

**执行顺序**：**#1 + #2 同批发版**（同一批拿到观测与恢复）→ 观察 2 轮 `raw_candidates` → #4 → #3 → #5。

> **实施进度（2026-10-28）**：#1 已落（`_funnel_observability` + `tests/test_scan_funnel.py` 单测，51 条漏斗用例通过）；#4 已落（KPI 改「本轮进池」「池内活跃」，拦截栈条拆出 `pending_waiting`，`/scans/funnels` 补 `pending_active`，`npx tsc --noEmit` 通过）；#2 待在生产库执行（本机连不到生产库，SQL 见下）；#3/#5 未动。
>
> ```sql
> UPDATE system_config
> SET scan_filters = jsonb_set(coalesce(scan_filters, '{}'::jsonb),
>                              '{quality_floor}', '60'::jsonb, true)
> WHERE scan_filters->>'quality_floor' = '80';
> ```

**#2 的预期要写实**：在 tangle + R²<0.4 同时成立时，base 70 吃两项惩罚 = 57、base 60 = 52，仍会被 60 砍掉。所以 60 的作用是「放行 base≥70 且最多吃一项惩罚」的候选，**不承诺恢复到 5–10 个/轮**。若首两轮 `raw_candidates` 显示过 60 的仍 <3 条/轮，再下探 55 或 0。

**#3 的理由**：`ai_tasks.py:305-308` 已按 `quality_score desc` 取前 `ai_max_per_scan`（默认 10），正常供给下绝对下限与配额重复，只有在供不应求时才成为唯一硬闸；`TRADING_MIN_RECOMMENDATION=60`（`config.py:533`）是执行层另一道独立闸。最终形态应是「取消 80 这类高位绝对下限 + 保留低护栏 + 配额排序」，而不是再引入一套分位逻辑。

---

## 6. 复现 SQL

```sql
-- 1) 本轮漏斗（含 quality_floor 拦截数与被砍币种）
select s.id, s.created_at, s.hit_count,
       (s.funnel_json->>'universe')::int  as universe,
       (s.funnel_json->>'scanned')::int   as scanned,
       (s.funnel_json->>'candidates')::int as candidates,
       (s.funnel_json->>'pending_pool')::int as pending_pool,
       s.funnel_json->'stages' as stages
from scan_records s
where s.status = 'completed'
order by s.created_at desc limit 12;

-- 2) 分族质量分分布：A1 下线后单族基准（关键对照，别再用 14 天混合口径）
select signal_type,
       count(*) as n,
       round(avg(quality_score)::numeric, 1) as avg_score,
       round(100.0 * count(*) filter (where quality_score < 80) / count(*), 1) as lt80_pct
from scan_results
where created_at >= '2026-10-21'
group by 1 order by 1;

-- 2bis) base × htf 交叉（floor 前）：验证「要过 80 基本得 base≥70 且 4h 给分」
select coalesce(quality_parts->>'base','?') as base,
       coalesce(quality_parts->>'htf','0')  as htf,
       count(*) as n,
       round(100.0 * count(*) filter (where quality_score >= 80) / count(*), 1) as pass80_pct
from scan_results
where created_at >= '2026-10-21'
  and signal_type = 'trigger_pullback_confirm'
group by 1, 2 order by 1, 2;

-- 3) floor 上线后的拦截率（半分位口径，按轮聚合）
select s.created_at,
       (s.funnel_json->'stages'->>'quality_floor')::int as cut,
       (s.funnel_json->>'candidates')::int as passed,
       (s.funnel_json->>'raw_candidates') is not null as has_raw
from scan_records s
where s.created_at > '2026-10-27 20:00'::timestamptz
order by s.created_at desc;

-- 4) 活跃 pending 真实总量（对比 pending_pool KPI）
select status, count(*) from pullback_pendings group by 1;

-- 5) 长期滞留占比（#5 的前置代理指标；刷新次数未落库，只能用存活时长近似）
select count(*) filter (where updated_at - created_at > interval '4 hours') as stale_gt4h,
       count(*) as active_total
from pullback_pendings where status = 'pending';
```

> **注意**：`apply_quality_floor` 在 `_save_results` **之前**执行，被砍的候选**不落 `scan_results`**，只留在 `funnel_json.rejects/details`，且不带分数。故 #1 上线前，§4.1 的"差多少分""差额是否被抑制"都无法从库里复盘——这正是 #1 排在第一位的理由。

---

## 7. 待确认

1. **9 轮里 raw 96 − 砍 80 − 落库 8 = 8 条差额**：是否全部为重复命中抑制？`suppressed` 落盘后即可判定；若成立，抑制需单独立项（它和 floor 一样在吃候选）。
2. **26 个 `pending_waiting` 中，有多少是"续命"超过 2 次的**：刷新次数未落库，只能用 `pullback_pendings.updated_at - created_at` 的存活时长做代理（SQL 5）。没有这个数就改续命限制会误杀慢确认的正常单，故 §5 第 5 项暂缓。
3. **`quality_floor` 的真实意图**：是"砍掉低质量候选"还是"只让高分进 AI"。若是后者，`ai_max_per_scan` + 质量分降序已能实现，下限反而重复且更粗暴。
4. **降到 60 后首两轮的 `raw_candidates` 分布**：决定是否继续下探 55/0，而不是预先承诺"恢复到 5–10 个/轮"。

---

## 8. 与既有文档的关系

| 文档 | 关系 |
|---|---|
| `16 spec §5.1` | 本文指出其门槛校准依据失效，但**原因不是"口径漂移"**：A1 下线突破族后上尾消失 + tangle 期 4h 不给分 + 震荡期两项折价共线；建议阈值降到 60 或按形态收敛 |
| `16 spec §1.2` | 14 天混合分布（`<80` 29.4%）不再适用；替换为单族基准 48.6%（n=1508） |
| `16 spec §17` | 本文用其实测拦截率（83.3%，9 轮）判定已触发停手条件的**拦截率半条**；单日成交数未取 |
| `16 spec §2 L4-3` | 本文的「2 / 40 确认」比例与其 7.4% 确认率同源 |
| `18 spec §6.5` | 「单轮 0 命中是常态」的出处；本文补上"被 floor 砍到 0"这一新类型 |
| `18 spec §3.1` | 本文 §2.1 的漏斗数值与其吸顶条示例同源（同一轮） |
