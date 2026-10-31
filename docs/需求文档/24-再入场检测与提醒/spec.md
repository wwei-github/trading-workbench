# 再入场检测与提醒（仅提醒，不自动开单）

## 0. 结论与定位

- 本需求实现「止损出场后、结构仍完好、价格回到原开仓价」的**检测 + 飞书提醒**，不改任何交易执行链路。
- **硬约束（不可在实现中隐藏）**：
  - 只提醒，不下单、不改仓位、不建 `TradeRecord`、不挂条件单。
  - 前端与飞书卡片必须出现免责声明：「仅提醒，不会自动开单；回测该再入场腿期望为负」。
  - **不 bump `RULE_VERSION`**、不改扫描/开仓/风控任何判定。
  - 仅正式网（`testnet=false`）建监控；测试网不产生任何监控行。
- 背景依据（不得在卡片上改写为「有效」）：CARD-07 `structure_ok` n=65；#12 预注册回放 FAIL（ΔR 均值 −0.115R、Σ −7.50R、敏感性 6/9）；且回放核算修正 θ 后 0/0.5/1.0R 三档全为负。故本功能定位为**实验性观察提醒**。
- 建监控两道闸（2026-10-31 增补）：① **止损根长影线**——止损那根 1h 的反向影线 ≥2×实体且 ≥40% 全幅（插针止损后收回才值得再看）；② **结构完好**（EMA 排列 / ADX≥20 / R²≥0.5）。任一不过落终态，不进入监控。
- 巡检口径（2026-10-31 增补）：**每小时 :06 一次**，只用**已收盘 1h** 的 high/low 判断是否越过触发价（多头 high≥触发价、空头 low≤触发价），不再读 ticker 现价；止损那根自身不参与触发（根内先后顺序不可知）。

## 1. 数据模型

新增 `backend/app/models/trade.py::ReentryWatch`（表 `reentry_watches`），随 `Base.metadata.create_all` 自动建表，无需手写 ALTER（新表）。

字段（全部非空除注明）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `id` | UUID PK | |
| `source_trade_id` | UUID | 来源止损单 `TradeRecord.id`；**唯一约束** `uq_reentry_watch_source` |
| `symbol` | String(32) index | |
| `direction` | String(8) | long / short |
| `account_key` | String(20) nullable | 来源单账号 |
| `testnet` | Boolean default False | 恒 False（仅正式网） |
| `rule_version` | String(16) nullable | 来源单快照，仅展示 |
| `entry_price` | Numeric(20,8) | 原开仓价 |
| `stop_initial` | Numeric(20,8) | 原单初始止损（OPEN 事件快照） |
| `r_unit` | Numeric(20,8) | `abs(entry_price - stop_initial)` |
| `theta_r` | Numeric(10,4) | 触发阈值（R），默认 `settings.REENTRY_TRIGGER_THETA_R`=0 |
| `trigger_price` | Numeric(20,8) | long: `entry + θ*r_unit`；short: `entry - θ*r_unit` |
| `status` | String(16) default `watching` index | watching / triggered / expired / structure_broken / **wick_miss** / superseded / dismissed |
| `structure_json` | JSON | 建监控时结构快照 `{ema_ok, ema144_used, adx, adx_ok, r2, r2_ok}` + `stop_wick={ok,pct,mult,shadow,bar_open_ms}`（止损根影线读数） |
| `current_price` | Numeric(20,8) nullable | 最近一根已收盘 1h 的收盘价 |
| `last_checked_at` | DateTime nullable | 最近巡检时刻（增量补扫的起点） |
| `triggered_at` | DateTime nullable | 触发时刻 |
| `notified_at` | DateTime nullable | 飞书卡发送时刻（幂等：非空不再发） |
| `expires_at` | DateTime index | `closed_at + settings.REENTRY_WINDOW_HOURS` |
| `resolve_reason` | String(32) nullable | `manual` / `ttl` / `structure_break` / `open_position` / `structure_broken_at_close` / **`stop_wick_weak`** |
| `resolved_at` | DateTime nullable | 终态时刻 |
| `created_at` / `updated_at` | DateTime | 惯例 |

索引：`ix_reentry_watch_symbol_status(symbol,status)`、`ix_reentry_watch_due(status,expires_at)`。

生命周期：
- `watching` → `triggered`（已收盘 1h 越过触发价，**发且仅发一张卡**）
- `watching` → `expired`（到期未触发）
- `watching` → `structure_broken`（巡检时最新已收盘 1h 结构不再完好）
- `watching` → `superseded`（同 symbol 同账号已有在跑仓位）
- `watching|triggered` → `dismissed`（人工忽略）
- 建监控时止损根影线不足 → 直接 `wick_miss`，`resolve_reason=stop_wick_weak`（优先级高于结构）。
- 建监控时影线合格但结构已不满足 → 直接 `structure_broken`，`resolve_reason=structure_broken_at_close`。

## 2. 配置

`backend/app/config.py::Settings` 新增四项（env 可覆盖，不改页面配置）：

```python
REENTRY_TRIGGER_THETA_R: float = 0.0   # 触发阈值：0=回到开仓价
REENTRY_WINDOW_HOURS: int = 24         # 监控窗口
REENTRY_WICK_BODY_MULT: float = 2.0    # 止损根反向影线 ≥ N×实体
REENTRY_WICK_RANGE_MIN: float = 0.4    # 止损根反向影线 ≥ M 全幅
```

## 3. 后端服务 `backend/app/services/reentry.py`（新增）

复用口径常量：`from app.services.shadow import REENTRY_ADX_MIN, REENTRY_R2_BARS, REENTRY_R2_MIN, REENTRY_LOOKBACK_H`；指标引擎 `calc_ema` / `analyze_adx` / `linear_regression`。

### 3.1 `compute_structure(bars: list[list], direction: str) -> Optional[dict]`
- `bars` 为按时间升序的 **1h K 线**（调用方负责只传已收盘根，或传含形成根并自行截断；本函数不截断）。
- 与 `shadow.compute_reentry_precond` 完全同口径：
  - EMA：`calc_ema(closes,21/55/144)`；long `e21>e55`（e144 可用再加 `e55>e144`），short 对称。
  - ADX：`analyze_adx(bars, period=14)`（内部剔尾，与 shadow 一致）；`adx>=REENTRY_ADX_MIN`。
  - R²：最近 `REENTRY_R2_BARS` 根收盘线性回归；`r2>=REENTRY_R2_MIN`。
- 返回 `{ema_ok, ema144_used, adx, adx_ok, r2, r2_ok, structure_ok}`；`len(bars)<66` 或 EMA 不可算返回 `None`（数据不可判，不建监控）。

### 3.1b `compute_stop_wick(bars, direction, closed_ms) -> Optional[dict]`
- 定位「止损根」：`bar_open_ms = floor((closed_ms−1)/3_600_000)*3_600_000`（**整点边界归前一根**，止损常发生在根收盘瞬间）。
- 多头止损（向下跌穿）看**下影** `min(open,close) − low`；空头看**上影** `high − max(open,close)`。
- 合格 = `shadow ≥ REENTRY_WICK_BODY_MULT × body` 且 `shadow ≥ REENTRY_WICK_RANGE_MIN × range`；`body≈0` 时倍数记 `None`（前端显示「实体≈0」）。
- 返回 `{ok, pct(影线占全幅), mult(影线/实体或 None), shadow, bar_open_ms}`；找不到止损根或 `range<=0` 返回 `None`（不可判，不建监控）。

### 3.2 `register_reentry_watches(db, now=None, limit=20) -> int`
入选（每轮 ≤limit，按 `closed_at` 升序，先来先建）：
- `TradeRecord.status=='CLOSED'`、`testnet is False`、`excluded is False`、`exit_reason=='sl'`
- `closed_at >= now - REENTRY_WINDOW_HOURS` 且 `closed_at <= now`
- 无既有 `ReentryWatch.source_trade_id`
- `entry_price>0` 且 OPEN 事件 `detail.stop_loss>0`（取初始止损，同 `shadow.tag_reentry_backfill` 口径）
步骤：
1. `closed_ms = closed_at` 毫秒；`bar_open_ms = 包含 closed_at 的 1h 开盘`；`fetch_end_ms = bar_open_ms + 3_600_000`。
2. `ExchangePool().get_klines_range(symbol,'1h', REENTRY_LOOKBACK_H+REENTRY_WINDOW_HOURS, fetch_end_ms)`（**拉到止损根收盘为止**）。
3. `wick = compute_stop_wick(klines, direction, closed_ms)`；`None` → 跳过（不落行）。
4. `struct_bars=[k for k in klines if int(k[0])+3_600_000 <= fetch_end_ms]`（含止损根），`compute_structure(struct_bars, direction)`；`None` → 跳过。
5. `structure['stop_wick'] = wick`（同一份快照落库，避免加列迁移）。
6. `trigger_price = entry + dir*θ*r_unit`（dir=long→+1, short→−1）。
7. 落 `ReentryWatch`：`wick.ok is False` → `wick_miss` + `resolved_at=now` + `resolve_reason='stop_wick_weak'`；否则 `structure_ok` → `watching`；否则 `structure_broken` + `resolved_at=now` + `resolve_reason='structure_broken_at_close'`。
8. 逐笔 `db.commit()`；单笔异常 rollback + `logger.debug` 吞掉，不影响其他笔。
9. 返回新建行数。

### 3.3 `sweep_reentry_watches(db, now=None) -> dict`
先 `register_reentry_watches`，再扫 `status in ('watching','triggered')`。预加载同账号在跑 symbol 集合（`TradeRecord.status in ('OPENED','TP1_HIT','TP2_HIT')`）。逐条：
1. `ExchangePool().get_klines(symbol,'1h',400)`（按小时桶缓存）；失败 → 记 debug、**保持 watching**，本条跳过。
2. `closed = klines[:-1]`（丢弃形成中根，只认已收盘 1h）。
3. **触发优先**，用已收盘根扫描窗口 `(last_checked_at, now]`（首次从 `closed_at` 起算，可补扫漏跑的小时）：
   - 跳过**止损那根本身**（`bar_open_ms <= 止损根 open`：根内先后顺序不可知）
   - 跳过 `close_time <= last_checked_at`（上轮已扫）与 `close_time > now`（未收盘）
   - `close_time > expires_at` → 窗口外，之后的根一律不认（break）
   - long `high >= trigger_price` / short `low <= trigger_price` → `status='triggered'`、`triggered_at=now`、`db.commit()`，随后 `notify_triggered(watch)`（发送失败只记 warning，`notified_at` 仅在发送成功时写；发送失败下轮重试，仍只发一张成功卡）。
4. 无论是否触发，刷新 `current_price=closed[-1] 收盘`、`last_checked_at=now`，并用同一批 `closed` 重算结构（`None` 则保留旧快照）；更新结构时必须**保留 `stop_wick`**。
5. 未触发则 `now >= expires_at` → `expired` + `ttl`。
6. 否则同 symbol 同账号已有在跑仓 → `superseded` + `open_position`。
7. 否则 `structure_ok is False` → `structure_broken` + `structure_break`。
8. 每条异常 rollback + debug 吞掉。
返回 `{registered, triggered, expired, broken, superseded}` 计数。

### 3.4 `build_reentry_card(watch) -> dict` + `notify_triggered(watch)`
卡片（`send_feishu_card(card, testnet=False)`，`services/notify.py::send_feishu_card`）：
- header：`template='orange'`，标题 `再入场提醒 {symbol} {long/short}`。
- elements 逐行 lark_md：原入场/原止损、触发价(θR)、**1h 收盘价**、原单平仓时间、来源交易 id、**止损根影线**（`下影/上影 31.00×实体｜95% 全幅 ✓`，无快照时 `—`）、结构读数（EMA/ADX/R² 各项 ok 标记）。
- 末尾 note：「仅提醒，不会自动开单；回测该再入场腿期望为负」。
- 结构异常/字段缺失用 `-`，不编造。

## 4. 定时任务

- `backend/app/tasks/trade_tasks.py` 新增 `@celery_app.task(name="app.tasks.trade_tasks.watch_reentry_watches")`，函数体：`SessionLocal()` → `sweep_reentry_watches(db)` → 关闭；整体异常 `logger.exception` 吞掉；不写 task_log（同 `watch_pending_entries`）。
- `backend/app/celery_app.py` beat 新增（**2026-10-31 由每分钟改为每小时 :06**，错开整点等 1h 收盘落定）：
  ```python
  "reentry-watch": {
      "task": "app.tasks.trade_tasks.watch_reentry_watches",
      "schedule": crontab(minute=6),
  },
  ```
- 同时段降频：`trade-review` 改为 `crontab(minute=10, hour="*/4")`、`ai-attribution` 改为 `crontab(minute=20, hour="*/4")`；`REVIEW_SHARDS_PER_RUN` 6→12（保持 24h 轮完 48 个分片）。
- 不加开仓锁（只读行情 + 只写自己的 watch 表）。

## 5. API

`backend/app/schemas/trade.py` 新增 `ReentryWatchOut`：表字段全透出 + `seconds_left: Optional[int]`（watching 时 `max(0, (expires_at-now).seconds)`，其余 0）+ 结构读数从 `structure_json` 展平为 `ema_ok/ema144_used/adx/adx_ok/r2/r2_ok/structure_ok` + 止损根影线展平为 `stop_wick_ok/stop_wick_pct/stop_wick_mult`（`structure_json["stop_wick"]`，无快照时 None）。

`backend/app/api/trades.py`：
- `GET /api/trades/reentry-watches?status=all&limit=200` → `list[ReentryWatchOut]`，`created_at desc`；`status` 可选（`watching/triggered/...` 精确过滤，`all` 或不传=全部）。
- `POST /api/trades/reentry-watches/{watch_id}/dismiss` → `{ok, id, status}`：仅 `watching/triggered` 可忽略，置 `dismissed` + `manual` + `resolved_at`；终态重复调用返回 400「该监控已结束」。

## 6. 前端（页面入口与呈现）

### 6.1 入口位置

- 只在**合约模式**下出现。`ScanResult.tsx` 的合约 Tabs 顺序：
  `扫描结果 → 待确认池 → 关注列表 → 搜索币种 → 策略提示词 → 战法 → 资金分析 → 交易记录 → **再入场提醒** → 复盘 → 资金划转 → 任务 → 系统日志 → CODE WIKI`。
- Tab key `reentry`，label `再入场提醒`；内容容器沿用现有写法：
  ```tsx
  { key: 'reentry', label: '再入场提醒',
    children: <div style={{height:'100%',overflowY:'auto',overflowX:'hidden'}}><ReentryWatchPanel /></div> }
  ```
- 现货模式不显示。

### 6.2 面板布局（`ReentryWatchPanel.tsx`，仿 `PendingPoolPanel`）

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⚠ 仅提醒，不会自动开单；回测该再入场腿期望为负                              │  ← 固定 Alert（warning，不可关闭）
├──────────────────────────────────────────────────────────────────────────┤
│ [ 监控中 2 ] [ 已触发 1 ] [ 已结束 15 ]                        [刷新]      │  ← Segmented + 手动刷新
├────────┬────┬──────────┬──────────────┬─────────┬────────┬────────┬────────┬──────┤
│ 币种   │方向│ 状态     │原入场 / 止损 │触发价   │当前价  │距触发  │ 结构   │操作  │
├────────┼────┼──────────┼──────────────┼─────────┼────────┼────────┼────────┼──────┤
│ BTCUSDT│多  │监控中    │68000 / 67500 │68000    │67400   │-0.88%  │✓ ✓ ✓  │忽略  │
│ ETHUSDT│空  │已触发    │3200 / 3260   │3200     │3215    │+0.47%  │✓ ✓ ✓  │忽略  │
│ SOLUSDT│多  │结构破坏  │150 / 147     │150      │148     │-1.33%  │✓ ✗ ✓  │ -    │
└────────┴────┴──────────┴──────────────┴─────────┴────────┴────────┴────────┴──────┘
  图例：状态 Tag 可带 Tooltip；结构列 = EMA排列 / ADX≥20 / R²≥0.5 三项 ✓✗；
  影线列 = 止损根反向影线（倍数/全幅）✓✗（表格实际列不止上图，另含止损根影线/剩余时间/最近检查）
```

- 顶部 `Alert`：`type="warning"`、`showIcon`、文案固定为「仅提醒，不会自动开单；回测该再入场腿期望为负」。
- `Segmented` 三项，计数为前端按状态过滤后的行数：
  - `监控中` = `watching`
  - `已触发` = `triggered`
  - `已结束` = `expired / structure_broken / wick_miss / superseded / dismissed`
- 数据源一次拉 `reentryWatches('all', 200)`，前端按上面分组过滤（与 `PendingPoolPanel` 同模式，不逐状态请求）。

### 6.3 列定义

| 列 | 内容 | 说明 |
|---|---|---|
| 币种 | 可点击的 Link | 点击打开 K 线弹窗（见 6.5） |
| 方向 | `看多`/`看空` Tag | 用 `schemeTag(colorScheme)` 与全站配色一致 |
| 状态 | Tag：监控中=processing / 已触发=success / 已过期=default / 结构破坏=volcano / 影线不足=default / 已有仓位=default / 已忽略=default | Tooltip 解释该终态原因（影线不足：需 ≥2×实体且 ≥40% 全幅） |
| 原入场 / 止损 | `entry_price / stop_initial`，6 位有效数字 | 单行两值 |
| 触发价 | `trigger_price`，副文本 `θ={theta_r}R` | θ=0 时显示「回到开仓价」 |
| 当前价(1h收盘) | `current_price`；未巡检过显示 `-` | 最近一根已收盘 1h 的收盘价（不是 ticker） |
| 距触发 | `(current_price - trigger_price)/trigger_price × 100`，带符号 2 位小数 | 多空都用带符号值：多单为正=已越过触发价；空单为负=已越过 |
| 止损根影线 | `下影 31.00× · 95% ✓`（空头 `上影`；实体≈0 显示「实体≈0」） | Tooltip 说明门槛 ≥2×实体且 ≥40% 全幅；无快照显示 `-` |
| 结构 | `EMA✓ ADX✓ R²✓` 三个短标记 | Tooltip 展示 `EMA21>EMA55(>EMA144)`、`ADX 28.4 ≥20`、`R² 0.62 ≥0.5` 原始读数 |
| 剩余时间 | `watching` 行倒计时（`seconds_left` 前端每秒递减，显示 `23:41:12` / `剩 1h 5m`）；终态显示 `resolved_at` 时刻 | 到期后由后端置 expired |
| 操作 | `watching/triggered` 显示「忽略」；终态显示 `-` | 二次确认：`Modal.confirm`「忽略后不再提醒该笔，历史记录保留」 |

### 6.4 交互、刷新与空态

- 自动刷新：`useEffect` **5 分钟**定时 + 手动「刷新」按钮；页面切走即清理定时器。
- 刷新频率说明：后端每小时 :06 巡检 ⇒ 触发的提醒最迟约 1 小时 6 分钟出现在页面；飞书卡由后端触发时即时发送，不等页面。
- 忽略：调用 `dismissReentryWatch(id)` 后局部刷新；失败由全局 axios 拦截器提示。
- 空态文案：
  - 监控中：`暂无监控中的止损单——正式网止损出场、止损根带长影线且结构完好时会自动出现（整点收盘后 6 分钟更新）`
  - 已触发：`暂无已触发提醒`
  - 已结束：`暂无已结束记录`
- 不做浏览器弹窗/声音提醒；唯一主动提醒渠道是飞书卡（避免与现有通知重复）。

### 6.5 K 线弹窗（可选但按此实现）

- 点击币种 → `Modal`（width 1200，`destroyOnClose`），内嵌 `<KlineChart symbol={r.symbol} limit={500} hitTime={closed_at} hitDirection={direction} />`。
- 作用：回看该笔止损单的出场位置与再入场触发位；不新增图表能力。

### 6.6 前端文件改动清单

1. `frontend/src/types/index.ts`：新增 `ReentryWatch`（字段同 `ReentryWatchOut`，`direction: 'long'|'short'`，`status` 七态枚举含 `wick_miss`，含 `stop_wick_ok/pct/mult`）。
2. `frontend/src/api/scan.ts` `trades` 增加：
   - `reentryWatches: (status='all', limit=200) => api.get<ReentryWatch[]>('/trades/reentry-watches',{params:{status,limit}}).then(r=>r.data)`
   - `dismissReentryWatch: (id) => api.post<{ok:boolean;id:string;status:string}>(`/trades/reentry-watches/${id}/dismiss`).then(r=>r.data)`
3. 新增 `frontend/src/components/ReentryWatchPanel.tsx`（按 6.2–6.5）。
4. `frontend/src/pages/ScanResult.tsx`：按 6.1 插入 Tab 并 import 组件。

## 7. 测试

新增 `backend/tests/test_reentry_watch.py`（参考 `test_reentry_shadow.py` / `test_pending_ops_db.py` 的 fixture 风格）：
1. `compute_structure`：多头强趋势样例 → `structure_ok=True`；空头同口径对称；不足 66 根 → `None`。
2. `compute_stop_wick`：止损根长下影 → `ok=True`；下影≈0 → `ok=False`；空头上影对称；整点边界归前一根；K 线未覆盖止损根 → `None`。
3. `register_reentry_watches`（monkeypatch `ExchangePool.get_klines_range`）：
   - 正式网 `sl` 单 + 影线/结构都过 → 一行 `watching`，`trigger_price==entry`，`expires_at==closed_at+24h`，`structure_json.stop_wick.ok=True`。
   - 影线不足 → 一行 `wick_miss`，`resolve_reason='stop_wick_weak'`（即使结构仍 OK）。
   - 影线过但结构 broken → 一行 `structure_broken`，`resolve_reason='structure_broken_at_close'`。
   - 测试网单 / 非 `sl` 单 / `excluded` 单 → 0 行；无 OPEN 止损快照 → 跳过；K 线不含止损根 → 跳过。
   - 同一 `source_trade_id` 连跑两次 → 仍 1 行（唯一约束）。
4. `sweep`（monkeypatch `ExchangePool.get_klines` + `send_feishu_card`，**不 mock ticker**）：
   - 已收盘 1h high/low 越过触发价 → `triggered`，且 `send_feishu_card` 仅调用 1 次、卡片 note 含免责声明；再跑一轮不重复发。
   - 止损那根自身越过触发价 → 不触发。
   - 窗口外（根收盘 > expires_at）越过 → 不触发，按到期收敛。
   - `last_checked_at` 已覆盖的根不重扫；更早的 `last_checked_at` 会补扫。
   - 到期未触发 → `expired/ttl`；同 symbol 在跑仓 → `superseded/open_position`；最新结构 broken → `structure_broken/structure_break`。
   - 巡检更新结构时保留 `stop_wick` 快照；K 线拉取异常 → 保持 `watching`。
5. API：`GET` 列表字段与 `seconds_left`（含 `stop_wick_*` 展平）；`POST dismiss` 正常置 dismissed、终态再调返回 400。
6. 回归：不触碰 `RULE_VERSION`、不产生 `TradeRecord`（断言表行数不变）。

## 8. 验证命令

```bash
cd backend && python -m pytest tests/test_reentry_watch.py tests/test_reentry_shadow.py tests/test_hold_warnings.py tests/test_pending_sweep_shortcut.py -q
cd frontend && npx tsc --noEmit
```

## 9. 明确不做

- 不自动开单、不生成 `PendingEntryOrder`、不改 SL/TP。
- 不写回 `TradeRecord`、不新增影子标记、不改 CARD-07 既有统计。
- 不做测试网监控、不做页面可编辑 θ/窗口（env 可改）。
- 不推送、不部署、不做服务器验证。本地 conventional commit。
