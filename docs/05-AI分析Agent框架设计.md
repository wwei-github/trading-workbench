# AI 分析 Agent 框架设计

> 状态：**设计稿，暂不开发**。本文档先梳理现有 AI 分析逻辑与瓶颈，再设计一套多角色 Agent 流水线，目标是提升 AI 分析的**效率**（速度/成本/并发）与**准确率**（数据可靠/校验闭环/可复盘）。

---

## 1. 现状梳理

### 1.1 现有流程

```
┌─────────────── 规则层（scanner，Celery 定时）────────────────┐
│ 拉K线(500根,1h,多交易所failover)                              │
│   → 摆动点(收盘价, order=3) → 结构分类(涨/跌/反转/震荡)        │
│   → 关键位(前高/前低/支撑/压力/区间顶底, 聚类+touches+zone)    │
│   → 最新收盘K线触及关键位区域 → position                      │
│   → 12金K(方向须匹配关键位角色) → pattern                    │
│   → EMA21/55/144 门控(反向否决/同向加权) → signal            │
└──────────────────────────┬───────────────────────────────────┘
                           ↓ ScanResult 落库
┌─────────────── AI 层（ai_tasks.run_ai_analysis_task）────────┐
│ 读 SystemConfig 开关 → 遍历 ScanResult（串行, 每个间隔1s）     │
│   每个币种:                                                   │
│   ① 重新拉 500 根 K 线（交易所 IO，扫描阶段已拉过一次）         │
│   ② 程序算 EMA 形态摘要                                       │
│   ③ 拼 prompt：信号摘要 + 关键位明细 + 30根K线CSV +            │
│      自定义策略提示词(可选) + 用户补充说明(可选)                │
│   ④ 单次 chat.completions（GLM思考模型, temp=0.3,             │
│      max_tokens=12000, 失败重试1次, 手剥```包裹→json.loads）   │
│   ⑤ JSON 直接 upsert 到 ai_analysis 表                        │
└──────────────────────────┬───────────────────────────────────┘
                           ↓
        前端 3s 轮询拉取（单行分析超时 120s）
```

关键文件：
| 环节 | 文件 | 说明 |
|---|---|---|
| 信号生成 | `app/services/strategy/__init__.py` | 关键位+12金K+EMA门控 统一过滤 |
| AI 任务 | `app/tasks/ai_tasks.py` | 串行循环、拉K线、upsert |
| AI 调用 | `app/services/ai_analyzer.py` | prompt 拼装 + 单次 LLM 调用 + JSON 容错 |
| 触发入口 | `app/api/scan.py` | 全量（扫描后）/单币种手动重分析 |
| 模型配置 | `app/config.py` | `AI_API_KEY / AI_BASE_URL / AI_MODEL`（OpenAI 兼容） |

### 1.2 痛点清单

**效率**

| # | 问题 | 位置 |
|---|---|---|
| E1 | 串行逐币种分析，`sleep(1)` 限速；几十个币种时要数分钟 | ai_tasks.py 循环 |
| E2 | 每个币种重新拉 500 根 K 线 —— 扫描阶段刚拉过，重复交易所 IO，还有限速/封禁风险 | ai_tasks.py `pool.get_klines` |
| E3 | 单次调用承担全部职责（读数据→验证→决策→算价→写叙述），prompt 巨大、思考型模型 token 消耗大、时延高，前端轮询需 120s 超时兜底 | ai_analyzer.py |
| E4 | 无缓存：同一币种信号未变时（扫描周期内重复命中）重复全价分析 | ai_tasks.py |
| E5 | max_tokens=12000 防截断式配额，思考 token 花费不可控 | ai_analyzer.py |

**准确率**

| # | 问题 | 位置 |
|---|---|---|
| A1 | JSON 无 schema 校验：`json.loads` 成功即入库，字段缺失/类型错/枚举值非法全放行 | ai_analyzer.py 末尾 |
| A2 | 无程序复核：AI 自报盈亏比/止损方向，做多止损高于入场这类逻辑错误可能直接入库 | ai_tasks.py `_upsert` |
| A3 | 信息供给有限易幻觉：只给 30 根 K 线 CSV + 摘要，AI 定的止损/止盈价位经常不是真实结构位，凭"感觉"报数 | ai_analyzer.py prompt |
| A4 | 单视角单采样：一次调用即最终结论，无自我审查、无分歧校验 | ai_analyzer.py |
| A5 | 规则层与 AI 权责重叠：规则层已用 EMA 门控否决/加权，AI 又拿 EMA 摘要重复判断一遍，口径可能互相矛盾 | strategy/__init__.py vs ai_analyzer.py |
| A6 | 无复盘闭环：建议没有后续跟踪（对/错），提示词永远靠人肉调 | 全局 |
| A7 | 决策与叙述耦合：300 字分析影响 token/时延，且叙述改动可能扰动决策本身 | SYSTEM_PROMPT |

---

## 2. 设计目标与核心原则

**目标**

1. 单币种分析端到端时延可控（P50 < 15s），批量扫描后 AI 完成时间 ≤ 串行现状的 1/3
2. 落库的每条建议**结构合法、数值自洽**（方向-价格-盈亏比程序复算通过）
3. AI 只做"判断"，"算数"全部交给程序；可复用数据不重复拉取
4. 每条建议可复盘，命中率可统计，复盘结果反哺提示词

**核心原则**

- **P1 程序能算的不让 AI 算**：EMA/ATR/盈亏比/仓位公式全部程序计算，AI 输出的是"方向与位置选择"，不是数字
- **P2 数据按需供给，工具化查询**：不再整段塞 30 根 CSV，AI 通过工具按需取（当前价、关键位明细、摆动点、更早历史、资金费率…），省 token 且减少幻觉
- **P3 结构化输出 + 校验回炉**：Pydantic Schema 强约束 + 确定性校验器，不合格自动带错误反馈重试（≤2 次），仍不合格降级为 skip
- **P4 决策与叙述分离**：先定结构化决策，再单独生成中文叙述；叙述不回改决策
- **P5 失败可降级**：流水线任一环节异常 → 自动回退到现有"单次调用"模式，保证可用性

---

## 3. 总体架构

```
                       ┌────────────────────────────────┐
                       │  Stage 0  数据准备（纯程序）      │
  ScanResult ─────────→│  · K线缓存复用（扫描时已拉取，    │
                       │    按 symbol+interval 命中缓存） │
                       │  · 指标预算: EMA状态/ATR/量能分位 │
                       │  · 信号指纹 fingerprint          │
                       └──────────┬─────────────────────┘
                                  ↓
                       ┌────────────────────────────────┐
                       │  Stage 1  规则闸门（纯程序,0 token）│
                       │  strength 阈值 / 重复信号 /      │
                       │  信号指纹缓存命中 → 直接短路       │
                       └──────────┬─────────────────────┘
                                  ↓（通过闸门才进 AI）
   ┌──────────────────────────────┴──────────────────────────────┐
   │                并行执行（Celery group + LLM 信号量）           │
   │                                                              │
   │  ┌─────────────┐   ┌─────────────┐   ┌────────────────────┐  │
   │  │ Stage 2     │   │ Stage 3     │   │ Stage 4            │  │
   │  │ Analyst     │ → │ Trader      │ → │ Risk Guard         │  │
   │  │ 轻量模型     │   │ 主力模型     │   │ 纯程序校验(+可选复核)│  │
   │  │ 四要素解读   │   │ 结构化决策   │   │ 不合格→带错重试(≤2) │  │
   │  └─────────────┘   └──────┬──────┘   └─────────┬──────────┘  │
   │                           ↓ 合格                │             │
   │                   ┌───────┴────────┐  不合格→回Stage3或降级skip│
   │                   │ Stage 5        │                       │
   │                   │ Narrator 叙述  │  轻量模型              │
   │                   └───────┬────────┘                       │
   └───────────────────────────┼────────────────────────────────┘
                               ↓
                     ai_analysis 落库（含 stage 轨迹）
                               ↓
                     Stage 6 复盘跟踪（定时任务）
                     1h/4h/24h 后对照入场/止损/止盈
                     命中率统计 → 摘要注入 Trader 提示词
```

---

## 4. 各阶段设计

### Stage 0 — 数据准备（纯程序，0 token）

- **K线缓存复用**：扫描任务拉取的 500 根 K 线按 `symbol+interval` 存入内存/Redis 短期缓存（TTL = 1 根 K 线周期 + 余量），AI 阶段直接命中，不再打交易所（解决 E2）
- **指标预算**（一次算好供全程使用）：EMA21/55/144 状态（复用 `analyze_ema`）、ATR(14) 及其占价比、量能 20 根分位、距最近关键位距离
- **信号指纹**：`sha1(symbol + signal_type + position + pattern + 收盘价按0.3%分桶 + 最新关键位价格)`。指纹相同 → 视为同一信号（解决 E4 的缓存键）

### Stage 1 — 规则闸门（纯程序，0 token）

进入 AI 前的最后过滤，短路掉不值得花 token 的信号：

- `strength < 0.4` → 直接 skip（原因：信号强度不足，不耗 AI）
- 24h 内同方向重复信号且 AI 已有结论 → 沿用旧结论（`is_repeat` 已有数据基础）
- 指纹缓存命中（TTL 1h）→ 直接返回上次结果
- 极端行情熔断：当前 K 线振幅 > 5×ATR → 跳过（插针行情不适合开单）

### Stage 2 — Analyst 解读（轻量模型，快/便宜）

输入：结构化事实包（信号摘要、关键位明细、EMA 状态、ATR、量能分位——**不含原始 K 线**）。
输出：四要素打分 JSON：

```json
{
  "trend":    {"score": 0-10, "note": "EMA多头排列, 斜率向上"},
  "location": {"score": 0-10, "note": "回踩支撑位(0.618回撤+2次触及)"},
  "momentum": {"score": 0-10, "note": "看涨吞没, 量能80分位"},
  "risk":     {"score": 0-10, "note": "上方0.8%存在区间顶压制"},
  "summary":  "一句话: 支撑位看涨形态+趋势同向, 但上方压制近"
}
```

价值：把长 prompt 的"读材料"职责剥离给便宜模型，Trader 只拿摘要决策（解决 E3/E5）。四要素分数同时成为后续**可复盘的结构化特征**。

### Stage 3 — Trader 决策（主力模型）

输入：Analyst 摘要 + 关键位明细 + **工具集**（见下）。
输出：强 Schema JSON（Pydantic 校验）：

```python
class TradeDecision(BaseModel):
    trade_decision: Literal["suggest", "skip"]
    skip_reason: list[str] = []            # ≤3 条；suggest 时必须为空
    direction: Literal["long", "short"] | None
    # 关键改变：AI 不再直接报价格，而是报"结构位引用 + 偏移"
    entry_level_ref: str | None            # 关键位列表中的 kind，如 "support"
    entry_offset_pct: float                # 相对该位的偏移%，通常 0~0.2
    stop_level_ref: str | None             # 止损锚定的结构位 kind
    stop_offset_pct: float
    tp1_level_ref: str | None              # 止盈锚定（如上方压力位/区间顶）
    tp2_level_ref: str | None
    recommendation: int                    # 0-100
```

**工具集**（function calling，按需调用）：

| 工具 | 返回 | 用途 |
|---|---|---|
| `get_recent_klines(n)` | 最近 n 根已收盘 K 线 | 需要看具体形态细节时才取 |
| `get_key_levels()` | 全部关键位（价格/zone/touches/role） | 精确锚定结构位 |
| `get_swing_points(n)` | 最近 n 个摆动点（价格+时间） | 确认结构位来源 |
| `get_indicator(name)` | EMA/ATR/量能分位等预算值 | 复核 |

价格由程序换算：`entry = level_price * (1 + offset/100)`（解决 A3 —— AI 只选锚点，不报浮点价格，从根上消灭"拍脑袋价位"）。

### Stage 4 — Risk Guard 校验（纯程序 + 可选模型复核）

确定性校验清单（全部程序可算，解决 A1/A2）：

- Schema 合法（Pydantic）
- 方向-价格一致性：long 要求 `stop < entry < tp1 < tp2`；short 反之
- 锚点存在性：`*_level_ref` 必须在关键位列表中
- **盈亏比复算**：`rr = |tp1-entry| / |entry-stop|`，要求 ≥1.2 才允许 suggest（AI 声称值一律不信，用复算值落库）
- 止损合理性：`|entry-stop|` ∈ [0.3×ATR, 3×ATR]（太近易扫损、太远盈亏比崩）
- 仓位公式化：`position_pct = clamp(风险预算% / (|entry-stop|/entry), 0.5, 10)` —— AI 不再自报仓位
- skip 一致性：suggest 时 `skip_reason` 必须为空，反之亦然

不合格 → 把**具体违规项**作为反馈消息追加，重试 Trader（≤2 次）；仍不合格 → 强制 skip（skip_reason="风控校验未通过: ..."）。可选增强：`recommendation ≥ 70` 的高置信 suggest 触发第二视角复检（双评委，分歧则降 recommendation）——默认关闭。

### Stage 5 — Narrator 叙述（轻量模型）

输入：定稿的 TradeDecision + Analyst 摘要（只许转述，不许改结论）。
输出：≤300 字中文 analysis（现有格式的 1. 2. 3. 逐条）。任务极短，秒级完成（解决 A7、进一步压时延）。失败不影响决策落库，analysis 字段退化为模板拼接。

### Stage 6 — 复盘跟踪（定时 Celery 任务，解决 A6）

- 建议落库后 1h/4h/24h 三个观察点，用缓存/交易所价格对照：
  - 先触止损 → `loss`；先触 tp1 → `win_tp1`；先触 tp2 → `win_tp2`；未触 → `open`
- 写入新表 `trade_review(scan_result_id, horizon, outcome, max_favorable, max_adverse)`
- 统计页：按 信号类型 × 关键位类型 × EMA状态 分组的命中率/平均RR
- **记忆注入**：每 N 天把"最近 30 条复盘摘要"（哪类信号常输、哪类常赢）压缩成一段文字注入 Trader 系统提示词，形成自我校准闭环

---

## 5. 并发与成本模型

- **并行**：批量分析改为 Celery `group`（每币种一个子任务），全局 `Redis 信号量` 限 LLM 并发（如 3），避免交易所/LLM 限速；单币种内部 Stage2→3→5 串行
- **模型分工**（OpenAI 兼容网关配两个模型即可）：
  | 角色 | 模型档位 | 预期 |
  |---|---|---|
  | Analyst / Narrator | 轻量（如 GLM-flash 级） | 单次 <2s，成本 ~1/10 |
  | Trader | 主力思考模型（现 AI_MODEL） | 只做决策，输出短，max_tokens 降至 ~2000 |
- **成本估算**（相对现状）：现状 = 每币种 1 次全量思考调用（12k tokens 配额）。新方案 = 1 次轻量 + 1 次主力（短输出）+ 1 次轻量 ≈ 主力 token 降 60%+，总成本下降，时延 P50 显著低于现状
- **降级路径**：任何 Stage 异常 → 回退现有 `analyze_coin` 单次调用模式（代码保留，作为 fallback）

---

## 6. 数据库与接口改动（预估）

| 改动 | 内容 |
|---|---|
| `ai_analysis` 表 | + `fingerprint`(索引)、`stage_trace`(JSON: 各阶段耗时/token)、`review_status` |
| 新表 `trade_review` | 复盘观察点结果 |
| `system_config` | + `ai_pipeline_enabled`（新流水线总开关，关=走旧逻辑）、`ai_min_strength`、`ai_rr_min`、`llm_concurrency` |
| API | `/ai-analyses` 响应附复盘状态；新增 `/ai-review-stats` 统计端点 |
| 前端 | 结果表加"复盘"列（win/loss/open 徽章）；统计页入口 |

---

## 7. 分期实施路线（暂不开发）

| 期 | 内容 | 解决 | 工作量感受 |
|---|---|---|---|
| **P0 立柱子** | Pydantic Schema + Risk Guard 校验回炉；K线缓存复用；批量并行 + 信号量 | A1 A2 E1 E2 | 小（不动 prompt 结构，纯外层加固） |
| **P1 拆角色** | Analyst/Trader/Narrator 三段拆分；结构位锚点替代裸价格；指纹缓存 | E3 E4 E5 A3 A7 | 中 |
| **P2 闭环** | trade_review 复盘任务 + 统计页 + 记忆注入；可选双评委 | A4 A6 | 中大 |

P0 收益最大且风险最小（即使不拆角色，校验+并行+缓存也直接消灭一半痛点），建议未来开发从 P0 起步。

---

## 8. 风险与权衡

- **锚点模式限制表达力**：AI 只能选关键位做锚点，极端行情想挂"突破追多"价时表达受限 → 保留 `entry_offset_pct` 弹性 + 极端情况允许 `level_ref="market"`（市价锚）
- **多阶段引入新的失败面**：每阶段都有超时与降级（Analyst 失败→直接进 Trader；Narrator 失败→模板叙述；Trader 重试耗尽→skip），最坏等价现状
- **轻量模型误读**：Analyst 打分偏低会压制 suggest 率 → 四要素分数进复盘统计，长期可回归校准阈值
- **复盘观察点的主观性**：先到止损还是先到止盈在插针行情受 tick 粒度影响 → 复盘只做趋势性统计，不做逐条定责
