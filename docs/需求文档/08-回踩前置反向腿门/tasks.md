# 08 - 回踩前置反向腿门：实施任务

> 前置：用户批准 `spec.md` 的 5 项决策清单。
> 纪律：只用已收盘K线；不新增数据库列；不改前端/AI 提示词；完成验收后仅本地 commit。

## T1. 配置层（scan_filters）

- [x] 在 `backend/app/services/scan_filters.py` 的 `SCAN_FILTER_KEYS` 增加：
  - `pullback_leg_gate_enabled`
  - `pullback_leg_lookback_bars`
  - `pullback_leg_min_bars`
  - `pullback_leg_min_atr`
- [x] `ScanFilters` 增加字段与默认值：
  - `pullback_leg_gate_enabled: bool = True`
  - `pullback_leg_lookback_bars: int = 24`
  - `pullback_leg_min_bars: int = 2`
  - `pullback_leg_min_atr: float = 0.5`
- [x] `resolve_scan_filters(cfg)` 用 `_bool/_int/_num` 容错解析；数值下限 `lookback>=2`、`min_bars>=0`、`min_atr>=0.0`。
- [x] `backend/tests/test_scan_filters.py` 增加默认值与覆盖解析断言。
- [x] `backend/app/api/scan.py` 的 `scan_filters_effective` / `_SCAN_FILTER_RANGES` / `_BOOL_FILTER_KEYS` 补齐 4 个新键。

## T2. 腿门 helper（trigger）

- [x] `backend/app/services/strategy/trigger.py` 导入 `atr_series`。
- [x] 新增常量 `LEG_END_WINDOW_BARS = 2`。
- [x] 新增 `_pullback_leg_gate(klines, direction, flt) -> tuple[bool, dict]`：
  - [x] 只用 `klines[:-1]`；
  - [x] `t = len(klines)-2`；
  - [x] 终点窗口 `[max(start, t-2), t]`；
  - [x] 做多：终点取最低 low，起点在终点前取最高 high；做空镜像；
  - [x] `bars = end_idx - start_idx`；`atr_mult = 腿幅 / ATR[end_idx]`；
  - [x] `atr_series` 长度必须为 `len(klines)-1` 且 ATR>0，否则 fail-closed；
  - [x] 返回证据字典（bars/atr_mult/leg_start_idx/leg_end_idx/leg_start_price/leg_end_price/atr）。

## T3. 接入 detect_trigger

- [x] 在行 1 突破分支之后、行 2 当根确认之前插入腿门：
  - [x] `touch_ref is not None`；
  - [x] `touch_ref != TOUCH_RETEST`；
  - [x] `flt.pullback_leg_gate_enabled`；
  - [x] `direction in (1, -1)`。
- [x] 不通过时：
  - [x] `_diag_note` 写「回踩{触点}前无有效{下跌腿|反弹腿}：X根/Y.XXATR < 门槛 …」；
  - [x] `touch_ref = None`、`touch_price = None`、`leg_rejected = True`；
  - [x] 行 2 不再当根确认；
  - [x] 行 5 不再 upsert pending；
  - [x] 行3 当时仍可用本根金K确认既有有效 pending（2026-10-22-2 已由 T11 收紧为确认侧复校）；
  - [x] 行 3 通用诊断在 `leg_rejected=True` 时抑制；
  - [x] 行 1 突破路径不受影响。

## T4. 新增单测

- [x] 新建 `backend/tests/test_pullback_leg_gate.py`，复制最小夹具 `_klines/_ema21_of/_set_signal_candle`。
- [x] 覆盖：
  - [x] 做空：1 根反抽 → 拒绝，诊断含「无有效反弹腿」；
  - [x] 做空：3 根反弹 ≥0.5 ATR → 确认；
  - [x] 做多：1 根回撤 → 拒绝；
  - [x] 做多：3 根下跌 ≥0.5 ATR → 确认；
  - [x] retest 豁免：helper 打成拒绝仍触发；
  - [x] `pullback_leg_gate_enabled=False` 可正常 upsert/确认；
  - [x] `min_bars=0 & min_atr=0` 通过；
  - [x] ATR 与已收盘K线对齐；
  - [x] 修改 `klines[-1]` 不改变判定（无前视）。

## T5. 调整既有测试

- [x] `test_trigger_two_stage.py` 增加 autouse bypass：`trig._pullback_leg_gate → (True, {...})`。
- [x] `test_touch_refs.py` 增加 autouse bypass；新增/保留一个 retest 豁免用例，内部显式打桩拒绝。
- [x] 运行 `test_scan_funnel.py`、`test_strategies.py`；因平坦夹具导致回踩用例失败的，加同样 bypass。
- [x] 不得通过放宽生产默认值来让测试通过。

## T6. 文档同步

- [x] `docs/信号与形态/02-信号触发器.md`：回踩两段式章节补充腿门定义/阈值/豁免/诊断（§5.1 腿门前置注记、§5.3 决策矩阵要点、新增 §5.7、§11 测试覆盖表）。
- [x] 同文件变更历史追加 2026-10-22 行。
- [x] `backend/skills/trend-pullback/SKILL.md`：入场条件补「回踩前应存在一段反向腿」。
- [x] `backend/app/config.py`：RULE_VERSION bump 至 `v2026-10-22` 并补版本注释（trigger 判定变化按机制必须递增）。

## T7. 验收

- [x] 定向测试：
  ```bash
  cd /Users/wangwei/Documents/github/trading-workbench/backend
  /Users/wangwei/.pyenv/versions/3.11.9/bin/python -m pytest \
    tests/test_pullback_leg_gate.py tests/test_trigger_two_stage.py \
    tests/test_touch_refs.py tests/test_scan_filters.py -q
  ```
- [x] 全量测试：
  ```bash
  /Users/wangwei/.pyenv/versions/3.11.9/bin/python -m pytest tests/ -q
  ```
- [x] 0 failed；新增用例全部通过；retest 豁免用例通过。
- [x] 检查 diff：无模型/迁移/前端/AI 提示词改动。

## T8. 本地提交

- [x] 仅 stage 本次改动文件。
- [x] conventional commit，建议：
  `feat(strategy): 增加回踩前置反向腿过滤门`
- [x] 不推送、不触发 CI、不连接服务器。

## T9. 检测修复（2026-10-22 复核）

- [x] 锁定漏洞：腿终点窗口含信号K线时，「1 根反抽/回撤 + 吞没/锤子自身振幅」可凑满 `min_bars`/`min_atr`，APR 类伪回踩仍会漏网。
- [x] 修复：腿终点窗口改为 `[max(window_start, t-LEG_END_WINDOW_BARS), t-1]`，严格不含信号K线。
- [x] 回归用例：`_short_reject`/`_long_reject` 的吞没极值改为严格超过反抽/回撤极值（去掉同值并列的掩盖），新增 `test_leg_gate_excludes_signal_candle_from_leg_end`。
- [x] `test_touch_refs.py` 补 autouse bypass（原实现遗漏；两个 swing 用例此前靠上述漏洞通过）。
- [x] 定向 167 passed、全量 1142 passed。
- [x] 同步 spec §4.1/§4.2/§4.3 与 02-信号触发器 §5.7 的窗口口径。

## T10. ARIA 复盘收紧（2026-10-22-2）

- [x] 定位：ARIAUSDT.P 上涨后的 retest 看涨吞没由 pending@retest 行3 确认命中——retest 豁免腿门 + 腿门未校验单调性（腿内低点比终点低约 2.6 ATR）。
- [x] `trigger.py`：移除 retest 豁免（全部触点同口径过门）。
- [x] `trigger.py`：新增 `LEG_OVERSHOOT_ATR = 0.5`，`_pullback_leg_gate` 增加单调性校验，返回 `reason="non_monotonic"` + `overshoot_atr`。
- [x] 诊断文案：新增「腿内出现更低低点/更高高点（超出 X.XXATR > 容差 0.50ATR，非单边腿）」。
- [x] `config.py`：`RULE_VERSION = "v2026-10-22-2"`。
- [x] 测试：非单调多/空回归夹具 + helper 断言；retest 无腿不确认 / 有真实腿照常确认。
- [x] 同步 spec、02-信号触发器 §5.1/§5.3/§5.7/历史、`scan_filters.py` 注释。
- [x] 定向 + 全量测试通过；本地 conventional commit（不推送）。

## T11. 行3 确认侧腿门复校（2026-10-22-2 补丁）

- [x] 定位：ARIA 真实K线逐根回放中，retest 豁免移除 + 单调性校验后，旧 pending（10-21 20:00 建池）仍在 10-22 00:00 按 Stage-1 语境确认——确认侧未复校腿门。
- [x] `trigger.py` 行3 有效 pending 分支：确认前用当前K线重跑 `_pullback_leg_gate`；不过则 `return None, None`（本根不确认、pending 保持等待），诊断写「待确认未确认：确认根前无有效{下跌腿|反弹腿}：…」。
- [x] 提取 `_leg_reject_detail` 共用触点侧/确认侧拒绝文案。
- [x] 回归用例：`_long_pending_then_rally`（拒绝）+ `_long_pending_then_confirm`（通过），62/60 根夹具确保 signal=60/58、anchor=56/57、elapsed≤4。
- [x] 定向 174 passed、全量 1149 passed；ARIA 600 根逐根回放确认 2026-10-22 00:00 UTC 不再触发。
- [x] 同步 spec §5.2/§6.1/§8、02-信号触发器 §5.3/§5.7/历史、`config.py` 版本注释。
- [x] 本地 conventional commit（不推送）。
