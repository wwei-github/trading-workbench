# 23 · 筛选链优化与观测机制

> 2026-10-29 · 来源：20 天 / 100 币筛选链回放审计 + 腿门 A/B + TRUTHUSDT 止盈未全平排查
> 状态：部分已实现（T1–T4 归因、T5–T7 观察期、T8–T10 观测字段、T11/T12/T13/T14a–T14f 已实施，2026-10-30；T11–T13 需按 §3.4 观察期复核后再定去留）

---

## 0. 一页摘要

| 结论 | 证据 |
|---|---|
| 4h 走平门的**绝对百分比阈值**疑似误杀（**信号级、待复核**） | SPY 100%、GOOGL 78%、AAPL 70%、BTC 52% 的根被判走平，SOL/DOGE 仅 3~4%；被 flat 拒的样本 avgR +0.186（n=304，t=2.65）vs PASS 基准 +0.164——差值仅 0.022R（≈0.3 SE），按 avgR 判「确凿误杀」证据不足，须按 §3.4 的差集判据（`flat_abs && !flat_norm`）重算。**另：SPY/GOOGL/AAPL/XAUT/MSFT 不在 Binance USDT 永续列表，样本口径待核（§7）** |
| 4h 方向门与状态锁**口径重复**，死因记错账 | 46 个 `undetermined` 样本状态机全部有明确态，其中 45 个本来就会被 `htf_state_lock` 拦，真正独占误杀仅 1 个 |
| 腿门**有必要**，但实现有三处缺陷 | 关掉腿门候选 +53%（49→75）但期望 0.147→0.116；新增 26 个中 11 个被错记为 `range_score`（类型漂移） |
| tangle / 状态锁 / range_score 拦得对，不动 | tangle +0.011（n=273）、turn_bear −0.176、bull_dip −0.146、range_score −0.035 |
| 「观察 7 天再决策」**已有系统载体**（T5–T7 已实现） | `observation_tasks` 表 + `/api/observations` + 任务记录「待决策」区块 + 08:10 到期推送。**仍缺**：`metrics` 无每日自动刷新（只能手工 PATCH）、到期通知失败不重试、页面无取消入口（§3.1/§3.2） |
| TRUTHUSDT 未全平**已定位为非缺陷** | 该单 10-27 开仓（早于全平模式上线），走分批 60/20/80；线上 5 单全平口径两档数量精确覆盖全仓；07:21 一次页面保存把开关改回 false |
| 全平模式活口子已修复（T14f，2026-10-30） | TP2 因 `qty_tp2 < min_qty` 未挂上时，TP1 提升为整仓（100%），不再留「只能靠跟踪止损退出、且到不了 `TP2_HIT`」的 40% 尾仓 → 见 §2.6 |

---

## 1. 任务清单

### 1.1 第 0 批｜核实现状（不改代码）

| ID | 任务 | 做法 | 完成定义 |
|---|---|---|---|
| T0 | 核实线上 `quality_floor` | `SELECT scan_filters->>'quality_floor' FROM system_config;` 或 `GET /api/scans/config` | 知道实际值；>0 则 T11 提前到第 1 批 |

### 1.2 第 1 批｜逻辑修正（零行为漂移）

| ID | 任务 | 位置 | 改法 | 验收 |
|---|---|---|---|---|
| T1 ✅ | 腿门类型漂移 | `trigger.py:1105-1126`、`scanner.py:632-649` | **已实现（只改归因，不终止）**：腿门失败按 `trig_diag.stage == "leg_gate"` 归回 `leg_gate`，不再落入 `range_score`；拒绝动作与后续判定不变 | 候选数 ±0；`range_score` 中混入的腿门样本归零 |
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
| T8 ✅ | 走平门影子字段 | — | `htf_context` 输出 `atr_pct_4h` / `slope_atr_ratio` / 影子 `flat_abs` / `flat_norm`；scanner 写逐币 **`htf_evidence`**（`scanner.py:1081`、`1298`），不是 `details` | 登记「走平门归一」，due=+7d |
| T9 ✅ | 腿门分档结构化 | T2 | 后端 `leg_evidence`（bars / atr_mult / overshoot，`trigger.py:1158/1235`）；前端「无触发」页签加「腿深」列 + `legBand` 档位筛选（`FunnelPanel.tsx:136-207/288-296`） | 登记「腿门 min_atr 分档」，due=+7d |
| T10 ✅ | 4h 拒因分布 + 降级 | — | `/scans/funnels` 补 `htf_degraded`（`api/scan.py:318-322`）；`LatestFunnelPanel` 的 `HtfReasonBar` 展示 flat/tangle/undetermined/degraded 分布 | 登记「数据抖动」，due=+14d |

### 1.5 第 4 批｜行为变更（逐项上线，各自留观察期）

| ID | 任务 | 前置 | 改法 | 回退 |
|---|---|---|---|---|
| T11 ✅ | 质量分同源惩罚去重 | T0 | **已实现（2026-10-30）**：`technical.py` 命中 `box` 时跳过 `range`/`r2_low`（同源三罚只留最重的 `-15`），其余项照常；放量加分门槛改由 scanner 透传 `flt.breakout_vol_ratio` | 删优先级判断 / 不传该参数即回落 1.2 |
| T12 ✅ | 走平门按 ATR 归一 | T8 数据 | **已实现（2026-10-30）**：`flat = slope_pct < max(0.2%, 1.0×atr_pct_4h)`（`flat_norm` 为主，ATR 不可得回落 `flat_abs`）；`flat_abs` 仍随 evidence 落库 | **注意**：`k=0` 不等于恢复现网——影子下限 0.002 ≠ 现网 0.005；回退须显式把下限改回 0.005，或直接回退 `flat = flat_abs` |
| T13 ✅ | 4h 数据缺失 fail-open | T10 数据 | **已实现（2026-10-30）**，默认关=现网 fail-closed。开关 `scan_filters.htf_fail_open_enabled`（页面「方向过滤·4h缺失放行」）；①仅「数据」类失败放行（拉取失败/空数据/core 不足/data），flat/tangle/undetermined/adx 仍拒；②单轮预算 = 币池 5%（至少 1 个，原子扣减）；③连续 3 轮放行后冷却 1 轮（状态读上轮 `funnel_json.htf_fail_open.consecutive_rounds`）；④AI 事实包显式标注「4h背景缺失、不得据此加分」 | 开关置 False（默认） |

### 1.6 Bug 修复

| ID | 任务 | 位置 | 说明 |
|---|---|---|---|
| T14 | 止盈全平：配置变更审计 + 存量持仓提示（**非缺陷**） | `api/scan.py:1156-1183`、`TradingConfigPanel.tsx:150-157/527-540`、`system_config` | 见 §2 |
| T14f ✅ | 止盈全平：TP2 未挂上时 TP1 应提到 100% | `trade_engine.py` `_tp_order_quantities` | **已实施（2026-10-30）**：全平模式下 `qty_tp2 < min_qty` → TP1 取整整仓、TP2=0；详见 §2.6，用例 `tests/test_tp2_residual_guard.py` |

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

即：**是 07:21 那次页面保存把开关关掉的**（或用户确实点了关闭）。排查当时 `system_config` 还没有 `updated_at` 列（T14a 已补），无法进一步定位操作者；日志只证明该时刻发生了一次保存。

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
TP2 触发后（trade_engine.py:3486-3499 / 3532-3546）
  tp2_hit（_tp_filled_real：不在挂单列表 + algoStatus=FINISHED）
  → status = TP2_HIT → _enforce_full_exit_residual(...)（T14e，已实现）
     ├ 补平成功（raw.tp_full_exit=true、未补过、残仓占比 ≤10%）
     │    → market reduceOnly 平掉残仓，跳过跟进止损
     └ 否则 → _move_sl_trailing(...)
  残仓占比 >10% 只记 FULL_EXIT_RESIDUAL_SKIPPED（视为 TP2 后手动加仓，交人工）

未兜住的分支（T14f 待补）
  TP2 因 qty_tp2 < min_qty / 合计校验未过而没挂上 → raw.qty_tp2 = 0
  TP1 成交后停在 TP1_HIT（:3501-3513），而 TP1 数量仍按 p1=60% 挂
  → 40% 尾仓交给跟踪止损，状态永远到不了 TP2_HIT，T14e 永远不触发
```

`_is_tp_full_exit`（:2748）只在**仓位已归零后**用于推断 `exit_reason`，不参与平仓动作。

### 2.4 其他潜在根因（防御性排查，本次未触发）

> 先排除一个常见误判：全平模式下 `qty_tp2 = round_qty(qty − qty_tp1)`，而 `round_qty` 用 `Decimal` 精确 floor（`binance_trader.py:281-287`）。由于 `qty` 是交易所成交量、`qty_tp1` 是 floor 后的 step 整数倍，两者差值仍是 step 整数倍，**两档合计在数学上恒等于全仓**（线上 5 单已验证）。所以「没全平」不可能是数量分配问题，只能是成交侧或开仓快照。

| # | 根因 | 判定证据 | 本次是否命中 |
|---|---|---|---|
| R1 | **该单开仓时开关还没打开**（存量仓位不重挂 TP；开关只在开仓时快照） | `raw.tp_full_exit` 缺失或 `false` | **命中**（TRUTHUSDT，10-28 上线前开仓） |
| R2 | **TP2 条件单只部分成交**（薄盘/滑点：`FINISHED` ≠ 全额成交；reduceOnly 只平可用量） | `q1+q2 ≈ qty` 但仍有残仓；子单 `executedQty < qty_tp2` | 未命中 |
| R3 | **TP2 挂单被跳过**（`qty_tp2 < min_qty`，此时 `raw.qty_tp2=0`，只剩 TP1 在挂） | `raw->>'qty_tp2' = '0'` 且 `tp2_order_id IS NULL`；全平模式下 TP1 仍按 60% 挂 → 遗留 40% 尾仓 | 本次未命中（TRUTHUSDT 的 TP2 已成交）；**但这是 T14e 覆盖不到的活口子，见 T14f** |
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

| 子项 | 内容 | 针对 | 优先级 | 状态 |
|---|---|---|---|---|
| T14a | **开关变更可追溯**：`system_config.updated_at`（`models/system_config.py:232`）+ 配置 PUT 同事务写 `config_changes`（`api/scan.py:1156-1183`），`GET /api/scans/config/changes` 可查（字段/旧值/新值/来源 IP） | R4 | P0 | ✅ |
| T14b | **保存回读校验**：PUT 后 GET 比对关键开关，不一致时页面报错（`TradingConfigPanel.tsx:150-157`） | R4 | P1 | ✅ |
| T14c | **开关作用域提示**：面板已写「仅对新开仓生效」，打开开关时弹出**存量持仓提示** modal（`TradingConfigPanel.tsx:527-540`） | R1 | P1 | ✅ |
| T14d | **TP2 挂单缺失告警**：`tp2>0` 但 `tp2_order IS NULL` 时记 `TP2_SKIPPED` + `raw.tp2_skipped`（原因：`qty_below_min`/`tp_sum_not_ok`/`order_failed`，`trade_engine.py:205-236/1261-1264`） | R3 | P2 | 后端 ✅ / 前端「持仓卡片标注」未核 |
| T14e | **TP2 后残仓兜底**：`status→TP2_HIT` 且 `raw.tp_full_exit=true` 时校验交易所持仓，仍有残仓则市价 `reduceOnly` 补平（同单只补一次，`_enforce_full_exit_residual` `trade_engine.py:244-303`）；**残仓占比 >10% 只告警不平**（防 TP2 后手动加仓被误平） | R2/R5 | P2（防御） | ✅ |
| T14f | **TP2 未挂上时提升 TP1 至全平**：全平模式下 `qty_tp2 < min_qty`（TP2 挂不上）时，TP1 数量改为 100% 全仓（而不是沿用 p1=60%），避免 40% 尾仓只能靠跟踪止损退出、且永远到不了 `TP2_HIT` | R3 | P1 | ✅ 已实施（`_tp_order_quantities`，2026-10-30） |

> 排序变化说明：原计划以 **T14e（补平）** 为主修，实测证明代码路径正确，补平只是未来防御；当前真正的痛点是**「开关何时被改、改成了什么」完全不可追溯**，所以审计优先。T14f 是本轮复核新发现的活口子（R3 一旦命中，T14e 无法兜底），优先级高于其余防御项。

### 2.7 验收

1. 配置变更后 `system_config.updated_at` 与 `config_change` 审计可见（含旧值/新值）。
2. 页面对全平开关做 PUT→GET 比对，不一致时报错而不是「保存成功」。
3. 单测：`tp2>0` 但挂单被跳过 → 产生 `TP2_SKIPPED` 事件，且 `_is_tp_full_exit=false`。
4. 单测：TP2 后仍有残仓 → 补平一次且幂等（已实现；`tests/test_tp2_residual_guard.py`）。
5. 单测（T14f）：全平模式 + `qty_tp2 < min_qty` → `qty_tp1 == qty`（TP1 挂 100%），且不产生 `TP2_SKIPPED` 之外的尾仓。

---

## 3. 观察期机制（T5-T7）

### 3.1 表结构

```
observation_tasks
  id            PK
  name          例：走平门按 ATR 归一（影子）
  config_key    例：slope_flat_atr_mult
  started_at / due_at
  metrics       JSON，**当前只能手工 PATCH（没有每日刷新任务）**；如 {flat_abs_n, flat_norm_n, diff_avgR, by_symbol}
  decision_rule text，登记时写死，禁止到期再定
  status        pending | due | decided | cancelled
  decision / decided_at / note / created_at / updated_at
```

实现现状（`models/observation.py`）：四态已实现；`cancelled` 用于作废误登记（API `decide` 拒绝对已取消任务决策），但页面暂无取消入口。表上**没有**唯一约束（同名可重复登记）、没有指标定义快照（`metrics` 的键语义靠 `note` 口口相传）、没有 `notified_at`（无法做通知重试）。

### 3.2 到期扫描

- 挂在 `daily-attribution`（08:10）末尾，复用 `feishu_bot.send_app_card`，**不新增 beat 任务**。
- `pending` 且 `due_at <= now()` → `status='due'` → 推送「到期 + 判据 + 当前读数 + 去看哪儿」。
- **已知缺陷（待修）**：`check_due` 先 `commit` 把状态改成 `due`，再发送卡片；发送异常只 `logger.warning`，次日只扫 `pending` → 该条提醒**永久丢失**（页面「待决策」仍在，但飞书不补）。建议二选一：①发送成功后再 commit；②加 `notified_at`，每轮补发 `due` 且 `notified_at IS NULL` 的记录。

### 3.3 页面

| 落点 | 展示 |
|---|---|
| `ScanResult` →「任务记录」tab 顶部 | 「待决策 (N)」：名称 / 到期日 / 判据 / 当前读数 /「去决策」 |
| `MarketOverviewBar` | `due` 时红点 |
| 决策后 | 同表 `decided`，形成「改阈值 → 观察 → 结论」可回溯链路 |

### 3.4 三条初始观测项

| 名称 | 观察期 | 判据 | 到期看哪 |
|---|---|---|---|
| 走平门按 ATR 归一（影子） | 7 天 | 差集（`flat_abs && !flat_norm`）avgR ≥ +0.1R 且 n ≥ 30 | 最新筛选 →「4h 背景门」抽屉 → `htf_evidence` 的 `flat_abs/flat_norm/atr_pct_4h`；漏斗 `htf_evidence` |
| 腿门 `min_atr` 分档（0.3/0.5/0.8） | 7 天 | 0.3 档增量候选 avgR ≥ 0.1R 且胜率 ≥ 45%，n ≥ 30 | 「无触发」页签 → 腿深列 + 档位筛选 |
| 4h 数据缺失 fail-open | 14 天 | 降级占比 >1% 的轮次 ≥ 3，且该时段候选收益不为负 | 最新筛选吸顶条「4h 降级」（`HtfReasonBar`） |

> 判据可执行性（2026-10-29 复核）：
> - 三条判据都需补**最小样本量 / 置信区间**门槛，否则 n 很小时 avgR 波动会把噪声读成结论（§0 的 0.022R 差值即反例）。
> - 原判据中的「SPY/XAUT/MSFT 未被误放」**无法在 Binance USDT 永续样本里验证**（三者均不在该列表，§7 口径矛盾），已改为对差集整体判定；若要保留「大盘指数不被误放」，须另建非币安样本集。
> - 走平门没有独立抽屉，读数落在「4h 背景门」抽屉的逐币 `htf_evidence` 里，不要去找名为「走平门」的入口。

---

## 4. 批次与依赖

```
T0 ──> (floor>0?) ──> T11 提前
T5 ──> T6 / T7                     ← 提醒先跑起来（已上线）
T1 ──> T2 ──> T9                   ← 观测口径先修对（已上线）
T3、T4 可与 T1 并行（已上线）
T8 / T10 ──> 登记 observation ──> 7/14 天后 T6 推送 ──> 决策 T12 / T13
T11（质量分去重）不依赖观察期 ──> 可独立上线
T14a/T14b（配置审计 + 回读）+ T14c–T14f ──> 独立上线，不依赖筛选链
```

| 批次 | 内容 | 状态 |
|---|---|---|
| 第 1 批 | T0 + T5 + T6 | ✅ |
| 第 2 批 | T7 + T1/T2 + T3/T4 | ✅ |
| 第 3 批 | T14a + T14b（配置变更审计 + 保存回读） | ✅（含 T14c/T14d/T14e） |
| 第 4 批 | T8 + T9 + T10 + 登记 3 条观察项 | 代码 ✅；观察项登记仍待手工执行 |
| 第 5 批 | T11 | ✅ 已实施（2026-10-30） |
| 第 5 批 | T12 + T13 + T14f | ✅ 已实施（2026-10-30）：T12 走平归一、T13 fail-open（默认关）、T14f TP1 提满 |

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
| 2 | T1–T4 逻辑修正 | ✅ T1 腿门失败的「未进池」不再被记成 `range_score`（归回 `leg_gate` + 证据，**只改归因、不终止**）；T2 确认侧腿门标 `leg_gate`（不再被 `window_expired` 吞掉）；T3 `undetermined` + 状态机禁开归回 `htf_state_lock`（tangle 仍记 `gate_4h`）；T4 `direction_flip` 提到 `votes` 之前——3 票全反向时可达。**全部只改关卡名/说明，拒绝动作与候选数不变** |
| 3 | T8 走平门影子字段 | ✅ 影子 `flat_abs/flat_norm` + `atr_pct_4h/slope_atr_ratio` 落 `htf_evidence`；**T12 后走平判定已改 `flat_norm` 为主** |
| 3 | T9 腿门分档 | ✅ 后端 `leg_evidence`；前端「无触发」页签腿深列 + `legBand` 档位筛选 |
| 3 | T10 4h 拒因分布 | ✅ `/scans/funnels` 下发 `htf_degraded`；`HtfReasonBar` 展示 flat/tangle/undetermined/degraded |
| 3 | T11 质量分去重 | ✅ 已实施（2026-10-30）：`box` 命中跳过 `range`/`r2_low`；`volume` 门槛由 scanner 透传 `flt.breakout_vol_ratio`（默认 1.2） |
| 5 | T12 走平门归一 | ✅ 已实施（2026-10-30）：`flat_norm` 为主、ATR 不可得回落 `flat_abs`；`RULE_VERSION` bump 至 v2026-10-30 |
| 5 | T13 fail-open | ✅ 已实施（2026-10-30）：`htf_fail_open_enabled` 默认关；单轮预算 5% + 连续 3 轮冷却 + AI 事实包标注；`funnel_json.htf_fail_open` 观测块 |
| 6 | T14a–T14f | ✅ 配置审计 / 回读校验 / 存量提示 / `TP2_SKIPPED` / 残仓兜底（10% 上限）/ T14f TP1 提满 |

测试（记录时点）：`tests/test_observations.py`（5 例）、`tests/test_stage_attribution.py`（4 例）、`test_no_trigger_stage.py` 新增确认侧腿门用例；后端全量 **1363 passed**；前端 `tsc --noEmit` 通过。§8 另记「改动前基线 1388 passed」——两者是不同时点的快照，**都不能当当前基线**，以最新一次全量运行为准（`tests/test_scan_funnel.py` 近期单独运行 48 passed）。

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
- **口径警告（2026-10-29 复核）**：
  - 样本声明为「Binance USDT 永续」，但 §0/§3.4 引用了 SPY/GOOGL/AAPL/XAUT/MSFT——这些标的**不在币安 USDT 永续列表**。要么回放脚本混入了其它市场数据，要么占比结论来自另一份样本。使用这些结论前必须核对脚本输出的 symbol 清单；否则相关占比按「待核实」处理。
  - 回放配置（`quality_floor=0`、投票门开、24h ≥3M、取前 100）与线上（`quality_floor=60`、`direction_vote_gate_enabled=false`、`direction_conflict_gate_enabled=false`、≥2M）不同。**组间对比仍有效，但 PASS 基准 +0.164 不能当线上预期**，也不能直接用于 §3.4 的「avgR ≥ +0.1R」绝对判据（需用同期 PASS 基准做差）。

腿门 A/B（同一批事件，patch `_pullback_leg_gate` 恒真）：

| 组 | 候选 | avgR | 胜率 |
|---|---|---|---|
| 腿门开 | 49 | +0.147 | 46.9% |
| 腿门关 | 75 | +0.116 | 45.3% |
| 关后新增 26 个 | — | +0.058 | 42.3% |

---

## 8. 追加：深回踩的质量分口径（2026-10-29 实施）

属于 T11「同源惩罚去重」的前置小步——先处理重复计罚与形态性误伤，不动 `box` 优先级。

### 8.1 问题

线上 14 天触点分数分布（`quality_parts` 拆解）：

| 触点 | 候选 | 均分 | 主要扣分项 |
|---|---|---|---|
| retest | — | 90.7 | — |
| swing | — | 80.9 | — |
| ema21 | — | 76.4 | — |
| ema144 | 46 | 58.7 | 80% 命中 `risk_vote`（−6）、80% 命中 `range`（旧 −8） |

ema144 候选近半数被 `quality_floor=60` 拦掉，但已平仓 6 笔 `avg_pnl ≈ 0`（未见劣势）。
根因是两项惩罚衡量的是**回撤形态本身**，而非质量缺陷：

- `risk_vote`：深回踩到中期/长期均线时，1h 方向票与 SuperTrend 等趋势跟踪指标**必然滞后逆向**；
- `r2_low`：回踩腿本身会打断 24 根窗口的线性度，R² 必然偏低；
- `range`：scanner 已按 `range_score` 判「降权放行」，质量分再扣满额属同一事实二次计罚。

### 8.2 改法

| 位置 | 改法 |
|---|---|
| `technical.py` `DEEP_PULLBACK_REFS = ("ema55", "ema144", "ema21_4h")` | 深回踩触点常量，注释内联数据依据。`ema21_4h ≈ 1h EMA84` 比 ema55 更深，且 `TOUCH_ORDER` 中排在 ema55 之前——不纳入会让豁免取决于命中顺序 |
| `technical.py` `range` 项 | −8 → −4（半额；仅回踩路径可达，突破类仍由 scanner 硬拒） |
| `technical.py` `risk_vote` 项 | `touch_ref ∈ DEEP_PULLBACK_REFS` 时跳过 |
| `technical.py` `r2_low` 项 | 同上，深回踩不按「震荡段」折价 |
| `technical.py` 签名 | 新增 `touch_ref: Optional[str] = None`（默认 None，旧调用方零改动） |
| `scanner.py` 两处调用 | 当根/次根确认传 `trig["touch_ref"]`；pending upsert 传 `pending_op["touch_ref"]` |
| `config.py` `RULE_VERSION` | `v2026-10-27` → `v2026-10-29`：质量分决定谁能过 `quality_floor`，候选池与分数分布均变，前向统计不可混算 |
| `scripts/gate_soft_replay.py` | 补 `touch_ref` 透传（此前不传，回放分数系统性低于线上）；`range_score` 旧口径硬拒未动 |

豁免只覆盖两项形态性惩罚，其余维度（`structure_gate`、`box`、`htf`、`adx` 等）照常计分。

### 8.3 验收

- `tests/test_quality_score.py` 新增 4 例：`range` 半额 = −4、深回踩豁免 `risk_vote`（三触点 ema55/ema144/ema21_4h 全覆）、深回踩豁免 `r2_low`、其余惩罚不受影响；
- `tests/test_scan_funnel.py` 两例回踩降权断言的差值 8 → 4；
- 后端全量 `pytest` **1388 passed**（改动前基线）。

### 8.4 版本切点

`RULE_VERSION` 由 `v2026-10-27` 升至 **`v2026-10-29`**。质量分直接决定谁能过 `quality_floor`，候选池与分数分布均变——按 docs/14 §六.1「跨版本不混算」，前向统计窗口自本版本起算；`shadow` 卡的 `since_rule_version` 未改（其门槛 `v2026-10-13-8` 仍低于新版本，过滤不受影响，但判定样本已换成新口径候选池）。

**CARD-09（REC70 前向对照）例外**：该卡按日期过滤（`REC70_START=2026-10-11`，锁定期至 2026-11-08）而非 `rule_version`，本次不改。影响限于候选选择——分桶锚点 `ai_snapshot.recommendation` 未变，新增样本仅限原被 `quality_floor` 拦掉的 ema55/ema144/ema21_4h 候选（14 天 ema144 候选 46 条）。重置会把判定推迟到 11-26 并丢弃 18 天样本，故**不重置**；复查条件：若新放行候选占该卡开仓样本 >10%，再评估重置。

### 8.5 观察与回退

登记观察项（due=+7d，自 v2026-10-29 起算）：ema55/ema144/ema21_4h 候选的通过率、分档 PnL 与均分变化；若「新放行」样本 avgR 显著低于同期 PASS 基准（+0.164 口径），回退方式为：

- `DEEP_PULLBACK_REFS` 置空 → 两项豁免失效（`range` 半额仍保留）；
- `range` 项改回 −8 → 恢复满额计罚。

两者独立、均单行可回滚；任一回退都需再 bump `RULE_VERSION`，否则新旧口径会在同一版本组内混算。

> 后续审计（触点降格、基础分偏低、单一 floor、volume 阈值等 7 项）见 [质量分触点口径审计-2026-10-29](./质量分触点口径审计-2026-10-29.md)。

---

## 9. 评审修正与遗留（2026-10-29，已对照代码）

本节是文档评审的落账：哪些问题真实存在、哪些描述与代码不符、哪些证据不足以支撑结论。**上文已就地修正，本节只做索引与遗留清单。**

### 9.1 已确认存在的实现缺口

| # | 问题 | 代码证据 | 处理 |
|---|---|---|---|
| 1 | ~~T12 未实施：走平门仍用绝对阈值 `flat_abs` 硬拒~~ | `technical.py` `htf_context` | ✅ 2026-10-30 已修：`flat_norm` 为主、ATR 不可得回落 `flat_abs` |
| 2 | ~~T11 未实施：`box`/`range`/`r2_low` 仍叠加；`volume` 阈值硬编码 1.2~~ | `technical.py` `compute_quality_score` | ✅ 2026-10-30 已修：箱体命中跳过同源两项；`volume` 接 `flt.breakout_vol_ratio` |
| 3 | ~~T13 未实施：无 fail-open 开关，且文档缺失败分类/上限/冷却~~ | `scan_filters` / `scanner` / `ai_analyzer` | ✅ 2026-10-30 已实现（默认关，边界见 §1.5） |
| 4 | ~~**T14f**：全平模式 TP2 未挂上时 TP1 仍 60%，40% 尾仓无兜底~~ | `trade_engine._tp_order_quantities` | ✅ 2026-10-30 已修（§2.6） |
| 5 | 观察期 `metrics` 无自动刷新 | `services/observations.py` 仅 `create/decide/check_due`，无 beat 任务 | 手工 PATCH 维护（§3.1） |
| 6 | 到期通知先 commit 后发送，失败不重试 | `observations.py:82-92`（commit → send，异常只 warning） | 待修（§3.2 给了两种改法） |
| 7 | 无「关卡改名」历史切点标记 | 全仓无 `stage_attr_version`；T1/T3/T4 上线前后 `stage` 口径无法按版本切分 | 已知限制：跨上线日期的漏斗统计会混算，切分只能按部署时间人工划 |
| 8 | 观察项登记仍靠手工 curl | `ObservationTask(` 只出现在 `services/observations.py:37` | 运维动作，非代码缺陷 |

### 9.2 文档描述与代码不符（已就地修正）

| 位置 | 原文问题 | 修正 |
|---|---|---|
| 头部状态 | 「待排期，代码未改」 | 改为「部分已实现」，列出未实施项 |
| §0 第 1 行 | 「唯一确凿的误杀」，与 §8 的 `quality_floor` 误杀论证自相矛盾；+0.186 vs +0.164 仅差 0.022R | 降级为「信号级、待复核」，改用差集判据 |
| §0 观察期 | 「没有任何系统载体」 | 已实现 T5–T7；改为「载体已有，刷新/重试缺失」 |
| T1 改法 | 「腿门失败立即终止」 | 实际只改归因（`scanner.py:632-649`），拒绝动作不变 |
| T8 落点 | scanner 写逐币 `details` | 实际写 `htf_evidence`（`scanner.py:1081/1298`） |
| T9 | 只写后端字段 | 前端已加腿深列 + 档位筛选 |
| T10 | 待做 | 已实现 |
| T12 回退 | 「`k=0` 恢复绝对阈值」 | 影子下限 0.002 ≠ 现网 0.005，回退须显式改回 0.005 |
| §2.6 备注 | 「原计划以 T14a（补平）为主修」 | 补平是 T14e |
| §3.1 | 状态只写三态；`metrics` 写「每日刷新」 | 实际含 `cancelled`；`metrics` 只能手工更新 |
| §4 | 「`7/14 天后决策 T11/T12/T13`」 | T11 不依赖观察期，可独立上线 |
| §6 测试数 | 1363 与 §8 的 1388 并列 | 标注为不同时点快照，均非当前基线 |

### 9.3 证据不足、需复核后再决策

| 结论 | 问题 |
|---|---|
| flat 拒单 avgR +0.186「显著优于」PASS +0.164 | 差值 0.022R ≈ 0.3 SE，不构成显著；需按差集（`flat_abs && !flat_norm`）重跑并给出 n/CI |
| SPY/GOOGL/AAPL/XAUT/MSFT 走平占比 | 不在 Binance USDT 永续列表，与 §7 样本口径矛盾；需核对回放 symbol 清单 |
| 腿门「均值稀释」 | avgR 0.147→0.116 同时候选 +53%，未评估组合容量/相关性；不能只凭 avgR 判「腿门必要」 |
| tangle / 状态锁「拦得对」 | 单期 20 天、裸方向回放，未叠加触点/金K/质量分/AI 配额（§7 局限） |

### 9.4 建议的执行顺序

1. ~~**T14f**（全平口子）~~ ✅ 已实施（2026-10-30）。
2. 观察期通知补「先发后 commit」或 `notified_at` 重试；三条观察项登记脚本化。
3. ~~T11（质量分去重）独立上线~~ ✅ 已实施（2026-10-30）。
4. ~~按 §3.4 补 min_n/CI 后重跑 flat 差集复核 → 决定 T12~~ —— T12 已实施；仍需补 min_n/CI 复核 avgR 判据，作为去留依据。
5. ~~T13 先补失败分类/上限/冷却，再评估~~ ✅ 已实施（默认关）；观察期数据够后再评估是否开启。
