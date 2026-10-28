# 17-复盘模块审计 · 验收清单（P0 三项 + P1-3）

## P0-1 回放入场诊断 + 五维分档

- [x] `TradeReview` 新增 `entry_diag` JSON 列，`database.py` 幂等迁移 `ADD COLUMN IF NOT EXISTS`
- [x] 回放任务按信号 K 口径取诊断 K 线，落库 5 键 `pos/room_r/stop_atr/planned_rr/quality_score`，异常不影响主流程
- [x] 任务明细 `items` 同步携带 `entry_diag`
- [x] `/review/stats` 返回 `entry_diag_groups`，覆盖 5 维且仅统计 `entry_diag` 非空并落档的行
- [x] schema / TS 类型补充 `ReviewEntryDiagGroup`
- [x] 前端「入场诊断分档」Card 可切换五维并展示分档统计

## P0-2 观察窗口可配置 + censored/expired 拆分

- [x] `REVIEW_OBSERVATION_HOURS` 默认 24、可配置；`_review_window` 与任务到期判定均使用该配置
- [x] 窗口内未触发 → `censored`；计划三价/方向无效 → `expired`
- [x] `_agg_stats` / schema / TS / 前端面板均单列 `censored`（「未触发（删失）」），`expired` 改名「无效计划」
- [x] `win_rate` 分母仅 `win_tp1 + win_tp2 + loss`，`win_rate_note` 明确写出
- [x] 48h 窗口、`censored`、`expired` 三条语义均有回归用例

## P0-3 补丁双阈值

- [x] `HEAT_THRESHOLD=0.30`、新增 `HEAT_ABS_MIN=20`，判定为「热度达标 **或** 绝对笔数达标」
- [x] `MIN_SAMPLE=8`、`COOLDOWN_DAYS=14` 与 draft/active 去重逻辑保持不变
- [x] `skips` 逐标签记录「热度 x.xx/笔数 N」，汇总行标注双阈值门槛
- [x] 前端 `ReviewPatchesPanel` 触发条件 tooltip 同步
- [x] 新增测试覆盖「热度 0.2985 < 0.30 但笔数 20 → 触发」与「7 笔低热度 → 不触发」

## P1-3 回放 MFE/MAE

- [x] `TradeReview` 新增 `mfe_r/mae_r`（signed R），`database.py` 幂等迁移两列
- [x] `replay_path()` 计算持仓区间偏移；`replay_suggestion()` 三元组签名保持兼容
- [x] 入场成交根 → 出场根（`censored` 窗口末根）；入场根整根计入并文档标注上界口径
- [x] 未成交 / `|entry-stop|≤0` 时为 NULL，不污染统计
- [x] 多单/空单/入场根即止损/未成交四条单测 + 任务级落库断言
- [x] `_agg_stats` / `ReviewGroupStats` 增 `mfe_r_avg/mae_r_avg`（只统计非空行）
- [x] `/review/stats` 增整体均值与 `excursions`（按 outcome 均值 + P50，固定顺序）
- [x] 前端汇总卡 + 「持仓偏移（MFE/MAE）」按结局表 + 入场诊断分档 MFE/MAE 列
- [x] `test_stats_mfe_mae_excursions` 覆盖均值、分位与 NULL 不计入

## P1-1 金K形态白名单收窄

- [x] `GOLDEN_PATTERN_ORDER` 12 → 现役 8；已下线 4 种历史行并入「未记录」
- [x] `test_golden_pattern_only_active_eight` + 既有 13 行断言改 9 行

## P1-2 触发族 legacy 折叠

- [x] `TRIGGER_LEGACY` 标 6 个已下线类型；组/族全为下线类型才标 `legacy`
- [x] 建议统计/实盘归因面板默认折叠，可勾选展开并显示「历史」
- [x] `test_signal_family_legacy_flag` / `test_stats_legacy_trigger_flag`

## 文档与验证

- [x] `docs/复盘归因/06-复盘归因.md` 流程图、门槛、常量表、五态语义、`entry_diag`、API 契约、配置项同步
- [x] 定向 pytest（review_replay / attribution / patch_trigger_threshold / obs_batch_b / actual_review）通过
- [x] 后端全量 pytest 通过（1334 passed）
- [x] 前端 `npx tsc --noEmit` 通过
- [x] `git status` 仅包含本次相关文件
