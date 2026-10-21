# 08 - 回踩前置反向腿门（Spec）

## 0. 结论（回答用户）

可以过滤。当前 `detect_trigger` 的回踩路径只校验「触点 + 同向金K」，没有校验触点之前是否存在一段反向腿。APRUSDT.P 的看跌吞没出现在单边下跌/下跌中继中，价格只是短暂反抽 EMA21 或摆动 LH，没有形成一段可交易的反弹腿，却被当作「回踩空」。

本方案在 Stage-1 触点判定处增加**硬门**：

- 做多：触点之前必须存在一段**下跌腿**，之后出现看涨金K；
- 做空：触点之前必须存在一段**反弹腿**，之后出现看跌金K；
- 不满足：不产生 `trigger_pullback_confirm`、不写 `PullbackPending`，漏斗记录具体死因；
- `retest` 触点豁免（突破回测自带突破事件与结构语境，避免误杀刚复出的突破回测路径）。

## 1. 背景与现状

### 1.1 用户反馈

系统想找的回踩单应为「1 小时一段下跌后出现反转金K」；做空镜像为「1 小时一段反弹后出现看跌反转金K」。APRUSDT.P 出现的看跌吞没并不在一段反弹趋势中，却被系统识别为回踩信号。

### 1.2 代码根因

`backend/app/services/strategy/trigger.py` 的 `detect_trigger`：

- 行 2（当根触碰 + 金K）：只判断 `touch_ref is not None and golden`；
- 行 5（触碰无金K）：只判断 `touch_ref is not None`，直接 upsert pending；
- 行 3（无触碰有金K）：依赖既有 pending，不在本根重新校验腿。

因此「单边下跌中反抽 EMA21/LH + 看跌吞没」可以完整走通 Stage-1 → Stage-2，产生伪回踩空。

### 1.3 现有可复用能力

- `klines[:-1]` 为已收盘K线，`klines[-2]` 为信号K线，管线全程无前视；
- `technical.atr_series(klines, 14)` 为 Wilder ATR，序列与已收盘K线对齐，`atr[-1]` 对应信号K线；
- `recent_swings` / `structure_1h` / 触点体系已提供结构语境，本方案不重复实现；
- `ScanFilters` 已是筛选参数单一来源（DB `system_config.scan_filters` JSONB > env > 代码默认）。

## 2. 目标

1. 在回踩 Stage-1 触点判定处增加「前置反向腿」硬门，剔除下跌/上涨中继中的伪回踩。
2. 腿的度量只用已收盘K线，按 ATR 归一化，避免不同币种波动率差异导致门槛失真。
3. 门槛可配置、可一键停用，默认值保持宽松，优先避免过度过滤。
4. 不通过时写入漏斗诊断，便于复盘和前端「判定说明」定位。
5. 补齐单测，保证原两段式决策矩阵与触点体系行为不回归。

## 3. 非目标

- 不修改突破触发（`trigger_breakout` / `trigger_trend_breakout`）。
- 不修改 4h 定向逻辑、1h 方向票、`range_score` 震荡闸门、质量分与 AI 提示词。
- 不新增数据库列，不改 `PullbackPending` 表结构（腿在 Stage-1 判定，不落快照）。
- 不新增前端筛选项；参数通过既有 `system_config.scan_filters` JSONB 或环境变量配置。
- 不改变 `retest` 触点的既有定义与优先级。

## 4. 判定口径

### 4.1 时间口径

- 只用已收盘K线：`closed = klines[:-1]`；
- 信号K线索引 `t = len(klines) - 2`（即 `closed` 的最后一根）；
- 腿的搜索窗口：`[max(0, t - lookback + 1), t]`；
- 腿的终点搜索窗口：`[max(window_start, t - LEG_END_WINDOW_BARS), t-1]`，`LEG_END_WINDOW_BARS = 2`；
  - **严格不含信号K线**（2026-10-22 检测修复）：反转K线自身的极值属于反转动作，不算腿终点——否则「1 根反抽/回撤 + 吞没/锤子自身振幅」即可凑满 `min_bars`/`min_atr`，APR 类伪回踩仍会漏网；
  - `t-1` 覆盖启明星/黄昏星的星线，`t-2` 覆盖两K形态的起点；
  - 避免把很久以前的极值当作本次回踩的腿终点。

### 4.2 做多（direction = +1）

1. 在终点窗口内取最低价索引 `end_idx = argmin(low[end_start:t])`（严格不含信号K线）；
2. 在 `[window_start, end_idx)` 内取最高价索引 `start_idx = argmax(high[window_start : end_idx])`；
3. 腿时长 `bars = end_idx - start_idx`；
4. 腿幅 `atr_mult = (high[start_idx] - low[end_idx]) / ATR[end_idx]`；
5. 通过条件：`bars >= pullback_leg_min_bars` 且 `atr_mult >= pullback_leg_min_atr`。

语义：从段内前高下跌到回踩低点，形成一段下跌腿；随后金K确认反转。

### 4.3 做空（direction = -1）

1. 在终点窗口内取最高价索引 `end_idx = argmax(high[end_start:t])`（严格不含信号K线）；
2. 在 `[window_start, end_idx)` 内取最低价索引 `start_idx = argmin(low[window_start : end_idx])`；
3. 腿时长 `bars = end_idx - start_idx`；
4. 腿幅 `atr_mult = (high[end_idx] - low[start_idx]) / ATR[end_idx]`；
5. 通过条件同做多。

语义：从段内前低反弹到回踩高点，形成一段反弹腿；随后看跌金K确认反转。

### 4.4 ATR 与异常处理

- `atrs = atr_series(klines, 14)`，要求 `len(atrs) == len(klines) - 1`；
- `ATR[end_idx]` 必须存在且 `> 0`；
- ATR 不可用或长度不对：**fail-closed**，本次不产生回踩确认/不建 pending，诊断写「ATR 不可用，无法验证反向腿」；
- 不做任何未来数据引用；不使用 `klines[-1]`（未收盘根）。

### 4.5 豁免与停用

- `touch_ref == TOUCH_RETEST`：跳过腿门；
- `pullback_leg_gate_enabled = false`：跳过腿门（生产回滚开关）；
- `pullback_leg_min_bars = 0` 且 `pullback_leg_min_atr = 0`：等价于腿门关闭（兼容用，推荐使用显式开关）。

## 5. 实现改动

### 5.1 `backend/app/services/scan_filters.py`

新增 4 个配置键与 `ScanFilters` 字段：

| JSON key | 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `pullback_leg_gate_enabled` | `pullback_leg_gate_enabled` | bool | `True` | 腿门总开关 |
| `pullback_leg_lookback_bars` | `pullback_leg_lookback_bars` | int | `24` | 腿搜索回看根数（1h） |
| `pullback_leg_min_bars` | `pullback_leg_min_bars` | int | `2` | 反向腿最少持续根数 |
| `pullback_leg_min_atr` | `pullback_leg_min_atr` | float | `0.5` | 反向腿最小幅度（ATR 倍数） |

要求：

1. 将 4 个键加入 `SCAN_FILTER_KEYS`；
2. 在 frozen dataclass `ScanFilters` 中增加字段，默认值如上；
3. 在 `resolve_scan_filters(cfg)` 中用既有 `_bool/_int/_num` 容错解析，缺省回落默认值；
4. 数值下限：`lookback >= 2`、`min_bars >= 0`、`min_atr >= 0.0`；
5. 不新增 settings/env 字段；既有 DB JSONB 配置缺键时自动使用默认值。

### 5.2 `backend/app/services/strategy/trigger.py`

#### 新增导入与常量

```python
from app.services.strategy.technical import atr_series, pullback_stabilize_bonus

# 腿终点搜索窗口：覆盖单K反转与三K形态（启明星/黄昏星的星线）
LEG_END_WINDOW_BARS = 2
```

#### 新增 helper

```python
def _pullback_leg_gate(
    klines: list[list], direction: int, flt: ScanFilters
) -> tuple[bool, dict]:
    """回踩前置反向腿门（2026-10-22）。

    做多要求触点前存在一段下跌腿；做空要求一段反弹腿。
    只用已收盘K线，按 ATR 归一化；返回 (是否通过, 证据字典)。
    """
```

返回证据字典至少包含：

```python
{
    "bars": int,              # 腿时长（根）
    "atr_mult": float,        # 腿幅 / ATR
    "leg_start_idx": int,     # 腿起点索引（closed 口径）
    "leg_end_idx": int,       # 腿终点索引（closed 口径）
    "leg_start_price": float,
    "leg_end_price": float,
    "atr": float,
}
```

失败时 `bars`/`atr_mult` 仍尽量返回可比较数值，便于诊断文案。

#### 接入 `detect_trigger`

在行 1 突破分支之后、行 2 当根确认之前插入：

```python
leg_rejected = False
if (
    touch_ref is not None
    and touch_ref != TOUCH_RETEST
    and flt.pullback_leg_gate_enabled
):
    leg_ok, leg_ev = _pullback_leg_gate(klines, direction, flt)
    if not leg_ok:
        leg_word = "下跌腿" if direction == 1 else "反弹腿"
        _diag_note(
            diag,
            f"回踩{_touch_ref_label(touch_ref, direction)}前无有效{leg_word}："
            f"{leg_ev.get('bars', 0)}根/{leg_ev.get('atr_mult', 0.0):.2f}ATR "
            f"< 门槛 {flt.pullback_leg_min_bars}根/{flt.pullback_leg_min_atr:.2f}ATR",
        )
        touch_ref = None
        touch_price = None
        leg_rejected = True
```

行为要求：

1. **行 2**：`touch_ref` 被置空后不再走当根确认；
2. **行 3**：若存在方向相同且仍在窗口内的有效 pending，仍允许用本根金K确认该 pending（pending 的 Stage-1 触点当时已过腿门）；
3. **行 3 的通用诊断**：当 `leg_rejected=True` 时，不再追加「金K未触发：本根出现同向金K但无回踩触点/待确认语境（纯金K不触发）」，避免与腿门死因重复；
4. **行 5**：`touch_ref` 被置空后不再 upsert pending；
5. **行 6/7**：按无触碰处理，既有 pending 的窗口计时与方向失效逻辑保持不变；
6. **行 1**：突破触发完全不受影响；
7. `direction not in (1, -1)` 时不调用腿门（保持原行为）。

### 5.3 不改动的文件

- `backend/app/services/scanner.py`：`detect_trigger` 返回值语义不变，漏斗诊断通过既有 `diag` 通道落库，无需改。
- `backend/app/models/scan.py`：不新增列，无迁移。
- `backend/app/services/ai_analyzer.py`：提示词与事实包不变。
- 前端：不新增配置项。

## 6. 测试

### 6.1 新增 `backend/tests/test_pullback_leg_gate.py`

覆盖：

1. `test_leg_gate_rejects_short_without_rebound`：下跌中继、1 根反抽 + 看跌吞没 → 无触发、无 upsert，诊断含「无有效反弹腿」；
2. `test_leg_gate_accepts_short_after_rebound`：3 根反弹且幅度 ≥0.5 ATR + 看跌吞没 → `trigger_pullback_confirm`；
3. `test_leg_gate_rejects_long_without_decline`：上涨中继、1 根回撤 + 看涨吞没 → 无触发、无 upsert；
4. `test_leg_gate_accepts_long_after_decline`：3 根下跌且幅度 ≥0.5 ATR + 看涨吞没 → `trigger_pullback_confirm`；
5. `test_leg_gate_exempts_retest`：将 helper 打成拒绝，retest 触点仍触发（验证豁免分支）；
6. `test_leg_gate_disabled_switch`：`pullback_leg_gate_enabled=False` 时平坦K线可正常 upsert/确认；
7. `test_leg_gate_zero_thresholds_disabled`：`min_bars=0` 且 `min_atr=0` 时通过；
8. `test_leg_gate_atr_alignment`：构造已知 ATR 序列，断言 `ATR[end_idx]` 与信号K线口径一致；
9. `test_leg_gate_no_lookahead`：改动未收盘根 `klines[-1]` 不改变判定结果。

夹具复用：新文件内复制 `_klines`、`_ema21_of`、`_set_signal_candle` 的最小实现（约 30 行），避免跨测试文件导入与改动既有夹具。

### 6.2 调整既有测试

- `backend/tests/test_trigger_two_stage.py`：新增 autouse fixture，将 `trig._pullback_leg_gate` 打成 `lambda *a, **k: (True, {"bars": 3, "atr_mult": 1.0})`，隔离矩阵逻辑；矩阵用例语义不变。
- `backend/tests/test_touch_refs.py`：非 retest 触点用例使用平坦夹具，预期受腿门影响；加同样的 autouse bypass，并新增/保留一个「retest 豁免」用例，在该用例内显式把 helper 打成拒绝，断言仍触发。
- `backend/tests/test_scan_funnel.py`、`backend/tests/test_strategies.py`：运行全量测试后，对因平坦夹具导致回踩用例失败的，加同样 bypass；不得为了通过测试而放宽生产默认值。
- `backend/tests/test_scan_filters.py`：新增默认值与覆盖解析断言。

### 6.3 验收命令

```bash
cd /Users/wangwei/Documents/github/trading-workbench/backend
/Users/wangwei/.pyenv/versions/3.11.9/bin/python -m pytest tests/test_pullback_leg_gate.py tests/test_trigger_two_stage.py tests/test_touch_refs.py tests/test_scan_filters.py -q
/Users/wangwei/.pyenv/versions/3.11.9/bin/python -m pytest tests/ -q
```

验收标准：

- 新增用例全部通过；
- 全量测试 0 failed；
- 平坦夹具下的既有矩阵/触点用例通过（通过显式 bypass，而非修改生产默认值）；
- retest 用例在 helper 被打成拒绝时仍能触发。

## 7. 文档同步

1. `docs/信号与形态/02-信号触发器.md`
   - 在「5. 回踩两段式」中补充腿门定义、阈值、豁免与诊断；
   - 在「变更历史」表追加 2026-10-22 行，记录用户拍板与默认阈值。
2. `backend/skills/trend-pullback/SKILL.md`
   - 在入场条件中补一句：回踩前应存在一段反向腿（做多下跌腿/做空反弹腿），无反向腿的反抽不属于本打法。

## 8. 风险、回滚与观察

| 风险 | 影响 | 缓解 |
|---|---|---|
| 过度过滤有效回踩 | 信号数下降 | 默认仅 2 根/0.5 ATR；`retest` 豁免；可配置；一键停用 |
| 浅幅多根反弹漏过 | APR 类伪信号仍可能出现 | 可将 `pullback_leg_min_atr` 提高到 0.8~1.0；先观察漏斗死因分布 |
| ATR 口径错位 | 门槛失真 | 单测锁定 `atr_series` 与已收盘K线对齐；fail-closed |
| 旧 pending 未过门 | 部署后 4 根窗口内少量旧 pending 可确认 | 窗口最长 4 根，自动过期；无需迁移 |
| 配置误填 | 腿门异常 | `_bool/_int/_num` 容错；数值下限；非法值回落默认 |

回滚：

- 首选 `system_config.scan_filters.pullback_leg_gate_enabled = false`；
- 无需回滚代码或数据库。

上线后观察（不在本次执行范围，用户自行部署后）：

- 漏斗「无触发」详情中新增死因的出现频率；
- 回踩确认信号数量与质量分分布；
- 若回踩信号数下降超过预期，优先下调 `min_atr` 或 `lookback`，而不是直接关闭腿门。

## 9. 决策清单（已确认 2026-10-22）

1. **过滤强度：硬过滤**——不满足反向腿不产生回踩确认、不写待确认池，漏斗记录死因。
2. **默认阈值：轻档**——`lookback=24`、`min_bars=2`、`min_atr=0.5`，优先避免过度过滤。
3. **适用范围：除 retest 外全部触点**——EMA21/EMA21(4h)/EMA55/EMA144/摆动结构位均过腿门；`retest` 豁免。
4. **不改前端/数据库/AI 提示词**：参数走既有 `scan_filters` JSONB。
5. **测试隔离**：既有矩阵/触点测试通过显式 bypass 隔离腿门，不修改生产默认值。

实施完成后仅本地 commit，不推送、不部署、不连接服务器。
