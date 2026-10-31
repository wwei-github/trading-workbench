# 再入场检测与提醒 — 任务清单

> 前置：用户批准 `spec.md` 后按顺序执行；每步完成即勾选。
> 约束：只提醒不下单；仅正式网；不 bump RULE_VERSION；本地提交不推送。
> **2026-10-31 第二轮增补**：再入场改每小时 :06、触发只认已收盘 1h、新增止损根长影线闸、
> 复盘/归因降为 4 小时。T3/T5/T6 中被增补覆盖的条目以文末「增补」节为准。

## T1 配置与模型
- [x] `backend/app/config.py`：新增 `REENTRY_TRIGGER_THETA_R: float = 0.0`、`REENTRY_WINDOW_HOURS: int = 24`。
- [x] `backend/app/models/trade.py`：新增 `ReentryWatch`（字段/索引/唯一约束按 spec §1 逐项）。
- [x] `python -c "from app.database import init_db; init_db()"` 验证建表无报错（单测 `Base.metadata.create_all` 已覆盖建表）。

## T2 服务层
- [x] 新增 `backend/app/services/reentry.py`：
  - [x] `compute_structure(bars, direction)`（与 shadow 同口径，复用阈值常量）。
  - [x] `register_reentry_watches(db, now, limit)`（仅正式网 sl 单；结构 gate；θR 触发价；逐笔提交/吞异常）。
  - [x] `sweep_reentry_watches(db, now)`（触发优先 → 到期 → 在跑仓 → 结构破；计数返回）。
  - [x] `build_reentry_card(watch)` + `notify_triggered(watch)`（仅成功发送写 `notified_at`，卡片含免责声明）。
- [x] `sweep` 中取价失败不得改变状态；单条异常不得中断整轮。

## T3 定时任务
- [x] `backend/app/tasks/trade_tasks.py`：新增 `watch_reentry_watches` celery 任务（SessionLocal → sweep → close，异常吞）。
- [x] `backend/app/celery_app.py`：beat 新增 `reentry-watch`，`crontab(minute="*")`（2026-10-31 改为每小时 :06，见增补 A4）。

## T4 API
- [x] `backend/app/schemas/trade.py`：新增 `ReentryWatchOut`（含 `seconds_left` 与结构展平字段）。
- [x] `backend/app/api/trades.py`：
  - [x] `GET /api/trades/reentry-watches`（status 可选，limit 默认 200，created_at desc）。
  - [x] `POST /api/trades/reentry-watches/{id}/dismiss`（仅 watching/triggered 可忽略；终态 400）。

## T5 前端
- [x] `frontend/src/types/index.ts`：新增 `ReentryWatch`（六态 status + 结构展平字段）。（2026-10-31 扩为七态 + 影线字段，见增补 A7）
- [x] `frontend/src/api/scan.ts`：`trades.reentryWatches` / `dismissReentryWatch`。
- [x] 新增 `frontend/src/components/ReentryWatchPanel.tsx`：
  - [x] 顶部固定 `Alert`：「仅提醒，不会自动开单；回测该再入场腿期望为负」。
  - [x] `Segmented` 监控中/已触发/已结束（前端分组过滤，一次拉 all）。
  - [x] 表格列：币种(可点 K 线)/方向/状态 Tag+Tooltip/原入场·止损/触发价(θR)/当前价/距触发%/结构 ✓✗/倒计时/忽略。
  - [x] 60s 自动刷新 + 手动刷新；忽略二次确认；三组空态文案。（2026-10-31 改 5 分钟刷新 + 影线列，见增补 A7）
  - [x] K 线弹窗：`KlineChart`（hitTime=created_at、hitDirection）。注：后端未透出 `closed_at`，用监控行 `created_at`（平仓后分钟级建立）近似。
- [x] `frontend/src/pages/ScanResult.tsx`：合约 Tabs 在「交易记录」后插入「再入场提醒」（仅合约模式）。

## T6 测试
- [x] 新增 `backend/tests/test_reentry_watch.py`，覆盖 spec §7 全部用例（17 例；2026-10-31 重写为 29 例，见增补 A8）。
- [x] `cd backend && python -m pytest tests/test_reentry_watch.py tests/test_reentry_shadow.py tests/test_hold_warnings.py -q` → 32 passed（2026-10-31 复跑 47 passed，含挂单短路用例）。
- [x] `cd frontend && npx tsc --noEmit` → 无错误。

## T7 提交
- [x] `git status` / `git diff` 自查（确认无 RULE_VERSION、无执行链路改动）。
- [x] 本地提交：`feat(reentry): 止损后再入场检测与飞书提醒（仅提醒不下单）`。
- [x] 不推送、不部署。

## 2026-10-31 增补（第二批：小时收盘确认 + 止损根长影线闸 + 降频）
> 用户拍板：再入场巡检由每分钟改为每小时 :06；触发不再用 ticker，只用已收盘 1h 的
> high/low；建监控增加「止损那根 1h 反向长影线」闸（≥2×实体 且 ≥40% 全幅）。
> 硬约束不变：只提醒不下单、仅正式网、不 bump RULE_VERSION。

### A1 配置
- [x] `backend/app/config.py`：新增 `REENTRY_WICK_BODY_MULT=2.0`、`REENTRY_WICK_RANGE_MIN=0.4`。

### A2 服务：止损根长影线闸
- [x] 新增 `_bar_open_ms(ts_ms)`（`(ms-1)//1h`，整点边界归前一根）。
- [x] 新增 `compute_stop_wick(bars, direction, closed_ms)`：多头看下影、空头看上影；
      `shadow ≥ N×body` 且 `shadow ≥ M×range`；`body≈0` → `mult=None`；
      缺止损根或 `range<=0` → `None`（不可判，不建监控）。
- [x] `register_reentry_watches`：`fetch_end_ms = 止损根 open + 1h` 拉 K 线到止损根收盘；
      `structure_json["stop_wick"]` 快照；影线不过 → `wick_miss` + `stop_wick_weak`
      （优先级高于结构）；影线过但结构破 → `structure_broken`。
- [x] 删除 `_trader_for`（建监控不再需要 `BinanceTrader`）。

### A3 服务：sweep 改小时收盘触发
- [x] 去掉 ticker：`ExchangePool().get_klines(symbol,'1h',400)` → `closed=klines[:-1]`。
- [x] 触发扫描 `(last_checked_at, now]`（首次从 `closed_at` 起算，漏跑小时天然补扫）；
      跳过止损那根（根内先后顺序不可知）；跳过未收盘根；窗口外 `break`。
- [x] `current_price` = 最近一根已收盘 1h 收盘价；`last_checked_at=now`。
- [x] 巡检刷新结构时**保留** `structure_json.stop_wick`。

### A4 排班（`backend/app/celery_app.py`）
- [x] `reentry-watch` → `crontab(minute=6)`（每小时，错开整点等 1h 收盘落定）。
- [x] `trade-review` → `crontab(minute=10, hour="*/4")`。
- [x] `ai-attribution` → `crontab(minute=20, hour="*/4")`（排在 review 后，避免 LLM 并发）。
- [x] `backend/app/tasks/review_tasks.py`：`REVIEW_SHARDS_PER_RUN` 6→12（48 分片 / 12 × 4h = 24h 一圈）。

### A5 模型 / API
- [x] `ReentryWatch` 注释：status 增 `wick_miss`（七态）；`resolve_reason` 增 `stop_wick_weak`。
- [x] `ReentryWatchOut` 增 `stop_wick_ok/stop_wick_pct/stop_wick_mult`；
      `_reentry_out` 从 `structure_json["stop_wick"]` 展平（无快照为 None）。

### A6 飞书卡片
- [x] `build_reentry_card` 增「止损根影线」行（`下影/上影 31.00×实体｜95% 全幅 ✓`，无快照 `—`）；
      当前价口径改为「1h 收盘」。

### A7 前端
- [x] `types/index.ts`：`ReentryWatchStatus` 加 `wick_miss`；`ReentryWatch` 加影线三字段。
- [x] `ReentryWatchPanel.tsx`：新增「止损根影线」列（`WickCell` + 门槛 Tooltip）；
      当前价列改「当前价(1h收盘)」；`TERMINAL` 加 `wick_miss`；自动刷新 60s→5 分钟；
      空态文案加「（整点收盘后 6 分钟更新）」。

### A8 文档 / 测试
- [x] `spec.md` 同步两道闸、小时收盘口径、字段/API/卡片/排班/前端/测试清单。
- [x] `test_reentry_watch.py` 重写（29 例）：`compute_stop_wick` 5 例 + register 影线/结构路径
      + sweep 小时收盘触发/止损根排除/窗口外/增量补扫/重发幂等。
- [x] `PYENV_VERSION=3.11.9 python -m pytest tests/test_reentry_watch.py tests/test_reentry_shadow.py
      tests/test_hold_warnings.py tests/test_pending_sweep_shortcut.py -q` → **47 passed**。
- [x] `cd frontend && npx tsc --noEmit` → 无错误。

### A9 提交
- [x] `git status` / `git diff` 自查（无 `RULE_VERSION`、无下单/仓位/止损改动）。
- [x] 本地提交：`feat(reentry): 每小时收盘确认 + 止损根长影线闸（并降频复盘/挂单巡检）`。
- [x] 不推送、不部署。

## 完成判定
- 正式网止损单在 24h 内结构完好时生成监控行；价格回到开仓价（或 θR 阈值）时只收到 1 张含免责声明的飞书卡；页面可看状态/倒计时/结构并可忽略；全量测试与 tsc 通过。
- 2026-10-31 增补后：止损根 1h 必须带长影线（否则直接 `wick_miss`）；巡检每小时 :06，仅用已收盘 1h 的 high/low 判定触发；漏跑小时自动补扫；页面显示影线读数并 5 分钟刷新。
