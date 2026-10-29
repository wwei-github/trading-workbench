# 23 · 筛选链优化与观测机制

> 2026-10-29 · 来源：20 天 / 100 币筛选链回放审计 + 腿门 A/B + TRUTHUSDT 止盈未全平排查
> 状态：待排期（任务清单已定，代码未改）

---

## 0. 一页摘要

| 结论 | 证据 |
|---|---|
| 唯一确凿的「误杀」是 4h 走平门的**绝对百分比阈值** | SPY 100%、GOOGL 78%、AAPL 70%、BTC 52% 的根被判走平，SOL/DOGE 仅 3~4%；被 flat 拒的样本 avgR +0.186（n=304，t=2.65），同期 PASS 基准 +0.164 |
| 4h 方向门与状态锁**口径重复**，死因记错账 | 46 个 `undetermined` 样本状态机全部有明确态，其中 45 个本来就会被 `htf_state_lock` 拦，真正独占误杀仅 1 个 |
| 腿门**有必要**，但实现有三处缺陷 | 关掉腿门候选 +53%（49→75）但期望 0.147→0.116；新增 26 个中 11 个被错记为 `range_score`（类型漂移） |
| tangle / 状态锁 / range_score 拦得对，不动 | tangle +0.011（n=273）、turn_bear −0.176、bull_dip −0.146、range_score −0.035 |
| 「观察 7 天再决策」目前**没有任何系统载体** | 任务记录只管已跑任务；飞书只有日报；补丁评估只管 `prompt_patches` |
| TRUTHUSDT 未全平**已定位为非缺陷** | 该单 10-27 开仓（早于全平模式上线），走分批 60/20/80；线上 5 单全平口径两档数量精确覆盖全仓；07:21 一次页面保存把开关改回 false |

---

## 1. 任务清单

### 1.1 第 0 批｜核实现状（不改代码）

| ID | 任务 | 做法 | 完成定义 |
|---|---|---|---|
| T0 | 核实线上 `quality_floor` | `SELECT scan_filters->>'quality_floor' FROM system_config;` 或 `GET /api/scans/config` | 知道实际值；>0 则 T11 提前到第 1 批 |

### 1.2 第 1 批｜逻辑修正（零行为漂移）

| ID | 任务 | 位置 | 改法 | 验收 |
|---|---|---|---|---|
| T1 | 腿门类型漂移 | `trigger.py:1105-1116`、`scanner.py:595-610` | 腿门失败立即终止并记 `leg_gate`，不再转投突破判定 | 候选数 ±0；`range_score` 中混入的腿门样本归零 |
| T2 | 腿门确认侧归因 | `trigger.py:1169-1177` | `diag` 写 `leg_gate_fail`，窗口耗尽 detail 标「确认侧腿门未过」；不杀 pending | `window_expired` 可区分腿门死因 |
| T3 | 4h 方向门与状态锁归因 | `technical.py:944-962`、`scanner.py:433-464` | 顺序改为 flat/tangle/数据 → `block_both` → 方向门 | 候选数 ±0；`gate_4h` 降、`htf_state_lock` 升 |
| T4 | `direction_flip` 死分支 | `scanner.py:507-508, 579-585` | 反向票并入 votes 关，或删分支并退役开关 | 反向票样本能记到 `direction_flip` |

### 1.3 第 2 批｜观察期机制（提醒本身）

| ID | 任务 | 依赖 | 交付物 | 完成定义 |
|---|---|---|---|---|
| T5 | 观察任务表 + API | — | `observation_tasks`（见 §3）；`GET /api/observations`、`POST /api/observations/{id}/decide` | 手工插一条 pending 可查 |
| T6 | 到期扫描 + 飞书推送 | T5 | 挂在已有 `daily-attribution`（08:10）末尾，**不新增 beat**；`pending 且 due_at<=now()` → `due` + `send_app_card` | 造一条昨日到期记录，次日 08:10 收到卡片 |
| T7 | 页面展示 | T5 | `ScanResult`→「任务记录」tab 顶部「待决策 (N)」区块；`MarketOverviewBar` 红点；决策后归档 | 有 due 时可见，决策后消失 |

> 没有 T6 就没有提醒——页面红点只在你打开页面时生效。

### 1.4 第 3 批｜观测数据（只加字段与展示）

| ID | 任务 | 依赖 | 交付物 | 观察项 |
|---|---|---|---|---|
| T8 | 走平门影子字段 | — | `htf_context` 输出 `atr_pct_4h` / `slope_atr_ratio` / 影子 `flat_norm`；scanner 写逐币 `details` | 登记「走平门归一」，due=+7d |
| T9 | 腿门分档结构化 | T2 | `leg_evidence`（bars / atr_mult / overshoot）；「无触发」页签加档位筛选 | 登记「腿门 min_atr 分档」，due=+7d |
| T10 | 4h 拒因分布 + 降级 | — | `/scans/funnels` 补 `htf_degraded`；最新筛选吸顶条加 flat/tangle/undetermined/degraded 分布 | 登记「数据抖动」，due=+14d |

### 1.5 第 4 批｜行为变更（逐项上线，各自留观察期）

| ID | 任务 | 前置 | 改法 | 回退 |
|---|---|---|---|---|
| T11 | 质量分同源惩罚去重 | T0 | `technical.py:1564-1596`：命中 `box` 不再计 `range`/`r2_low`，或回踩 `max(score,35)`；顺手把 `volume` 阈值接回 `flt.breakout_vol_ratio` | 删优先级判断 |
| T12 | 走平门按 ATR 归一 | T8 数据 | `flat = slope_pct < max(下限, k×atr_pct_4h)` | `k=0` 恢复绝对阈值 |
| T13 | 4h 数据缺失 fail-open | T10 数据 | 新增开关（默认 False），缺失时用 1h 票定向 + `degraded` | 开关置 False |

### 1.6 Bug 修复

| ID | 任务 | 位置 | 说明 |
|---|---|---|---|
| T14 | 止盈全平：配置变更审计 + 存量持仓提示（**非缺陷**） | `api/scan.py:825`、`TradingConfigPanel.tsx:424`、`system_config` | 见 §2 |

---

## 2. TRUTHUSDT 到 TP2 后未全部平仓（已定位：非代码缺陷）

### 2.1 现象

- 用户观察：止盈全平模式（`tp_full_exit_enabled`）为**打开**。
- `TRUTHUSDT` 上一个整点触发 TP2，仓位未清零（剩余仓位继续被跟踪止损管理）。

### 2.2 线上实测（2026-10-29 07:40 UTC，root@43.129.220.237）

| 事实 | 值 |
|---|---|
| 当前配置 | `tp_full_exit_enabled = false`（`tp2_enabled = true`） |
| API 回读 | `GET /api/scans/config` → `"tp_full_exit_enabled":false` |
| 该单 | `id=a38e3425…`，`TRUTHUSDT`，long，2026-10-27 12:02 开仓，`status=TP2_HIT`，未平仓 |
| 该单数量 | `qty=3718`，`raw.qty_tp1=2230`（60.0%），`raw.qty_tp2=743`（20.0%） |
| 该单快照 | `raw.tp_full_exit` **字段不存在**（10-28 上线前开的老单） |
| 事件 | `TP1_FILL qty=2230`（04:31）、`TP2_FILL qty=743`（06:31），合计 2973/3718 = 80% |
| 剩余 | 745（20%），由跟进止损管理 |

对照：近 14 天 `raw.tp_full_exit=true` 的 5 单（10-29 01:01~05:58 开仓）两档数量**精确覆盖全仓**：

```
MEGAUSDT      2337 = 1402 + 935
SOLVUSDT     17259 = 10355 + 6904
BABYUSDT     12434 = 7460 + 4974
AIOTUSDT      1695 = 1017 + 678
1000PEPEUSDT 10450 = 10450 + 0   （无 TP2 → TP1 平 100%）
```

**结论：全平模式代码路径工作正常。** TRUTHUSDT 是 10-27 开的老单（早于 10-28 全平模式上线），走的是分批 60/20/80，TP2 只平 20% 属设计行为。

开关为何显示为「打开」而后端为 `false`：

```
10-29 01:01~05:58  5 单以全平模式开仓（说明当时配置为 true）
10-29 06:36        前后端重新部署（容器 StartedAt）
10-29 06:31        TRUTHUSDT TP2 成交（老单，与开关无关）
10-29 07:21:25     一次 PUT /api/scans/config → 之后 tp_full_exit_enabled = false
```

即：**是 07:21 那次页面保存把开关关掉的**（或用户确实点了关闭）。`system_config` 无 `updated_at` 列，无法进一步定位操作者；日志只证明该时刻发生了一次保存。

### 2.3 当前实现（代码事实）

```
开仓 _tp_order_quantities (trade_engine.py:188-202)
  full_exit = cfg.tp_full_exit_enabled
  qty_tp1 = round_qty(qty × p1%)                 # floor 到 stepSize
  qty_tp2 = round_qty(qty − qty_tp1)             # 全平：覆盖剩余
  TP2 挂单条件：tp2>0 且 qty_tp2 ≥ min_qty 且 qty_tp1+qty_tp2 ≤ qty
  挂单失败 → raw.qty_tp2 记 0（:1205），_is_tp_full_exit 不会误判
```

```
TP2 触发后（trade_engine.py:3374-3390）
  tp2_hit（_tp_filled_real：不在挂单列表 + algoStatus=FINISHED）
  → status = TP2_HIT → _move_sl_trailing(...)
  没有任何「检查交易所剩余持仓并补平」的步骤
```

`_is_tp_full_exit`（:2748）只在**仓位已归零后**用于推断 `exit_reason`，不参与平仓动作。

### 2.4 其他潜在根因（防御性排查，本次未触发）

> 先排除一个常见误判：全平模式下 `qty_tp2 = round_qty(qty − qty_tp1)`，而 `round_qty` 用 `Decimal` 精确 floor（`binance_trader.py:281-287`）。由于 `qty` 是交易所成交量、`qty_tp1` 是 floor 后的 step 整数倍，两者差值仍是 step 整数倍，**两档合计在数学上恒等于全仓**（线上 5 单已验证）。所以「没全平」不可能是数量分配问题，只能是成交侧或开仓快照。

| # | 根因 | 判定证据 | 本次是否命中 |
|---|---|---|---|
| R1 | **该单开仓时开关还没打开**（存量仓位不重挂 TP；开关只在开仓时快照） | `raw.tp_full_exit` 缺失或 `false` | **命中**（TRUTHUSDT，10-28 上线前开仓） |
| R2 | **TP2 条件单只部分成交**（薄盘/滑点：`FINISHED` ≠ 全额成交；reduceOnly 只平可用量） | `q1+q2 ≈ qty` 但仍有残仓；子单 `executedQty < qty_tp2` | 未命中 |
| R3 | **TP2 挂单被跳过**（`qty_tp2 < min_qty`，此时 `raw.qty_tp2=0`，只剩 TP1 在挂） | `raw->>'qty_tp2' = '0'` 且 `tp2_order_id IS NULL` | 未命中 |
| R4 | **页面显示开但保存关闭/未落库** | `system_config` 与页面/接口不一致 | **部分命中**（07:21 一次保存把开关改回 false） |
| R5 | **qty 非 step 整数倍**（异常成交量/手工改单） | `qty % step ≠ 0`，`qty_tp1+qty_tp2 < qty` | 未命中 |

### 2.5 排查 SQL（复用）

```sql
-- 1) 当前开关
SELECT tp_full_exit_enabled FROM system_config WHERE id = 1;

-- 2) 该单开仓快照与两档数量
SELECT id, symbol, status, qty, tp1, tp2, opened_at, closed_at, exit_reason,
       raw->>'tp_full_exit' AS full_snapshot,
       raw->>'qty_tp1'      AS q1,
       raw->>'qty_tp2'      AS q2,
       raw->>'tp1_order_id' AS tp1_id,
       raw->>'tp2_order_id' AS tp2_id
FROM trade_records
WHERE symbol = 'TRUTHUSDT'
ORDER BY id DESC LIMIT 3;

-- 3) 事件流水（看 TP2_FILL 的 qty 是否等于 q2）
SELECT event_type, detail, created_at
FROM trade_events
WHERE trade_record_id = <上一步 id>
ORDER BY id;

-- 4) 近 14 天有多少单实际以全平口径开仓
SELECT count(*) FILTER (WHERE raw->>'tp_full_exit' = 'true') AS full_true,
       count(*) FILTER (WHERE raw->>'tp_full_exit' = 'false') AS full_false,
       count(*) AS total
FROM trade_records WHERE opened_at > now() - interval '14 days';
```

### 2.6 改进项（从「bug 修复」降级为「防呆」）

| 子项 | 内容 | 针对 | 优先级 |
|---|---|---|---|
| T14a | **开关变更可追溯**：`system_config` 加 `updated_at`；配置 PUT 写一条 `config_change` 审计（字段/旧值/新值/来源 IP），页面「配置变更」可查 | R4 | P0 |
| T14b | **保存回读校验**：PUT 后 GET 比对关键开关，不一致时页面报错（当前只报「配置已保存」） | R4 | P1 |
| T14c | **开关作用域提示**：面板已写「仅对新开仓生效」，再加一条**存量持仓提示**：打开开关时列出未平仓单，注明它们不会补平，并提供「手动平掉尾仓」入口 | R1 | P1 |
| T14d | **TP2 挂单缺失告警**：`tp2>0` 但 `tp2_order IS NULL` 时记 `TP2_SKIPPED`，持仓卡片标注「无 TP2，剩余由跟踪止损退出」 | R3 | P2 |
| T14e | **TP2 后残仓兜底**：`status→TP2_HIT` 且 `raw.tp_full_exit=true` 时校验交易所持仓，仍有残仓则市价 `reduceOnly` 补平，记 `FULL_EXIT_RESIDUAL`（同单只补一次） | R2/R5 | P2（防御） |

> 排序变化说明：原计划以 T14a（补平）为主修，实测证明代码路径正确，补平只是未来防御；当前真正的痛点是**「开关何时被改、改成了什么」完全不可追溯**，所以审计优先。

### 2.7 验收

1. 配置变更后 `system_config.updated_at` 与 `config_change` 审计可见（含旧值/新值）。
2. 页面对全平开关做 PUT→GET 比对，不一致时报错而不是「保存成功」。
3. 单测：`tp2>0` 但挂单被跳过 → 产生 `TP2_SKIPPED` 事件，且 `_is_tp_full_exit=false`。
4. 单测：TP2 后仍有残仓 → 补平一次且幂等（防御项上线后）。

---

## 3. 观察期机制（T5-T7）

### 3.1 表结构

```
observation_tasks
  id            PK
  name          例：走平门按 ATR 归一（影子）
  config_key    例：slope_flat_atr_mult
  started_at / due_at
  metrics       JSON，每日刷新（如 {flat_abs_n, flat_norm_n, diff_avgR, by_symbol}）
  decision_rule text，登记时写死，禁止到期再定
  status        pending | due | decided
  decision / decided_at / note
```

### 3.2 到期扫描

- 挂在 `daily-attribution`（08:10）末尾，复用 `feishu_bot.send_app_card`，**不新增 beat 任务**。
- `pending` 且 `due_at <= now()` → `status='due'` → 推送「到期 + 判据 + 当前读数 + 去看哪儿」。

### 3.3 页面

| 落点 | 展示 |
|---|---|
| `ScanResult` →「任务记录」tab 顶部 | 「待决策 (N)」：名称 / 到期日 / 判据 / 当前读数 /「去决策」 |
| `MarketOverviewBar` | `due` 时红点 |
| 决策后 | 同表 `decided`，形成「改阈值 → 观察 → 结论」可回溯链路 |

### 3.4 三条初始观测项

| 名称 | 观察期 | 判据 | 到期看哪 |
|---|---|---|---|
| 走平门按 ATR 归一（影子） | 7 天 | 差集 avgR ≥ +0.1R，且 SPY/XAUT/MSFT 未被误放 | 最新筛选 →「4h 背景门」抽屉 + 筛选漏斗判定说明 |
| 腿门 `min_atr` 分档（0.3/0.5/0.8） | 7 天 | 0.3 档增量候选 avgR ≥ 0.1R 且胜率 ≥ 45% | 「无触发」页签 |
| 4h 数据缺失 fail-open | 14 天 | 降级占比 >1% 的轮次 ≥ 3，且该时段候选收益不为负 | 最新筛选吸顶条「4h 降级」 |

---

## 4. 批次与依赖

```
T0 ──> (floor>0?) ──> T11 提前
T5 ──> T6 / T7                     ← 提醒先跑起来
T1 ──> T2 ──> T9                   ← 观测口径先修对
T3、T4 可与 T1 并行
T8 / T10 ──> 登记 observation ──> 7/14 天后 T6 推送 ──> 决策 T11 / T12 / T13
T14a（配置审计）+ T14b（回读校验）──> 独立上线，不依赖筛选链
```

| 批次 | 内容 | 预估 |
|---|---|---|
| 第 1 批 | T0 + T5 + T6 | 1 天 |
| 第 2 批 | T7 + T1/T2 + T3/T4 | 1.5 天 |
| 第 3 批 | T14a + T14b（配置变更审计 + 保存回读） | 1 天 |
| 第 4 批 | T8 + T9 + T10 + 登记 3 条观察项 | 1 天 |
| 第 5 批 | T11 | 0.5 天 |
| 7~14 天后 | T6 到期推送 → 决策 T12 / T13 | — |

---

## 5. 不做

| 项 | 理由 |
|---|---|
| 放宽 `tangle` | +0.011R（n=273），与 docs/16 §15 一致 |
| 放宽 `STATE_ALIGNED` 白名单 | turn_bear −0.176、turn_bull −0.268、bull_dip −0.146、macro_cross_bear −0.135 |
| `range_score` 对突破的硬拒 | −0.035R（n=205） |
| 删除腿门 | 关掉后候选 +53%，期望 0.147→0.116 |
| 提高流动性门槛 | 风险已由执行层兜底，会砍掉小币行情 |
| 为提醒新增 beat 任务 | 复用 08:10 `daily-attribution` |
| 新建独立「观测中心」页面 | 先放任务记录 tab + Header 红点 |

---

## 6. 实施进度（2026-10-29）

| 批次 | 任务 | 状态 |
|---|---|---|
| 0 | T0 核实 `quality_floor` | ✅ 线上实测：`quality_floor=60`、`min_volume_24h=2M`、`allowed_triggers` 3 类、`direction_vote_gate_enabled=false`、`direction_conflict_gate_enabled=false`。**注意**：正文回放按默认筛配置（投票门开）跑，与线上口径不同，T3/T4 的优先级应下调 |
| 1 | T5 观察任务表 + API | ✅ `models/observation.py`、`schemas/observation.py`、`services/observations.py`、`api/observations.py` |
| 1 | T6 到期扫描 + 飞书 | ✅ 挂在 `tasks/attribution_tasks.py`（08:10）末尾，`check_due()` 标 `due` + 发卡，失败不影响归因 |
| 1 | T7 页面 | ✅ `TaskList` 顶部「待决策」区块 + 决策弹窗；`ScanResult` 任务记录页签红点（30s 轮询 `counts.due`） |
| 2 | T1–T4 逻辑修正 | ✅ T1 腿门失败的「未进池」不再被记成 `range_score`（归回 `leg_gate` + 证据）；T2 确认侧腿门标 `leg_gate`（不再被 `window_expired` 吞掉）；T3 `undetermined` + 状态机禁开归回 `htf_state_lock`（tangle 仍记 `gate_4h`）；T4 `direction_flip` 提到 `votes` 之前——3 票全反向时可达。**全部只改关卡名/说明，拒绝动作与候选数不变** |

测试：`tests/test_observations.py`（5 例）、`tests/test_stage_attribution.py`（4 例）、`test_no_trigger_stage.py` 新增确认侧腿门用例；后端全量 **1363 passed**；前端 `tsc --noEmit` 通过。

登记一条观察项（部署后）：

```bash
curl -X POST localhost:8000/api/observations \
  -H 'Content-Type: application/json' \
  -d '{"name":"走平门按 ATR 归一（影子）","days":7,"config_key":"slope_flat_atr_mult",
       "decision_rule":"差集 avgR ≥ +0.1R 且 SPY/XAUT/MSFT 未被误放"}'
```

提醒触发条件：`daily-attribution`（北京 08:10）扫描 `pending` 且 `due_at<=now` 的记录。手工验证可先把 `due_at` 改到过去（`PATCH /api/observations/{id}`），再等次日任务或手动触发该 celery 任务。

---

## 7. 附录：回放口径

- 样本：Binance USDT 永续，24h 成交额 ≥3M，按成交额取前 100；最近 20 天
- 判定：只取「动量票 |net|≥2」的已收盘 1h 根，同币同向 48 根内去重（前向窗口不重叠）
- 收益：入场=信号根收盘，SL=1.33×ATR14(1h)，TP=1.5R，前向 48 根，同根 SL 优先
- 链路：真实 `detect_indicator_candidate`（`pending=None`、`quality_floor=0`）
- 基准：已通过全部门（PASS）avgR +0.164（n≈49~75 视配置）
- 局限：单期 20 天、裸方向回放，未叠加触点/金K/质量分/AI 配额；**组间对比可用，绝对值不可当预期**

腿门 A/B（同一批事件，patch `_pullback_leg_gate` 恒真）：

| 组 | 候选 | avgR | 胜率 |
|---|---|---|---|
| 腿门开 | 49 | +0.147 | 46.9% |
| 腿门关 | 75 | +0.116 | 45.3% |
| 关后新增 26 个 | — | +0.058 | 42.3% |
