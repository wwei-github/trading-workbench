# 影子开庭执行清单（2026-10-27）

> 先决条件：spec §七 D1–D6 人工确认完成。未确认前不执行任何代码删除（T2）。
> 所有改动仅本地提交，不推送、不部署、不触达线上数据。

## T0 前置：快照留档

- [x] 保存线上快照 JSON（只读请求）到临时目录，核对 spec §一 数字与 §十一 归档一致。
  - 快照保存于 `/tmp/shadow_report_2026-10-27.json`（`window_days=30`，7 卡）。
  - 与 spec §一 核对：CARD-01/02/04/09 因快照后新增 1~2 条归因计数出现微漂移（如 CARD-01 confirm 446→447、CARD-02 baseline 122→124、CARD-09 rec70plus 75→76），`sufficient`/`court_date` 与全部分桶方向均不变，裁决结论不受影响。

## T1 CARD-10 升格（r2_low 折价）

- [x] `backend/app/services/strategy/technical.py`：新增 `R2_LOW_MAX = 0.4`、`R2_LOW_PENALTY = -5`；`compute_quality_score` 内计算 24 根已收盘 1h R²（局部导入 `swing.linear_regression`），`R²<0.4` → `_add("r2_low", -5)`；补文档串。
- [x] `backend/app/services/ai_analyzer.py`：`QUALITY_PART_LABELS` 加 `("r2_low", "弱趋势")`。
- [x] `backend/tests/test_quality_score.py`：新增 4 个断言场景（强趋势 / 震荡 / K 线不足 / clamp）。
- 验收：`python -m pytest -q tests/test_quality_score.py tests/test_prompt_invariants.py` 全绿；手工构造震荡 K 线时 `parts["r2_low"] == -5`。

## T2 CARD-02 / CARD-04 关闭即删除

- [x] `backend/app/services/shadow.py`：删 `compute_macd_div`、`compute_oi_conf_scan_stub`、`macd_div`/`oi_conf` 注册表条目、OI 段（`OI_CONF_ENABLED` … `tag_oi_conf` 全部）。
- [x] `backend/app/tasks/ai_tasks.py`：删 `tag_oi_conf` 导入与调用块。
- [x] `backend/app/services/exchange_pool.py`：删 `get_open_interest`（确认无其他调用方）。
- [x] `backend/app/services/strategy/technical.py`：删 `analyze_macd_div`（保留 `_macd_series` / `analyze_macd`）。
- [x] `backend/tests/test_shadow_batch2.py`：删 macd/oi 用例、fixture、import；保留 r2_slope 用例；更新文件头。
- 验收：`grep -R "macd_div\|oi_conf\|tag_oi_conf\|get_open_interest\|analyze_macd_div" backend/app` 仅剩无关字符串；`python -m pytest -q` 全绿。
  - 已核：`backend/app` 无残留（仅 `models/shadow.py` 注释已同步改写）；`COURT_DATES` 的 CARD-02/CARD-04 条目一并删除。相关 4 个测试文件 59 passed。

## T3 CARD-08 读数补全（按触点族分层）

- [x] `backend/app/services/shadow.py`：`build_report` 对 `CARD-08` 输出 `family_buckets`（swing/retest，各自 baseline+buckets，按 `value_json.touch_ref` 分族），既有 `buckets` 不变。
- [x] `frontend/src/types/index.ts`、`frontend/src/components/ShadowPanel.tsx`：渲染分族摘要行（swing 主判定 / retest 观察）。
- [x] `backend/tests/test_sweep_shadow.py`：新增 build_report 分族用例。
- 验收：报告 JSON 出现 `family_buckets.swing` 且不含 retest 标记；前端 `tsc --noEmit` 通过。
  - 已核：`test_build_report_family_buckets_split` 通过（swing n=2 不含 sweep_open、retest n=1 仅 sweep_open，混合 buckets 不变）；`tsc --noEmit` 无输出。

## T4 CARD-07 面板提示（P2，待 D6 确认）

- [x] `ShadowPanel.tsx`：CARD-07 卡头加「已裁决 2026-10-19 · 回放待 n≥60」静态标签。
- 验收：`tsc --noEmit`；该卡仍可查看读数，标签不影响三态逻辑。

## T5 文档回写

- [x] `docs/需求文档/02-影子模式方案.md`：§1.3 / §七 / §十 / §十一（新增 2026-10-27 开庭记录 + 关闭归档两行）。
- [x] `docs/需求文档/01-流派融入路线.md`：#3 状态改为「进回放」；#4 / #11 关闭；#10 延期复评。
- 验收：§十一 归档表与 spec §一/§二 数字一致；无自相矛盾状态。

## T6 全量验证与本地提交

- [x] `cd backend && python -m pytest -q`（全量）
- [x] `cd frontend && npx tsc --noEmit`
- [x] `git add` 仅本次涉及文件；`git commit`（conventional commits，例如 `feat(shadow): r2_low quality discount + close oi_conf/macd_div cards`）
- 验收：提交成功、未推送；`git status` 无本次遗留未暂存文件。

## 遗留（不在本次执行）

- [ ] CARD-09 测试网 avg_r −13.616 异常排查（单独立项）。
- [ ] CARD-08 swing 族复评（需部署后拉取线上报告）。
- [ ] CARD-01 的 #3 本体预注册回放（另立项，预注册须含现役三票共线性核对）。
