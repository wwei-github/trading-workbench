# 再入场检测与提醒 — 任务清单

> 前置：用户批准 `spec.md` 后按顺序执行；每步完成即勾选。
> 约束：只提醒不下单；仅正式网；不 bump RULE_VERSION；本地提交不推送。

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
- [x] `backend/app/celery_app.py`：beat 新增 `reentry-watch`，`crontab(minute="*")`。

## T4 API
- [x] `backend/app/schemas/trade.py`：新增 `ReentryWatchOut`（含 `seconds_left` 与结构展平字段）。
- [x] `backend/app/api/trades.py`：
  - [x] `GET /api/trades/reentry-watches`（status 可选，limit 默认 200，created_at desc）。
  - [x] `POST /api/trades/reentry-watches/{id}/dismiss`（仅 watching/triggered 可忽略；终态 400）。

## T5 前端
- [x] `frontend/src/types/index.ts`：新增 `ReentryWatch`（六态 status + 结构展平字段）。
- [x] `frontend/src/api/scan.ts`：`trades.reentryWatches` / `dismissReentryWatch`。
- [x] 新增 `frontend/src/components/ReentryWatchPanel.tsx`：
  - [x] 顶部固定 `Alert`：「仅提醒，不会自动开单；回测该再入场腿期望为负」。
  - [x] `Segmented` 监控中/已触发/已结束（前端分组过滤，一次拉 all）。
  - [x] 表格列：币种(可点 K 线)/方向/状态 Tag+Tooltip/原入场·止损/触发价(θR)/当前价/距触发%/结构 ✓✗/倒计时/忽略。
  - [x] 60s 自动刷新 + 手动刷新；忽略二次确认；三组空态文案。
  - [x] K 线弹窗：`KlineChart`（hitTime=created_at、hitDirection）。注：后端未透出 `closed_at`，用监控行 `created_at`（平仓后分钟级建立）近似。
- [x] `frontend/src/pages/ScanResult.tsx`：合约 Tabs 在「交易记录」后插入「再入场提醒」（仅合约模式）。

## T6 测试
- [x] 新增 `backend/tests/test_reentry_watch.py`，覆盖 spec §7 全部用例（17 例）。
- [x] `cd backend && python -m pytest tests/test_reentry_watch.py tests/test_reentry_shadow.py tests/test_hold_warnings.py -q` → 32 passed。
- [x] `cd frontend && npx tsc --noEmit` → 无错误。

## T7 提交
- [x] `git status` / `git diff` 自查（确认无 RULE_VERSION、无执行链路改动）。
- [x] 本地提交：`feat(reentry): 止损后再入场检测与飞书提醒（仅提醒不下单）`。
- [x] 不推送、不部署。

## 完成判定
- 正式网止损单在 24h 内结构完好时生成监控行；价格回到开仓价（或 θR 阈值）时只收到 1 张含免责声明的飞书卡；页面可看状态/倒计时/结构并可忽略；全量测试与 tsc 通过。
