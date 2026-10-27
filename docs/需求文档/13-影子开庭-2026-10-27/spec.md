# 影子实验开庭 spec（2026-10-27）

> 流程依据：[02-影子模式方案](../02-影子模式方案.md) §八「快照 → 标准卡核对 → 拍板」。
> 快照来源：线上 `GET http://43.129.220.237:8088/api/shadow/report?window_days=30`（只读，2026-10-27）。
> 窗口：近 30 天；`counted` = 有效归因样本；scan_result 宿主卡的 R 口径按 §七「预注册 R 口径（2026-10-19）」。
> 当前 RULE_VERSION：`v2026-10-27`（同日 TP/SL 留距调整已 bump，本 spec 不再重复 bump）。

## 一、快照摘要（counted / win_rate / avg_r）

| 卡 | 特征 | 预注册开庭日 | sufficient | 分桶读数（counted = 胜率 / avgR） | 基线 |
| --- | --- | --- | --- | --- | --- |
| CARD-01 | cvd_div | 2026-10-27 | true | confirm 446 = 49.33% / +0.3201；diverge_bear 82 = 41.46% / +0.0927；diverge_bull 169 = 50.89% / +0.2503 | 697 = 48.78% / +0.2764 |
| CARD-02 | oi_conf | 2026-10-21 | true | confirm 64 = 43.75% / +0.0503；diverge 58 = 48.28% / +0.3072 | 122 = 45.90% / +0.1724 |
| CARD-04 | macd_div | 2026-10-27 | true | confirm 512 = 48.05% / +0.2843；diverge_bear 51 = 54.90% / +0.3623；diverge_bull 134 = 49.25% / +0.2138 | 697 = 48.78% / +0.2764 |
| CARD-08 | sweep_reclaim | 2026-10-21 | true | no_sweep 103 = 53.40% / +0.4613；sweep_open 55 = 43.64% / +0.2273；sweep_reclaim 207 = 50.72% / +0.3309 | 365 = 50.41% / +0.3521 |
| CARD-10 | r2_slope | 2026-10-22 | true | r2_high 153 = 50.98% / +0.3265；r2_low 329 = 43.47% / +0.1054；r2_mid（观察）222 = 55.86% / +0.5179 | 704 = 49.01% / +0.2835 |
| CARD-07 | reentry_precond | null（到量即判） | true | structure_ok 55 = 67.27%（continued 37）；structure_broken 88 = 70.45%；正式网合计 143 = 69.23% | SPRT h1 locked 2026-10-17：LLR 2.9036 > 上界 2.7726，posterior 0.6618 [0.4972, 0.8083]，n=33；测试网 23 = 73.91% |
| CARD-09 | rec70_shadow | 2026-11-08 | true | rec60_69 173 = 44.51% / +0.1974；rec70plus 75 = 38.67% / −0.0016 | 248 = 42.74% / +0.1372；SPRT h0 LLR −11.4865；测试网 360 = 40.00% / avg_r −13.616（异常，见 §六） |

## 二、逐卡标准卡核对与裁决

### CARD-01 cvd_div —— 达标（①②④）→ 进 docs/13 #3 本体预注册回放

标准卡（§七）核对：

1. `n ≥ 80/桶`：confirm 446 ✓ / diverge_bear **82 ✓（刚过线）** / diverge_bull 169 ✓ → 满足；
2. `no_div vs bullish_div`（confirm − diverge_bear）win_rate 差 = **+7.87pp ≥ 5pp** ✓；avg_r 差 = **+0.2274R ≥ 0.05R** ✓，方向与假设一致（CVD 未跟随的价升突破更差）；
3. `与 OBV 票同票率 < 80%` → **不可测且判定载体已失效**：OBV 已于 2026-10-15 指标拆角色（[01-流派融入路线 #3](../01-流派融入路线.md)）退出方向票，报告从未记录该读数，`value_json` 也无 OBV 字段；
4. 周期 ≥ 14 天：前向窗口自 2026-10-13（`since_rule_version=v2026-10-13-8`）起算，至 2026-10-27 恰 14 天 ✓。

裁决：**不予直接改生产投票池**。理由：① 卡的升格动作是「三票 → 四票、门槛 ≥2 → ≥3」，按 docs/13 #3 纪律必须先走**本体预注册回放**（扩池改变全部信号流向）；② 条件③的 OBV 载体已退役，其等价问题（CVD 与**现役三票** MACD/布林带/RSI 的共线性/同票率）在回放中可直接用历史 K 线精确复算，阈值沿用 80%。故裁决为「达标 → 进 #3 本体预注册回放」，回放预注册时须写入：若同票率 ≥ 80%，回放判不升格（不以扩池形态落地）。

### CARD-02 oi_conf —— FAIL 关闭归档（关闭即删除）

标准卡核对：

1. `n ≥ 60/桶`：confirm 64 ✓ / diverge 58 ✗（差 2）；
2. `diverge win_rate ≤ confirm − 5pp`：实测 **diverge 高于 confirm 4.53pp**（48.28% vs 43.75%），距阈值 9.53pp，**方向相反**，✗；
3. 周期 ≥ 14 天：10-07 观察期重置起算，至 10-21 已满 ✓（今日已超期）。

裁决：**FAIL 关闭归档**。依据：两轮窗口（10-19：n=29、−2.94pp；10-27：n=122、+4.53pp）均未复现「背离组更差」的假设，方向翻转且不稳定；卡预注册的 n=60/桶对 5pp 单向效应本身检定力不足（约需 8–10pp 观测差才可分辨），继续积累无法改变「无可靠证据」的结论。按 §八「关闭即删除」删除全部相关代码，DB 历史 marks 与 §十一 归档保留。若日后重启 OI 方向，须按检定力重新预注册，不沿用本卡。

### CARD-04 macd_div —— FAIL 关闭归档（关闭即删除）

标准卡核对：

1. `n ≥ 60/桶`：confirm 512 ✓ / diverge_bear **51 ✗** / diverge_bull 134 ✓；
2. `confirm − diverge_bear ≥ 5pp`：实测 **−6.85pp**（48.05% vs 54.90%），方向相反 ✗；
3. `confirm − diverge_bull ≥ 5pp`：实测 **−1.20pp** ✗；
4. 周期 ≥ 14 天：10-13 起算至 10-27 满 14 天 ✓。

裁决：**FAIL 关闭归档**。依据：两轮窗口读数不稳定（10-19：+2.56pp / +2.27pp；10-27：−6.85pp / −1.20pp），无任何可持续复现的效应，且判定桶样本不足；已用尽 2026-10-19 的一次延长。按「关闭即删除」删除注册表条目、专用 compute 与 `technical.analyze_macd_div`（无其他调用方），DB 历史 marks 与归档保留。

### CARD-08 sweep_reclaim —— 延期（读数缺口：报告未按触点族分层）

标准卡主判定为 **swing 族**（retest 族仅观察列），但 `build_report` 常规分支只按 `value_json.bucket` 聚合，**未按 `value_json.touch_ref` 分族**，当前报告为 swing+retest 混合读数：

- 混合读数：sweep_reclaim − no_sweep = **−2.68pp**（50.72% vs 53.40%），不满足 `≥ +3pp`；sweep_reclaim 50.72% ≥ sweep_open 43.64% ✓；
- 但该混合值不能用于主判定（retest 族按卡文本须排除），若 swing 族单独读数不同，结论可能反转。

裁决：**本次不拍板**（不升格、不关闭）。先补读数：`build_report` 对 CARD-08 增出 `family_buckets`（swing / retest 各自的 baseline + 三桶读数），前端面板同步展示；部署后以 swing 族读数复评（`n≥30/桶`、`Δ≥+3pp`、`sweep_reclaim ≥ sweep_open`，任一不满足 → FAIL 关闭）。

### CARD-10 r2_slope —— 达标 → 升格 quality_score 折价（排序层）

标准卡核对（全部满足）：

1. 判定桶 `n ≥ 60/桶`：r2_high 153 ✓ / r2_low 329 ✓（r2_mid 观察列不进判定）；
2. `win_rate(high) − win_rate(low) = +7.51pp ≥ +5pp` ✓；
3. `avg_r(high) − avg_r(low) = +0.2211R ≥ +0.05R` ✓；
4. `avg_r(low) ≥ avg_r(high)` → 否（0.1054 < 0.3265），不触发 FAIL ✓；
5. 周期 ≥ 14 天：2026-10-08 锁卡至 2026-10-22 开庭日已满 ✓。

裁决：**升格为 quality_score 单向折价（排序层，不进闸门）**：

- 规则：24 根已收盘 1h 收盘价线性回归，`R² < 0.4` → **质量分 −5**；r2_high/r2_mid 不加分（r2_mid 读数最高 55.86% / +0.5179R，非单调，反对给 high 加分；只对「震荡段」做折价，语义与 CARD-03 先例「压缩期折价」一致）；
- 不 bump RULE_VERSION（§八：排序修饰影响小先走）；`parts_out` 记 `r2_low` 分项，提示词与复盘可见；
- 后续观察：折价会改变 AI 配额排序，若需 A/B 效果评估，走规则补丁流程另立项。

### CARD-07 reentry_precond —— 无新裁决（2026-10-19 已判达标 → #12 回放）

- 2026-10-19 已裁决「达标 → 进 docs/13 #12 本体预注册回放」，P-08 早停 h1 于 10-17 落锁（判定时点在预注册之后，不构成提前）；
- 今日 structure_ok n=55 < 60，未达 #12 回放的复评条件（首轮 n=36 样本不足不拍板），**本次不重复裁决**；
- 登记：距复评门槛差 5 条；面板仍显示「可开庭」（court_date=null）属预期，不代表待裁决。

### CARD-09 rec70_shadow —— 未到期（时间锁 2026-11-08），今日不开庭

- 三条成功标准：`n ≥ 15` ✓（173）；`avg_r(rec60_69) < 0` ✗（+0.1974）；`win_rate(60_69)+8pp ≤ win_rate(70+)` ✗（52.51% vs 38.67%）；
- SPRT h0（LLR −11.49 < 下界 −1.56）为「存量是运气差」方向证据，但 CARD-09 协议无早停通道（固定 28 天窗口 + 三条件联合判定），**不得提前裁决**；
- 若维持至 11-08，按决策规则「avg_r ≥ 0 → 卡关闭归档、门槛保持 60」。

## 三、代码变更（仅本地提交，不部署）

### T1 CARD-10 升格：r2_low 质量分折价

- [technical.py](../../../backend/app/services/strategy/technical.py) `compute_quality_score`：
  - 新增模块常量 `R2_LOW_MAX = 0.4`、`R2_LOW_PENALTY = -5`（放在 `STRUCTURE_GATE_PENALTY` 附近）；
  - 在「震荡闸门降权」之后、4h 强度影子块之前插入：`klines` 可用且 ≥25 根时，取 `klines[:-1][-24:]`，局部导入 `app.services.strategy.swing.linear_regression` 计算 R²（与 CARD-10 影子同引擎，避免重复实现），`R² < R2_LOW_MAX` → `_add("r2_low", R2_LOW_PENALTY)`；
  - 文档串补一行修饰规则（2026-10-27 升格）。
- [ai_analyzer.py](../../../backend/app/services/ai_analyzer.py) `QUALITY_PART_LABELS`：新增 `("r2_low", "弱趋势")`，提示词质量分行可见。
- [test_quality_score.py](../../../backend/tests/test_quality_score.py)：新增用例——强趋势（R²≥0.7）无 r2_low 项、震荡（R²<0.4）−5 且 `parts["r2_low"] == -5`、klines 不足 25 根不生效、总分 clamp 0~100 不受影响。

### T2 CARD-02 / CARD-04 关闭：删除全部相关代码

- [shadow.py](../../../backend/app/services/shadow.py)：
  - 删除 `compute_macd_div`、`compute_oi_conf_scan_stub`；
  - 删除注册表 `macd_div`、`oi_conf` 两条（含其注释）；
  - 删除 OI 段全部内容：`OI_CONF_ENABLED`、`_OI_RING`、`_OI_RING_TTL_S`、`_OI_RING_MAXLEN`、`_OI_PREV_WINDOW_S`、`_oi_redis_client`、`_oi_redis`、`_ring_add`、`_ring_window`、`tag_oi_conf`；
- [ai_tasks.py](../../../backend/app/tasks/ai_tasks.py)：删除 `from app.services.shadow import tag_oi_conf` 与配额段调用块（约 250–260 行）；
- [exchange_pool.py](../../../backend/app/services/exchange_pool.py)：删除 `get_open_interest`（唯一调用方为已删的 `tag_oi_conf`）；
- [technical.py](../../../backend/app/services/strategy/technical.py)：删除 `analyze_macd_div`（唯一调用方为已删的 `compute_macd_div`；`_macd_series` 与 `analyze_macd` 保留）；
- [test_shadow_batch2.py](../../../backend/tests/test_shadow_batch2.py)：删除 macd/oi 的用例、fixture 与 import，保留 r2_slope 用例并更新文件头注释；
- 前端无需改动（`diverge_*` / `confirm` 桶标签仍被 CARD-01 使用；已删卡不再进报告）。

### T3 CARD-08 读数补全：按触点族分层

- [shadow.py](../../../backend/app/services/shadow.py) `build_report` 常规分支：当 `feat.card == "CARD-08"` 时，额外输出 `family_buckets`：
  `{"swing": {"baseline": {...}, "buckets": [...]}, "retest": {...}}`，分族依据 `value_json.touch_ref`，桶内统计复用 `_bucket_stats`；不改 `sufficient`、不改既有 `buckets` 字段（向后兼容）。
- [ShadowPanel.tsx](../../../frontend/src/components/ShadowPanel.tsx)：当 `family_buckets` 存在时，在桶表下方追加一行「swing（主判定）… / retest（观察）…」摘要；[types/index.ts](../../../frontend/src/types/index.ts) 补类型。
- [test_sweep_shadow.py](../../../backend/tests/test_sweep_shadow.py)：新增 build_report 用例——同一 feature 下 swing/retest 混合标记，断言 `family_buckets.swing` 不含 retest 标记、`family_buckets.retest` 不含 swing 标记，且既有 `buckets` 仍为混合聚合。

### T4 CARD-07 面板提示（P2，可选）

- `ShadowPanel.tsx`：对 `CARD-07` 增加静态说明标签「已裁决 2026-10-19 · 回放待 n≥60」，避免「可开庭」误导。

## 四、文档变更

- [02-影子模式方案.md](../02-影子模式方案.md)：
  - §1.3 特征表：`oi_conf` / `macd_div` 标注「2026-10-27 FAIL 关闭，代码已删除」；
  - §七：CARD-02 / CARD-04 卡尾追加关闭结论；CARD-10 卡尾追加升格实施结论（−5、排序层、不 bump）；
  - §十：行 2 / 行 4 状态补注关闭，行 13 保持；
  - §十一：新增「2026-10-27 开庭记录」小节（快照 + 核对表 + 遗留问题），并在关闭归档表追加 CARD-02 / CARD-04 两行。
- [01-流派融入路线.md](../01-流派融入路线.md)：
  - #3 状态 → 「CARD-01 达标 → 进本体预注册回放」；
  - #4 / #11 标题状态 → 关闭（CARD-02 / CARD-04 FAIL）；
  - #10 状态 → 读数补全后复评（延期）。

## 五、验证

```bash
cd backend && python -m pytest -q                      # 全量
cd backend && python -m pytest -q tests/test_quality_score.py \
  tests/test_shadow_batch2.py tests/test_sweep_shadow.py \
  tests/test_shadow_and_beta.py tests/test_prompt_invariants.py
cd frontend && npx tsc --noEmit
```

验收口径：全量测试通过；`SHADOW_FEATURES` 不再含 `macd_div` / `oi_conf`；报告对 CARD-08 输出 `family_buckets`；质量分对 `R²<0.4` 候选稳定 −5 且分项可见。

## 六、风险与遗留（登记，不在本次修复）

1. **检定力不足（方法论）**：CARD-02 / CARD-04 的 n=60/桶对 5pp 单向效应检定力不足，本次 FAIL 的结论表述为「无可靠证据 / 未复现」，不等于「已证明无信息量」。后续立卡须先算 power 再定 n。
2. **CARD-09 测试网 avg_r = −13.616**（360 笔）数值不可信，疑似 `risk_unit` 在测试网口径异常（如 initial_risk 过小导致 R 放大）；测试网仅对照组、不影响判定，需单独立项排查。
3. **CARD-01 条件③载体失效**：OBV 已退出方向票，卡文本未同步；本次以「回放内核对与现役三票共线性」承接，须在 #3 回放预注册中写明，避免口径漂移。
4. **CARD-10 折价权重 −5 为设计选择**（非读数直接映射）；只做单向折价，r2_mid 最优提示非单调性，后续若观察到配额分布异常需复核。
5. **候选池漂移**：CARD-10 折价会改变 AI 配额排序，新标记的 `counted` 总体与旧窗口略有不同源；CARD-08 复评以存量为主，若读数处边界需按新窗口重算。
6. **CARD-08 需部署后复评**：本次仅本地提交，线上报告在部署前不会出现 `family_buckets`，复评需等下一次发布（不触发部署）。

## 七、需人工确认的决策

| # | 决策 | 推荐 | 备选 |
| --- | --- | --- | --- |
| D1 | CARD-02 裁决 | FAIL 关闭（删代码） | 续观察（需说明理由，违反一次延长纪律） |
| D2 | CARD-04 裁决 | FAIL 关闭（删代码） | 续观察 |
| D3 | CARD-01 路径 | 达标 → 进 #3 预注册回放（不改生产） | 续观察 + 补共线性读数 |
| D4 | CARD-10 折价幅度与方向 | r2_low 单向 −5，不进闸门，不 bump | 折价 −8；双向 ±5 |
| D5 | CARD-08 | 延期，先补读数 | 按混合读数直接判 FAIL（违反卡文本，不推荐） |
| D6 | CARD-07 面板提示（P2） | 实施静态标签 | 不做，仅文档说明 |
