# 17-复盘模块审计 · 实施任务（P0 三项）

> 需求边界见同目录 `spec.md` §5/§7。本清单只覆盖用户确认的 P0-1 / P0-2 / P0-3；
> P1/P2 明确不做。完成后仅本地提交，不推送、不部署、不连服务器验证。

## 完成情况（2026-10-28）

- [x] P0-1 模型与迁移：`trade_reviews.entry_diag` 列 + 幂等迁移
- [x] P0-1 回放计算并落库：`review_tasks` 信号 K 口径取诊断快照，`items` 同步
- [x] P0-1 `/review/stats` 五维分档 + schema / TS 类型
- [x] P0-1 前端「入场诊断分档」Card（Radio 切换五维）
- [x] P0-1 测试：回放 `entry_diag` 五键 + `/review/stats` 五维分档与 `censored` 计数
- [x] P0-2 配置 `REVIEW_OBSERVATION_HOURS`（默认 24，可调 48）+ 回放窗口传参
- [x] P0-2 `censored` / `expired` 语义拆分 + `win_rate_note` 分母说明 + 前端列与卡片
- [x] P0-2 测试：48h 窗口、未触发为 `censored`、无效计划为 `expired`
- [x] P0-3 补丁双阈值 `heat ≥0.30` 或绝对笔数 `≥20`（保留 `MIN_SAMPLE=8` 与冷却）
- [x] P0-3 前端 tooltip + 新建 `test_patch_trigger_threshold.py`（20/67 绝对笔数路径）
- [x] 文档同步 `docs/复盘归因/06-复盘归因.md`
- [x] 验证：定向 pytest / 全量 pytest（1325 passed）/ 前端 `tsc --noEmit`

## 已确认的关键事实

- 回放任务：`backend/app/tasks/review_tasks.py`，`OBSERVATION_HOURS=24` 硬编码，
  `open` 被直接改写为 `expired`；回放窗口只取完全落在窗口内的已收盘 1h K。
- 诊断函数：`backend/app/services/entry_diagnostics.py` 的 `snapshot()` 已产出
  `pos / room_r / stop_atr / planned_rr / quality_total / range_flag` 等；
  `closed_bars()` 会丢弃传入 K 线的最后一根。
- 分档常量：`backend/app/services/attribution.py` 的 `STOP_ATR_BUCKETS`、
  `POS_BUCKETS`、`ROOM_BUCKETS`、私有 `_bucket()`；`tests/test_attribution.py`
  直接调用 `A._bucket`，不得删除该名字。
- 补丁触发：`backend/app/services/patch_service.py`，`HEAT_THRESHOLD=0.5`、
  `MIN_SAMPLE=8`、`COOLDOWN_DAYS=14`。
- 统计接口：`backend/app/api/scan.py` 的 `review_stats()` 与 `_agg_stats()`；
  schema 在 `backend/app/schemas/scan.py`；前端类型在
  `frontend/src/types/index.ts`，面板在 `frontend/src/components/ReviewStatsPanel.tsx`。
- 当前无 `patch_service` 专项测试文件。

---

## P0-1 回放落 entry_diag + /review/stats 分档

### 1. 模型与迁移

- `backend/app/models/scan.py` → `TradeReview` 新增（`JSON` 已导入）：
  ```python
  # 回放时点入场诊断快照（P0-1）：pos / room_r / stop_atr / planned_rr / quality_score
  entry_diag: Mapped[Optional[dict]] = mapped_column(JSON(none_as_null=True), nullable=True)
  ```
- `backend/app/database.py` 迁移区新增幂等语句：
  ```sql
  ALTER TABLE trade_reviews ADD COLUMN IF NOT EXISTS entry_diag JSONB
  ```

### 2. 回放计算并落库

文件：`backend/app/tasks/review_tasks.py`

- 计算信号 K 开盘：`signal_bar_open = (start_ms // interval_ms) * interval_ms`
  （信号时刻落在该根内；对齐整点时取前一根的结束边界，保持「最后一根已收盘」语义）。
- K 线一次拉取，limit 覆盖窗口 + 信号前诊断上下文：
  `expected + 30`（窗口 24 根 + room_r 24 根 + ATR14 + 成形根 + 余量），
  结束时间仍为 `end_closed_ms`。
- 诊断 K 线：`open_time <= signal_bar_open + interval_ms`（包含信号根之后一根，
  供 `closed_bars()` 丢弃，保证信号根成为最后一根已收盘根）。
- 调用
  `entry_diagnostics.snapshot(klines=diag_klines, price=entry, stop_loss=stop, direction=r_direction, tp1=tp1, entry_price=entry, signal_type=sr.signal_type, interval=settings.KLINE_INTERVAL)`，
  落库时只保留 5 个键：
  ```python
  entry_diag = {
      "pos": snap.get("pos"),
      "room_r": snap.get("room_r"),
      "stop_atr": snap.get("stop_atr"),
      "planned_rr": snap.get("planned_rr"),
      "quality_score": getattr(sr, "quality_score", None)
                        if getattr(sr, "quality_score", None) is not None
                        else snap.get("quality_total"),
  }
  ```
  `snapshot()` 自身已吞异常并置 None；整段诊断再包一层 `try/except`，
  失败时 `entry_diag=None`，不影响回放主流程。
- `TradeReview(...)` 增加 `entry_diag=entry_diag`；任务明细 `items` 同步带
  `entry_diag`（便于任务记录页排查）。
- 仅在计划三价有效且窗口完整时计算；`expired`（无效计划）行为 `None`。

### 3. /review/stats 五维分档

文件：`backend/app/services/attribution.py`

- 保留 `_bucket` 原名，新增公开别名 `bucket_value = _bucket`（不破坏既有测试）。
- 新增常量：
  ```python
  PLANNED_RR_BUCKETS = [(0, 1.5, "<1.5R"), (1.5, 2, "1.5-2R"), (2, 3, "2-3R"), (3, 1e9, "≥3R")]
  QUALITY_BUCKETS = [(0, 60, "<60"), (60, 70, "60-69"), (70, 80, "70-79"),
                     (80, 90, "80-89"), (90, 1e9, "≥90")]
  ```

文件：`backend/app/api/scan.py` → `review_stats()`

- 复用已加载的 `TradeReview` 行，构造 `entry_diag_groups: list[dict]`，
  每项 = `{diag_dim, diag_bucket, **_agg_stats(rows)}`。
- 维度与常量映射：
  `pos → POS_BUCKETS`、`stop_atr → STOP_ATR_BUCKETS`、`room_r → ROOM_BUCKETS`、
  `planned_rr → PLANNED_RR_BUCKETS`、`quality_score → QUALITY_BUCKETS`。
- 只统计 `entry_diag` 非空且落在某个分档内的行；不匹配任何分档的取值
  （如 `room_r < 0`）不进入分档，与 D6 日报口径一致；分档按常量顺序输出。
- `ReviewStatsResponse` 增加 `entry_diag_groups`。

### 4. Schema / 类型 / 前端

- `backend/app/schemas/scan.py`：
  ```python
  class ReviewEntryDiagGroup(ReviewGroupStats):
      diag_dim: str
      diag_bucket: str
  ```
  `ReviewStatsResponse` 增加 `entry_diag_groups: list[ReviewEntryDiagGroup] = []`。
- `frontend/src/types/index.ts`：新增 `ReviewEntryDiagGroup`（继承 `ReviewGroupStat`
  + `diag_dim/diag_bucket`），`ReviewStats` 增加 `entry_diag_groups`。
- `frontend/src/components/ReviewStatsPanel.tsx`：在「4h 降级」表之后新增
  「入场诊断分档」Card；用 antd `Tabs` 或 `Select` 切换 5 个维度，表格列复用
  `hdColumns` 样式：分档 / 总样本 / 胜率(CI) / win_tp1 / win_tp2 / loss /
  censored / expired / EV R / 盈亏比。标题注明「回放时点快照，历史行无诊断」。

### 5. 测试

- `backend/tests/test_review_replay.py`：新增一条用例，断言回放落库行的
  `entry_diag` 含上述 5 个键；无诊断时允许 `None`。
- `/review/stats` 分档聚合：复用现有 API 测试基建，断言 `entry_diag_groups`
  非空且 `diag_dim` 覆盖 5 个维度（无样本维度可为空）。

---

## P0-2 观察窗口可配置 + censored/expired 拆分 + 分母说明

### 1. 配置

- `backend/app/config.py` 在 K 线配置附近新增：
  ```python
  # 建议回放观察窗口（小时）：默认 24，可调 48 以覆盖慢节奏行情；
  # 仅影响新回放，不改历史行
  REVIEW_OBSERVATION_HOURS: int = 24
  ```

### 2. 回放任务

文件：`backend/app/tasks/review_tasks.py`

- `_review_window(signal_time, hours: int | None = None)`：
  `hours = hours if hours is not None else settings.REVIEW_OBSERVATION_HOURS`。
- 任务内 `hours = int(settings.REVIEW_OBSERVATION_HOURS)`；
  `due_before = now_naive - timedelta(hours=hours)`；窗口计算传 `hours`。
- 模块常量 `OBSERVATION_HOURS=24` 删除，避免双源（测试默认值仍为 24）。
- 语义拆分：
  - 计划三价/方向无效 → `expired`（数据缺失，非行情结论）；
  - 回放返回 `open`（窗口内未触发任何价位）→ `censored`（右删失）；
  - 其余 `win_tp1 / win_tp2 / loss` 不变。
- 任务 `counts` 字典与 `items` 同步支持 `censored`。
- 模块 docstring 与任务 docstring 改为「观察窗口内（默认 24h，可配置）」。

### 3. 统计与文案

文件：`backend/app/api/scan.py`

- `_agg_stats()` 增加 `censored` 计数（`outcome == "censored"`）。
- `win_rate` 分母仍为 `win_tp1 + win_tp2 + loss`；`censored` 与 `expired`
  都不进入分母。
- `win_rate_note` 改为明确口径：
  「胜率分母只含已触发结论（win_tp1/win_tp2/loss）；`censored`=窗口结束仍未触发
  （右删失），`expired`=计划三价缺失/无效，二者均不计入分母。」

文件：`backend/app/schemas/scan.py`

- `ReviewGroupStats` 增加 `censored: int = 0`。
- `ReviewStatsResponse` 增加 `censored: int = 0`。

### 4. 前端

- `frontend/src/types/index.ts`：`ReviewGroupStat`、`ReviewStats` 增加 `censored`。
- `frontend/src/components/ReviewStatsPanel.tsx`：
  - 现有「超时」列（`expired`）改名为「无效计划」；
  - 新增「未触发(删失)」列（`censored`）；
  - 汇总卡片增加 `censored` 计数；
  - 头部注释与胜率 tooltip 同步新口径。

### 5. 测试

- `backend/tests/test_review_replay.py`：
  - `test_utc_signal_time_and_exact_24_hour_window` 增加 `hours=48` 断言；
  - `test_task_finalizes_unfilled_entry_as_expired` 改名 `..._as_censored`，
    断言 `outcome == "censored"`；
  - `test_old_incomplete_shard_does_not_hide_later_complete_row` 中
    `LATERUSDT` 断言由 `expired` 改 `censored`；
  - 新增无效计划用例：缺 `stop_loss` 时仍为 `expired`。

---

## P0-3 补丁双阈值

文件：`backend/app/services/patch_service.py`

- 常量：
  ```python
  HEAT_THRESHOLD = 0.30   # 热度阈值：近7天该标签笔数/已归因笔数
  HEAT_ABS_MIN = 20       # 绝对笔数兜底：热度不足但笔数够多同样触发
  ```
- `run_trigger()` 判定改为：
  ```python
  count = stats["tag_counts"].get(tag, 0)
  if heat < HEAT_THRESHOLD and count < HEAT_ABS_MIN:
      if count:
          below.append(f"{tag} 热度 {heat:.2f}/{count} 笔")
      continue
  ```
- 达标日志/skips 文案带「热度 + 绝对笔数」；汇总行改为
  `f"热度未达标（门槛 {HEAT_THRESHOLD} 或绝对笔数 {HEAT_ABS_MIN}）：..."`。
- 模块 docstring 第 3 行改为「近7天热度 ≥0.30 或绝对笔数 ≥20（且样本 ≥8）」。
- `MIN_SAMPLE=8`、`COOLDOWN_DAYS=14`、已有 draft/active 去重逻辑不动。

前端：

- `frontend/src/components/ReviewPatchesPanel.tsx` 触发条件 tooltip 改为
  「近7天热度 ≥0.30 或绝对笔数 ≥20，且总样本 ≥8」。

测试（新建 `backend/tests/test_patch_trigger_threshold.py`）：

- 内存 SQLite + `SystemConfig(id=1)`。
- 造 67 笔已归因交易：7 笔带「追高追空」、20 笔带「止损过宽」。
  - 「追高追空」heat≈0.10、count<20 → 不触发；
  - 「止损过宽」heat≈0.2985（<0.30）但 count=20 → 触发（绝对笔数路径）。
- monkeypatch `patch_service._generate_draft` 返回 `None`，通过 `skips`
  中含「止损过宽 热度 0.30 达标」且不含「追高追空 ... 达标」断言阈值生效
  （不调 LLM、不落补丁行）。

---

## 文档同步

`docs/复盘归因/06-复盘归因.md`：

- §流程图 `标签热度 ≥0.5 且样本 ≥8` → `热度 ≥0.30 或绝对笔数 ≥20，且样本 ≥8`；
- 回放任务描述「24h 后逐 K 回放」→「观察窗口（默认 24h，可配置）后逐 K 回放」；
- 四态说明补 `censored`，明确 `expired` 仅指计划无效；
- `TradeReview` 字段表补 `entry_diag`；
- 常量表 `HEAT_THRESHOLD 0.5` → `0.30`，补 `HEAT_ABS_MIN 20`；
- `/review/stats` 契约补 `entry_diag_groups` 与 `censored`；
- `window_stats`/热度门槛段落同步双阈值。

---

## 验证

1. `cd backend && python -m pytest tests/test_review_replay.py tests/test_attribution.py tests/test_patch_trigger_threshold.py -q`
2. `cd backend && python -m pytest -q`（全量回归）
3. `cd frontend && npx tsc --noEmit`（或仓库既有类型检查脚本）
4. 检查 `git diff`：只包含本清单涉及文件。

## 提交

- 仅本地提交，不推送、不触发 CI、不连服务器。
- conventional commits，建议拆三条：
  - `feat(review): 回放落入场诊断并按五维分档统计`
  - `feat(review): 观察窗口可配置并拆分 censored/expired`
  - `feat(review): 补丁触发改为热度或绝对笔数双阈值`
