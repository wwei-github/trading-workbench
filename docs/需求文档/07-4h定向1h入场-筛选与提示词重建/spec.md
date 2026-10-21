# 4h 定向 / 1h 入场：筛选层与 AI 提示词重建方案

- 状态：开发中（工作区已有未提交实现；下列分阶段状态以实际代码和测试为准，策略性改动未完成回放前不建议上线）
- 范围：`scanner` 的币种筛选与排序、`ai_tasks` 的配额、`ai_analyzer` / `ai_agent` 的提示词与事实注入
- 不含：止盈止损结构、执行节拍、组合风控（另行立项）
- 方法：以代码现状为基线，按「这道门应该回答什么问题」逐层重画，不沿用其他审计文档的结论

---

## 0. 先定义系统在做什么

系统只有一个交易假设：

> **4h 决定方向与是否值得做；1h 只决定在哪一根 K 线、以什么形态进场。**

由此推出两条硬性推论，后文所有判断都以此为准：

1. **1h 的任何指标都不能否决 4h 的方向**，只能影响「这次入场的质量」；
2. **筛选层的第一职责不是「找出所有形态」，而是「把不可执行、不匹配周期、已经做过的机会排掉」**。

远程 main 的回踩方向票基础分已改为按同向票数排序，且提示词明确 4h 定向、1h 反向只作确认度；当前不应概括为所有位置仍按旧的「1h 动量优先」。待验证的重点是跨管线事实一致性、配额选择效果与执行约束事实是否充分注入。

---

## 1. 筛选层：五道门模型

把筛选拆成职责互不重叠的五道门。任何一道门缺失，后面的门都会被迫承担它不该承担的判断。

| 门 | 应回答的问题 | 失败后果 | 当前实现位置 |
|---|---|---|---|
| G0 可交易性 | 这个币现在能不能按计划手数成交？ | 信号有效但订单被拒/滑点吃掉边际 | 扫描侧按 `min_volume_24h`；执行侧另有交易所规格、可用余额、名义/数量及价差校验。扫描侧目前不做同等精度的预估 |
| G1 数据完备性 | 4h/1h 历史够不够算出可信的状态？ | fail-closed 浪费请求，或 fail-open 放行脏数据 | 新币冷静期 + 函数内部根数检查 |
| G2 周期适配性 | 这个币的波动节奏与 4h/1h 是否匹配？ | 止损过宽/过窄，RR 结构性不成立 | **缺失** |
| G3 结构有效性 | 4h 是否有方向、1h 是否有可挂单的入场？ | 核心信号质量 | `htf_context` + `detect_trigger`（有，但门是分类式） |
| G4 机会预算 | 这一轮该把分析资源给谁？ | 好机会被配额挤掉、重复做同一个币 | `quality_score` 排序 + top-N + 3h 抑制（最粗） |

初步结论：G2 适配判据缺少显式配置；G4 当前按质量分与成交额排序并有重复抑制。G0 的执行校验已存在，问题是这些依赖止损价/账号资金/交易环境的条件能否在 AI 前可靠预估，而不是执行层完全没有校验。各门是否导致损失配额或机会，需以漏斗和执行日志数据验证。

---

## 2. 币种筛选的六个具体问题

### F1｜G2 缺失：没有「周期适配」判据

**证据**：`get_usdt_swap_symbols_with_volume()` 只返回 `{symbol, volume_24h, last_price}`（[exchange_pool.py L488-L492](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/exchange_pool.py#L488-L492)）；筛选参数表里没有任何 ATR、波动率、价差键（[scan_filters.py L61-L68](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scan_filters.py#L61-L68)）。

**为什么这是问题**：同一个「aligned_bull + 回踩 EMA21」形态，在 1h ATR% = 0.4% 的币上，扣掉双边手续费和滑点后 RR 天然不成立；在 1h ATR% = 4% 的币上，止损距离会让按固定风险预算算出的手数低于交易所最小下单量。这两种币现在都会进入 AI 分析，浪费配额，并在执行层被拒。

**改法**：新增三项筛选，全部进 `scan_filters`（保持单一来源）：

| 键 | 作用门 | 口径 |
|---|---|---|
| `min_atr_pct_1h` | G2 | `ATR14(1h) / close`，低于此值无法覆盖成本 |
| `max_atr_pct_1h` | G2 | 高于此值时止损过宽、手数不可成交 |
| `max_spread_bps` | G0 | `(ask-bid)/mid × 10000`，取自 `bookTicker` |

**验证**：`funnel_json` 增 `atr_band` / `spread` 两个 `reject_stage`；对被拦样本统计「若放行的 4h 后验收益」，确认不是误杀。

---

### F2｜G0 缺「最小可成交性」：信号能生成，订单下不出去

**证据**：筛选层只按 24h 成交额过滤（[scanner.py L751-L759](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scanner.py#L751-L759)），不读 `exchangeInfo` 的 `MIN_NOTIONAL` / `LOT_SIZE` / `PRICE_FILTER`。

**为什么值得观测**：执行阶段已经按具体账号预算、止损距离、参考价格和币安交易规则复算数量/名义，并在下单前拦截不满足 `min_qty` / `min_notional` 的单子。这可能白耗一次 AI 分析，但不能统称为“交易所拒单”。扫描阶段预估还会受账户/环境、风险配置、AI 止损和执行时价格影响，不能声称预判等价于执行校验。

**改法（仅影子预估）**：利用已有合约规格缓存计算预计可行性；必须显式带上账号/测试网环境、风险预算来源、止损距离假设、数量步进取整及参考价。名义与数量的具体公式须与 `trade_engine` 同单测交叉验证。任一输入不可得时标记 `unknown`，不得拒绝。只记录影子阶段，不据此声称替代执行检查。

**验证**：与执行层实际 `open_block_reason` / 下单拒绝原因按账号、环境、币种和价格时间对账；报告误报、漏报与未知率，未定义可接受误差前不启用硬拦截。

---

### F3｜G3 的门是「状态分类」而不是「趋势质量」

**证据**：扫描前置门只放行 `STATE_ALIGNED = ("aligned_bull", "aligned_bear")`；但 `STATE_QUALITY` 对 aligned 同向背景给固定 `+10`，质量分事实与状态说明中还可计算 ADX 等指标。待开发中的分位/强度分只应作为影子项，不能在本阶段改变正式排序。

**为什么这是问题**：`aligned` 只说明「均线排列」，不说明「趋势有多强、还有多少空间」。ADX 18 的 aligned 与 ADX 40 的 aligned 在筛选层完全等价，随后由质量分 +10 再次抹平。系统实际上没有可用的 4h 强度信号，AI 只能从 K 线里自己猜。

**改法（影子特征，不进正式分）**：计算 4h 强度分并落独立 `htf_strength_shadow` 分项；不得加到 `quality_score` 或改变配额顺序，待样本外评估后再决定是否并入：

- `adx_4h` 三档：`<20 → 0`、`20~30 → +3`、`≥30 → +6`；
- `EMA144` 斜率分位（近 200 根内）：`<30% → 0`、`30~70% → +3`、`≥70% → +5`；
- 价格距 `EMA144` 的 4h ATR 倍数：`≤1.5 → +3`、`1.5~3 → 0`、`>3 → -4`（防追高，与 F6 呼应）。

**验证**：同一批历史信号重算质量分，比较强弱 aligned 的分差分布；配额命中率对比。

---

### F4｜G4 的单键排序让回踩在配额上结构性落后

**证据**：回踩基础分 `50 + same_n*10`，突破 `50 + direction*net*10`（[technical.py L1489-L1491](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/strategy/technical.py#L1489-L1491)）；配额按 `quality_score.desc(), volume_24h.desc()` 取前 N（[ai_tasks.py L156-L177](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/tasks/ai_tasks.py#L156-L177)）。

**待验证假设**：两类候选的基础分算法不同，且全局按 `quality_score` 排序；这可能造成候选选择偏差。但回踩分也叠加 4h、量能、金K结构、企稳和惩罚分，不能由基础分范围直接推出“永远排在后面”。当前没有数据足以断言是系统性歧视。

**候选方案（暂不启用）**：

- A. 按触发类型分池配额；B. 按类型分位归一化后混排。两者都会改变 AI 分析集合，应先影子计算选中集合差异。

**验证**：至少记录每轮全局排名、池内排名、两种假设方案的选中集合及实际分析结果。回踩占比只作为流量指标，不能作为质量目标；比较覆盖率、suggest 率、可执行率和按信号族分层的后续结果后再讨论正式启用。

---

### F5｜G4 的去重与反馈维度不足

**证据**：`_suppress_repeat_hits` 的键是 `(symbol, signal_type)`，窗口 3 小时（[scanner.py L1274](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scanner.py#L1274)、[scanner.py L180](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scanner.py#L180)）；排序同分时用 `volume_24h`（[ai_tasks.py L176](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/tasks/ai_tasks.py#L176)）。

**三个独立缺陷**：

1. **不看方向**：同币反向信号被吞；
2. **不看结果**：刚止损的同向信号 3 小时内也被吞，而「止损后的二次回踩」往往是更优价位；
3. **不看历史**：没有任何「该币在该形态下过往表现」的反馈，同一个币可以每周重复贡献负期望样本。

**改法**：

- 抑制键改为 `(symbol, direction, price_bucket)`，`price_bucket` 用 0.25×ATR 分桶；
- 查询最近一笔 `TradeRecord`：`sl_hit` 后同向不抑制（质量分 -5），`tp_hit` 后同向抑制窗口延长到 6h；
- 新增 `symbol_stats` 只读汇总（近 90 天该币的信号数、成交率、期望），仅作为排序的**次级键**与 AI 事实，不直接否决（避免样本不足时误杀）。

**验证**：`suppressed` 计数按方向拆分；`symbol_stats` 与最终成交率的单调性检查。

---

### F6｜防追高判据被退役，突破类没有「位置约束」

**证据**：`candle_max_ema21_dist_pct` 在参数表中标注「纯金K下线后无消费方，键保留兼容」（[scan_filters.py L67](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scan_filters.py#L67)）。当前没有任何「价格离均线/突破位过远」的上限。

**为什么这是问题**：突破单的入场质量取决于「离触发位多远」。价格已经跑了 3 个 ATR 才被扫描到，此时入场要么止损过宽，要么 RR 不足。这类信号会被执行层的偏离门打成 `blocked` 或挂 pending，但已经消耗了 AI 配额。

**改法**：新增 `max_extension_atr`（突破类专用）：`(close - breakout_level) / ATR14(1h) > max_extension_atr` 则拒绝；回踩类不适用（回踩天然贴均线）。用 ATR 归一化而不是百分比，跨币可比。

**验证**：`reject_stage="overextended"` 计数；被拦样本后续 4h 的最大有利/不利偏移统计。

---

## 3. AI 提示词：先定义「信息契约」，再谈措辞

当前提示词的问题不是「写得不够多」，而是**契约不成立**：模型被要求基于它没收到的事实做判断，同时收到的事实里有一部分是错的。

### 3.1 信息契约（模型必须收到的事实）

| # | 事实 | 用途 | 现状 |
|---|---|---|---|
| 1 | 4h 状态 + ADX + EMA144 斜率 + 距 EMA144 的 ATR 倍数 | 判断趋势强度与剩余空间 | 只有状态标签 |
| 2 | 4h 最近一段 impulse 的起点/终点、当前回撤比例、耗时根数 | 判断回踩是否「正常」 | **缺失** |
| 3 | 1h 触发类型、触点 ref、金K形态及其质量（实体比/影线） | 判断入场形态 | 部分有 |
| 4 | 量比（近 20 根中位量口径）+ 原始成交量（标注单位） | 量能判断 | **错误**（见 P1） |
| 5 | 程序算好的止损价、TP1/TP2、RR、以及该触发类型的 RR 下限 | 禁止模型自己算 | 部分有 |
| 6 | 质量分及其分项构成 | 解释排序与置信度 | **P0 缺失** |
| 7 | AI 分析阶段静态门槛；连损动态门槛按账号开仓时复核 | 避免把分发门槛和账号执行门槛混为一谈 | 静态值已注入；动态值不是批量分析的统一门槛 |
| 8 | 市场环境（BTC 状态、资金费率），缺失时显式标注 | 环境风险 | P0 预取；P1 主要依工具查询，需区分未查询与查询失败 |
| 9 | 信号年龄与当前价相对信号价的偏离 | 判断是否已经追不上 | **缺失** |

### 3.2 六个提示词问题

#### P1｜量能事实错误（确定性 Bug，最高优先级）

**证据**：两处同错——[ai_analyzer.py L628-L630](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L628-L630)、[ai_agent.py L935-L937](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_agent.py#L935-L937)：

```python
vol_desc = signal.get("volume_type") or "—"
if signal.get("volume"):
    vol_desc += f"（{float(signal['volume']):.1f}×20根均量）"
```

`signal["volume"]` 来自 `classify_volume` 的 `vol = float(klines[-2][5])`（[scanner.py L143-L154](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/scanner.py#L143-L154)），是**原始成交量**；而 `volume_ratio` 用的是近 20 根**中位量**。同一段提示词的另一处又写「≥近 20 根中位量 1.2 倍」（[ai_analyzer.py L293](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L293)）。

**影响**：模型看到形如「放量（12345678.0×20根均量）」的不可能数值，量能判断完全失真；而量能是评分 70/85 档的共振项之一。

**改法**：`signal` 增 `volume_ratio` 字段，渲染为 `量能: 缩量（量比 0.62×近20根中位量）`；原始成交量单独一行 `原始量: 12,345,678（币）`，并注明「不参与判断」。

---

#### P2｜评分门槛与执行门槛不一致

**证据**：评分提示词注入静态 `min_rec`；批量分析分发也按静态门槛预筛；连损 2/3 笔的 75/85 门槛是在每个交易账号的 `try_open_for_analysis()` 阶段复核，并受 `CONSECUTIVE_LOSS_DERATE` 开关控制。

**修正判断**：批量分析时无法把一个连损数视为所有账号的有效门槛。分析分发层取账号最大连损用于减半配额，不代表每个目标账号的动态门槛；账号集合还可能不同。把该最大值直接渲染成统一硬门槛会误导模型并可能不必要地跳过可在其他账号开仓的信号。

**改法**：提示词明确标注分析阶段静态门槛；可附加说明动态门槛按账号开仓时单独复核，不要求模型复述或按未知账号门槛 skip。若未来要把动态门槛纳入模型决策，需先定义账号扇出策略（分账号分析或账号集合中的门槛聚合规则），另行设计。

---

#### P3｜要求「结合质量分」，但事实包里没有质量分

**证据**：`_RULE_HTF_HARD` 与 `_RULE_SCORE` 多处要求结合质量分（[ai_analyzer.py L25-L28](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L25-L28)、[ai_analyzer.py L185-L188](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L185-L188)），但 P0 的用户消息不注入 `quality_score`，P1 只有 `strength`。

**改法**：两管线都注入 `quality_score` 与分项（base / 4h_state / vote / penalty / extension），并说明「质量分只用于排序与置信度，不替代你的判断」。

---

#### P4｜回踩档位规则需重放验证（不是已证实的不可达）

**证据**：评分 70 档要求「方向票同向 ≥2」，85 档要求四项共振（[ai_analyzer.py L186-L188](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L186-L188)）；而回踩规则明确说 1h 反向「属回撤常态，不得作为否决或降分理由」（[ai_analyzer.py L40-L41](file:///Users/wangwei/Documents/github/trading-workbench/backend/app/services/ai_analyzer.py#L40-L41)）。

**风险假设**：回踩 70/85 档的规则可能与其“1h 反向是回撤常态”原则发生张力，但提示词文本本身不能证明档位不可达；而连损门槛账号级、分发集合级逻辑不同，也不能推出回踩必然无法开仓。需对历史信号逐条重放档位依据、实际静态分发门槛和账号级执行结果。

**改法**：评分档位**按触发类型分表**，两管线共用同一常量：

| 档 | 突破 | 回踩 |
|---|---|---|
| 60 | 4h 同侧 + 触发有效 + 方向票同向 ≥1 | 4h 同侧 + 触点有效 + 金K确认 |
| 70 | + 量能达标 + 未过度延展 | + 回撤比例在 0.236~0.618 且量能未异常放大 |
| 85 | + 突破后首次回踩确认或结构位共振 | + 回踩后 1h 收盘收回并创段内新高/新低 |

---

#### P5｜4h 扫描门与 AI 兜底门职责不同；单一放行集合是策略变更

**基线证据**：scanner 的前置门只放行 `STATE_ALIGNED`；AI 兜底仍按 `STATE_BLOCK_BOTH`、`STATE_LOCK_CONTRA` 和对过渡态回踩的影子 A/B 分支处理。两者职责不同：扫描门决定候选进入 AI，兜底门处理手动/历史/异常输入以及 4h 重取后的约束，不能简单认定所有分支不可达。

**已接受但需单独验证的策略变更**：工作区实现拟将 AI 兜底也改为仅 `STATE_ALIGNED` 可通过，意味着所有非 aligned 状态由原来的部分放行/锁逆势转为双向 veto；而既有 `transition_pbc` 影子 A/B 只覆盖其中一个子集。此改动改变实际开仓机会，不属于提示词瘦身或事实修复。

**改法**：如保留该策略，必须先对全部非 aligned 状态 × 信号类型 × long/short 构造 veto 快照并逐组回放；4h 数据缺失、重取不一致、手动重分析路径应分别测。先以独立 `RULE_VERSION`/开关或明确的灰度方式上线，并设回滚条件。提示词的约束文案再按该变更后的行为对齐；不得用“筛选层已经拦截”代替兜底正确性测试。

---

#### P6｜P0 / P1 的事实面不一致

| 维度 | P0 | P1 |
|---|---|---|
| 市场环境 | 有 | 无（依赖工具） |
| 质量分 | 无 | 无（只有 strength） |
| K 线列 | 5 列 | 7 列 |
| 止损/止盈规则 | 共享常量 | 手工重写 |
| 缺失数据处理 | 部分 | `_RULE_BTC_ENV` 默认「未急跌」 |

**改法**：抽出 `build_signal_facts()`，两管线共用；差异只保留「是否允许工具补充查询」。P1 的手写规则 11/12 改为复用 `_RULE_SL` / `compose_rule_tp`。

**附带修复**：市场环境缺数据时渲染「BTC 环境：未知（数据缺失）」，并禁止据此加分；`AIAnalysis` 落 `btc_env_missing` 标记用于复盘分组。

---

### 3.3 目标态提示词骨架（P0/P1 共用）

```
[系统]
角色：只做一件事——在已给定的 4h 方向内，判断这根 1h 入场是否可执行。
硬规则（6 条）：
  1) 方向只来自 4h 状态行；1h 方向票只影响置信度
  2) 禁止自己计算价格/止损/止盈/RR，全部使用程序给出的值
  3) 禁止编造事实；缺失即写「数据缺失」并降置信度，不得猜测
  4) 执行门槛以「执行门槛」行为准
  5) 评分只取 60/70/85 三档，skip ≤30
  6) 输出必须先给「事实摘要」，再给「判断」，最后给决策 JSON

[用户]
一、4h 事实（状态/ADX/斜率/距EMA144/impulse 区间/回撤比例/耗时）
二、1h 事实（触发类型/触点/金K/量比/收盘位置/距触发位 ATR）
三、程序计算结果（止损/TP1/TP2/RR/该类型 RR 下限/质量分及分项）
四、执行约束（当日生效门槛、信号年龄、当前偏离）
五、环境（BTC/资金费率，缺失显式标注）
六、最近 40 根 1h K 线（7 列）
```

要点：**规则只写一次，事实按固定顺序给，程序能算的全部前置**。目标是把提示词 token 降低 ≥30%，同时消除重复与矛盾。

---

## 4. 实施顺序（四阶段，每阶段可独立回滚）

### Phase 0 — 事实修复（不改判据，不改放行集合）

只改「模型看到的事实」，不动 scanner 的放行与排序。

| ID | 内容 |
|---|---|
| T0-1 | 量能口径修正（P1） |
| T0-2 | 注入当日生效门槛（P2） |
| T0-3 | 注入质量分及分项（P3） |
| T0-4 | 指标块标题按触发类型渲染（回踩不再写「方向=方向票多数决」） |
| T0-5 | 市场环境缺失显式标注（P6 附带） |

**验收**：离线重放近 30 天已存 `ScanResult`，断言事实文本与重算值 100% 一致；提示词中不再出现 `×20根均量` 接原始量。

### Phase 1 — 观测补齐（只加字段，不改行为）

| ID | 内容 |
|---|---|
| T1-1 | `funnel_json` 记录 G2/G0 缺失判据的「假想拒绝」集合（不实际拒绝） |
| T1-2 | 每条信号记录 `quality_score` 分项、配额池、排名 |
| T1-3 | AI 输出要求填写 `skip_reason_code`（枚举），便于统计拒绝原因 |

**验收**：观察 1 周，得到「若启用新门会拦掉多少、拦掉的是否为差样本」的基础数据。

### Phase 2 — 影子（判据类改动）

| ID | 内容 | 影子口径 |
|---|---|---|
| T2-1 | G2 周期适配门（`min/max_atr_pct_1h`） | 被拦信号打 `veto_original=atr_band` |
| T2-2 | G0 价差门 + 最小可成交性（`max_spread_bps` / `min_size`） | 同上 |
| T2-3 | 突破防追高（`max_extension_atr`） | 同上 |
| T2-4 | 配额按触发类型分池（F4） | 记录两种分配的选中差异 |
| T2-5 | 抑制键扩展 + 结果反馈（F5） | 记录本会被抑制但放行的信号 |
| T2-6 | 4h 强度分入质量分（F3） | 排序变化 diff |

**上线门槛**：每组影子 ≥30 笔，期望不劣于基线且最大回撤不劣化；样本按 `RULE_VERSION` 独立统计。

### Phase 3 — 提示词结构重构（与评分口径绑定）

| ID | 内容 |
|---|---|
| T3-1 | 评分档位按触发类型分表（P4） |
| T3-2 | 4h 规则单一权威 + `enforce_htf_constraint` 对齐（P5） |
| T3-3 | `build_signal_facts()` 统一 P0/P1（P6） |
| T3-4 | 提示词合并瘦身，token 降 ≥30%（3.3 骨架） |

**注意**：Phase 3 会改变 AI 给分行为，必须与 Phase 0 分开提交、分开 `RULE_VERSION`，否则无法归因。

---

## 5. 验收指标

### 筛选层

| 指标 | 目标 | 数据来源 |
|---|---|---|
| 各 `reject_stage` 占比 | 全量可见，含假想拒绝 | `funnel_json` |
| 回踩进入 AI 的占比 | ≥30% | `scan_results` |
| 信号→成交率 | ≥60% | 信号与订单对账 |
| 被 `min_size` / `spread` 前置拦截的单 | 执行层对应拒绝数降至 0 | 订单日志 |
| 配额选中集合的簇内相关性 | ≤0.8（每簇 ≤2） | 4h 收益相关矩阵 |

### 提示词

| 指标 | 目标 | 验证方式 |
|---|---|---|
| 事实一致率 | 100% | 离线重放断言 |
| 生效门槛一致率 | 100% | 单测（连损 0/2/3 三档） |
| 回踩 rec 分布 | 不再 100% 贴 60 | 观察 2 周 |
| P0/P1 同信号一致率 | ≥90% | 同一信号双跑对比 |
| 提示词 token | 降 ≥30% | 渲染后统计 |

---

## 6. 明确不做

- 不放宽 `STATE_ALIGNED` 前置门——状态扩展必须单独影子，不与本方案绑定。
- 不调整 TP/SL 结构与执行节拍——本方案只负责「选谁」和「给模型什么事实」。
- 不在 Phase 0 改任何阈值或放行判据。
- 不用「放宽止损」去凑 RR。
- 不在影子样本 <30 笔时根据盈亏调参数。
