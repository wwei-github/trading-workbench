# 任务清单：4h 定向 / 1h 入场（筛选 + 提示词）

> 对应 [spec.md](file:///Users/wangwei/Documents/github/trading-workbench/docs/需求文档/07-4h定向1h入场-筛选与提示词重建/spec.md)
> 状态：开发中（工作区已有未提交实现）。事实修复、观测与策略变化分开验收；4h 非 aligned 双向 veto 是用户确认的策略变更，回放完成前不得视为可上线。

## Phase 0 — 事实修复（不改判据）

- [x] **T0-1 量能口径修正**（共享事实包用 `volume_ratio` 单独渲染量比与原始量；核心提示词回归通过）
  - 文件：`backend/app/tasks/ai_tasks.py`（构造 signal 时补 `volume_ratio`）、`backend/app/services/ai_analyzer.py:628-630`、`backend/app/services/ai_agent.py:935-937`
  - 改法：`volume_ratio = volume_ratio(klines)`；渲染 `量能: {type}（量比 {ratio:.2f}×近20根中位量）`；原始量单独一行并标注单位与「不参与判断」
  - 验收：新增单测断言提示词不含 `×20根均量` 后接原始量；`ratio` 与 `indicators.volume_ratio` 同源
  - 回归：`/preview` 提示词页面同步正确

- [x] **T0-2 门槛语义修订**（静态分发门槛进提示词；动态连损按账号开仓时复核，非共用 `effective_min_rec`）
  - 改法：不把批量分发层的最大连损值伪装成所有目标账号的开仓门槛；执行层仍保留账号级现有判断。提示词明确静态分析门槛与执行动态门槛的区别。
  - 验收：连损开关关闭不渲染动态提升；账号级门槛继续由现有开仓测试覆盖；提示词不声称统一 effective gate。

- [x] **T0-3 注入质量分及分项**（渲染路径已落地；扫描实际分项持久化与迁移仍待验证）
  - 文件：`backend/app/services/ai_analyzer.py`（P0 用户消息）、`backend/app/services/ai_agent.py`（P1 事实包）、`backend/app/services/strategy/technical.py:compute_quality_score`（返回分项）
  - 改法：`compute_quality_score` 返回 `(score, parts)` 或附加 `parts` 字段；两管线渲染 `质量分: 68（base 50 / 4h +10 / 方向票 +10 / 减分 -2）`
  - 验收：P0/P1 均可见；不改变 scanner 放行集合与排序结果

- [x] **T0-4 指标块标题按触发类型**（实现已落地；相关回归通过后完成）
  - 文件：`backend/app/services/strategy/technical.py:1281`、调用方 `scanner.py` / `ai_analyzer.py` / `ai_agent.py`
  - 改法：`render_indicator_block(klines, htf_klines, trigger_type=None)`；回踩标题为 `技术指标（方向=4h；1h 方向票=确认度）`
  - 验收：回踩提示词不再出现「方向=方向票多数决」

- [ ] **T0-5 市场环境缺失显式标注**（P0 文案和字段已实现；P1 工具查询未发生/失败/确认为未知需区分，完成后再勾选）
  - 文件：`backend/app/services/ai_analyzer.py`（`_RULE_BTC_ENV`、`build_market_context`）
  - 改法：缺数据渲染 `BTC 环境: 未知（数据缺失）`，规则中禁止据此加分；`AIAnalysis` 增 `btc_env_missing`
  - 验收：数据缺失场景单测；复盘可按该标记分组

## Phase 1 — 观测补齐（不改行为）

- [ ] **T1-1 假想拒绝集**
  - 文件：`backend/app/services/scanner.py`（宇宙层与触发层）、`backend/app/services/scan_filters.py`
  - 改法：新增 `dry_run_filters` 开关，把未来要启用的 `atr_band` / `spread` / `min_size` / `overextended` 计算出来但只写入 `funnel_json`，不实际拒绝
  - 验收：`funnel_json` 中可见四类计数；放行集合与现状完全一致（回归断言）

- [x] **T1-2 排序可观测字段**（工作区已存质量分分项与池内名次；仍需补全并验证 AI 实际入选标记/排名导出）
  - 文件：`backend/app/tasks/ai_tasks.py:156-177`、`backend/app/models`（`ScanResult`）
  - 改法：落 `quality_parts`、`quota_pool`、`quota_rank`
  - 验收：可导出「同一轮中回踩/突破各自的排名分布」

- [ ] **T1-3 skip 原因枚举**
  - 文件：`backend/app/services/risk_guard.py`、`backend/app/services/ai_analyzer.py`（输出 schema）
  - 改法：`skip_reason_code ∈ {htf_weak, entry_bad, rr_low, env_risk, data_missing, overextended, other}`；`risk_guard` 校验必填
  - 验收：skip 样本 100% 带 code；复盘按 code 聚合

## Phase 2 — 影子（判据类）

- [ ] **T2-1 周期适配门**
  - 文件：`scan_filters.py`（`min_atr_pct_1h` / `max_atr_pct_1h`）、`scanner.py`
  - 影子：`veto_original=atr_band`
  - 验收：影子 ≥30 笔，期望不劣于基线

- [ ] **T2-2 价差门 + 最小可成交性**
  - 文件：`exchange_pool.py`（`bookTicker` 批量 + `exchangeInfo` 缓存）、`scanner.py`、`scan_filters.py`（`max_spread_bps`）
  - 影子：`veto_original=spread` / `min_size`
  - 验收：执行层对应的订单拒绝数下降至 0

- [ ] **T2-3 突破防追高**
  - 文件：`scanner.py`（突破分支）、`scan_filters.py`（`max_extension_atr`）
  - 影子：`veto_original=overextended`
  - 验收：被拦样本的后续 4h 最大有利偏移不高于放行样本

- [ ] **T2-4 配额按触发类型分池**
  - 文件：`ai_tasks.py:156-177`、`scan_filters.py`（`ai_quota_breakout` / `ai_quota_pullback`）
  - 影子：同时计算两种分配，只执行旧分配，记录差异
  - 验收：回踩进入 AI 占比 ≥30%

- [ ] **T2-5 抑制键扩展 + 结果反馈**
  - 文件：`scanner.py:1274`（`_suppress_repeat_hits`）
  - 改法：键加 `direction` + `price_bucket`；读最近 `TradeRecord` 终态
  - 影子：记录「本会被抑制但放行」的信号
  - 验收：止损后二次回踩的命中数可观测，且影子期望不劣化

- [ ] **T2-6 4h 强度分影子特征**（工作区已计算并记录 `htf_strength_shadow`；确认不进入总分/不改变排序，并完成离线评估）
  - 文件：`technical.py:compute_quality_score`
  - 改法：ADX 三档 + EMA144 斜率分位 + 距 EMA144 的 ATR 倍数（见 spec F3）
  - 影子：只记录新排序，不改变实际配额选择
  - 验收：强弱 aligned 分差可观测；配额命中率对比

## Phase 3 — 提示词结构（与评分口径绑定）

- [ ] **T3-1 评分档位按触发类型分表**
  - 文件：`ai_analyzer.py:_RULE_SCORE`
  - 改法：突破 60/70/85 保持；回踩按 spec P4 的表；与门槛分段共用同一常量
  - 验收：回踩 rec 分布不再 100% 贴 60；影子 ≥30 笔

- [ ] **T3-2 4h 非 aligned 双向 veto（策略变更）**
  - 文件：`technical.py:enforce_htf_constraint`、`ai_analyzer.py` / `ai_agent.py` 提示词
  - 改法：兜底约束引用 `STATE_ALIGNED`；提示词删除不可达状态分支
  - 验收：覆盖全部 4h 状态 × 信号类型 × 多空的兜底矩阵；transition_pbc 之外所有新 veto 均有原始建议快照与可回放结果；验证无 4h 数据、重取不一致、手动重分析失败关闭/放行语义；独立 RULE_VERSION 与回滚条件。单一常量引用不是行为验收。

- [ ] **T3-3 共享 facts builder**
  - 文件：新增 `backend/app/services/ai_facts.py`（或就近放 `ai_analyzer.py`）
  - 改法：`build_signal_facts(signal, klines, htf_klines) -> str`，P0/P1 共用；P1 规则 11/12 改为复用 `_RULE_SL` / `compose_rule_tp`
  - 验收：同一 signal 下两管线事实文本 diff 仅剩工具能力段

- [ ] **T3-4 提示词瘦身**
  - 文件：`ai_analyzer.py`、`ai_agent.py`
  - 改法：按 spec §3.3 骨架合并为 5 段；规则只写一次
  - 验收：token 降 ≥30%；同一批历史信号重放，rec/skip 分布无系统性偏移

## 当前实现边界与待办

- 当前代码没有正式启用 ATR band、spread、min_size、overextended 硬过滤、类型分池配额或新去重键；这些仍是方案候选，不得当作已交付。
- `quality_score` 是扫描排序分，`AIAnalysis.recommendation` 是模型决策档位，二者不可互相替代；影子强度分不得进入前者。
- BTC 环境缺失标记目前由 P0 单次调用记录；P1 Agent 走工具循环，未查询不等于查询失败，需将 `not_requested / unavailable / available` 状态设计清楚后再完成 T0-5。
- 本清单勾选表示代码路径已存在，不代表数据库线上迁移、历史重放和运行观察均已完成。

## 验证命令（实施时执行）

```bash
# 后端单测
cd backend && pytest tests -q

# 事实一致性离线重放（实施时新增脚本）
cd backend && python -m app.scripts.replay_prompt_facts --days 30
```

## 提交纪律

- Phase 0 一个提交：`fix(ai): correct volume facts and inject effective threshold`
- Phase 1 一个提交：`feat(obs): record hypothetical rejects and quota ranking`
- Phase 2 按任务拆分提交，每项独立影子开关
- Phase 3 一个提交：`refactor(ai): rebuild prompt facts contract`
- 每次判据改动同步递增 `RULE_VERSION`
