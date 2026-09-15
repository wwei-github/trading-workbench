# 09 - 金K形态与Donchian突破信号及摆动点锚定设计（关键位功能移除方案）

> **状态**：2026-09-14 方案稿（已实施）
>
> **背景**：关键位（摆动点聚合支撑/压力线，docs/08）是系统的核心枢纽，也是唯一的信号触发器。用户决定**彻底去掉关键位功能**（线、逻辑、限制全删）——信号触发与风控锚定全部换掉，摆动点结构分类与 EMA 门控保留。
>
> **拍板决策（2026-09-14）**：
> 1. **信号触发** = 12金K 形态（最近已收盘K线出现 GOLDEN_12 形态）+ N 根 Donchian 通道突破（量能确认）；摆动点结构分类 + EMA 严格门控保留。
> 2. **风控** = 保留铁律、仅换锚——方向铁律、止损/止盈锚定形状全不变，锚从"关键位聚合线"换成**摆动点（recent_swings）+ 近端影线极值**。
> 3. **（2026-09-15 拍板：校验链收缩）** 上述锚定规则随后整体退出程序强制——② AI 校验链仅保留 **RR 铁律（复算 ≥1.5）** + **双评委辩论复核（默认开）** 两条；止损/止盈锚定与方向纪律降级为 AI 提示词方法论，程序不再打回/改挂（开仓闸门仍复核方向-价格、偏离 ≤1%、RR 按现价、强平闸门）。本文的锚定算法保留为提示词方法论与历史设计存档。

---

## 1. 信号触发（strategy/_detect 重写）

前置（与旧版一致）：K线数 ≥30、摆动点 ≥4 个 → 结构分类（signal_type 标签）→ EMA 状态。

### 1.1 Donchian 通道突破（优先）

- 通道 = 信号K线**之前** N 根已收盘K线的最高价/最低价（影线口径）：窗口 `klines[-2-chan_n : -2]`，恰 N 根，**不含信号K线自身、不含未收盘根**（防"自己破自己"）；
- `N = BREAKOUT_CHANNEL_BARS`（env 可覆盖，默认 20；不进 SystemConfig/DB）；
- 触发：信号K线收盘**严格**越过上/下轨 + `volume_ratio ≥ BREAKOUT_VOL_RATIO(1.2)` + EMA 4 态且与突破方向同向；
- 信号：signal_type=breakout，position=向上 resistance / 向下 support，breakout_pct 从轨起算，strength = 0.7（倍量 2× +0.1）+ 0.1(EMA)；
- 量能/EMA 不过 → 落到形态路径。

### 1.2 12金K 形态路径

- `detect_all_patterns(klines, idx=-2)` 首个 GOLDEN_12 形态（6 种：锤形线/看涨吞没/启明星/上吊线/看跌吞没/黄昏星）；
- EMA 严格门控（state ∈ 4 态 且 bias 与形态方向同向）；
- 信号：position = 看涨→support / 看跌→resistance，breakout_pct=0，strength = 形态强度 + 0.1(EMA) + 0.1(趋势反转加权)；reason = 位置标签 + 形态标签。

### 1.3 信号 dict（新旧对比）

保留：signal_type / position / current_price / breakout_pct / trend_slope / r_squared / pattern / pattern_direction / signal_reason / ema_state / strength / reversal。
**删除：`key_levels`、`hit_level` 两个键。**

`scan_results.position` 列**保留**，语义变为形态/突破方向派生（看涨形态→support、看跌→resistance；突破向上→resistance、向下→support）——列本身语义没变，历史行照常展示。

## 2. 风控换锚（risk_guard.py）

规则形状全保留，只换锚来源：

| 规则 | 旧锚 | 新锚 |
|---|---|---|
| 方向铁律 | 入场贴近对应角色关键位线 ±0.3% ±0.25×ATR | ~~入场贴近摆动低点/高点或最近5根影线极值 ×(1±0.3%)~~（**距离校验 2026-09-15 移除**——±0.3% 贴近要求不再打回，纪律降级到提示词） |
| 止损锚定 | 入场侧最近关键位线 vs 5根极值取更远者，+0.3% 缓冲，<0.3×ATR 弃用 | ~~**入场价下方最近摆动低点（多）/上方最近摆动高点（空）** vs 5根极值取更远者，其余不变~~（**程序强制 2026-09-15 移除**，降级为提示词方法论） |
| 止盈锚定 | 前方第一/下一档关键位线；≤1×ATR 近线不作目标；越线拉回 0.2%；无位回退固定 RR | ~~**recent_swings(n=50) 前方摆动点**（多=高点升序、空=低点降序）；近位过滤/拉回/固定 RR 兜底全保留；新增近距摆动点合并~~（**程序强制 2026-09-15 移除**——最近优先/改挂/固定 RR 兜底均不再执行，降级为提示词方法论） |

`ai_agent` 锚点枚举（LEVEL_REFS）不变（历史命名），解析改为摆动点：`resistance_zone_low` → 现价上方最近摆动高点、`support_zone_high` → 现价下方最近摆动低点、`next_*` → 下一档；`signal["recent_swings"]` 由 ai_tasks 分析时注入（n=20，已有）。

## 3. 移除面清单

| 层 | 移除 | 保留 |
|---|---|---|
| strategy/ | key_levels.py 整个文件；`compute_signal_key_levels` / `detect_any_signal` | recent_swings、EMA 门控、candlestick、structure、swing |
| indicators | — | calc_atr / volume_ratio / fmt_price / BREAKOUT_VOL_RATIO 迁至新模块 `strategy/indicators.py`（叶子模块） |
| ai_tasks | fingerprint 最近关键位分量；分析时关键位重算块 | 指纹其余分量（symbol/type/position/pattern/0.5%价格桶）；recent_swings 注入 |
| ai_agent / ai_analyzer | 关键位列表事实段、touches/pattern_hits 可靠性规则 | 摆动结构段、全部锚定纪律规则 |
| models/schemas | `scan_results.key_levels`、`system_config.key_level_tolerance` / `level_merge_threshold` 列映射与字段（**DB 列不 DROP**，历史行不动） | position、swing_order、fib_enabled |
| api/scan | 图表端点关键位块；config GET/PUT 两参数 | 图表摆动标记（swings）；手动分析 recent_swings 注入 |
| scanner / watchlist | config dict 两键；落库 key_levels | volume_ratio（改从 indicators 导入） |
| skill_library | build_facts 的 key_levels 回退分支 | position 快速路径（role 语义不变） |
| 前端 | KeyLevel 类型、图表关键位画线/切换按钮、配置两字段、positionTag tooltip | 摆动点标记、AI 标注层、position 筛选 |
| docs/skills | docs/03、08 加废弃横幅；docs/04/06/07 按新口径修订；技能措辞"关键位"→"结构位（摆动点）" | 新建本方案稿（docs/09） |

## 4. 行为变化（须知）

| 变化 | 后果 |
|---|---|
| 触发面向"纯形态"放开 | 任意 GOLDEN_12 + EMA 同向即出信号，信号量明显上升 → LLM 调用量上升 |
| 铁律实际覆盖变严 | ~~5 根极值恒为合格锚，远离结构的入场会被打回（旧版"无关键位放行"的口子没了）~~（随 2026-09-15 校验链收缩失效：止损/入场锚定不再程序强制） |
| 止盈锚只剩摆动点 | 创新高突破前方无摆动点 → 固定 RR 兜底（TP1=entry±1.5×止损距离、TP2=0） |
| 形态信号强度下限 0.6 | GOLDEN_12 基础强度 ≥0.5 + EMA 0.1（AI_MIN_STRENGTH 强度闸门 2026-09-15 已移除，全部信号交 AI 分析） |
| 指纹组成变化 | 旧指纹 60min TTL 内自然失效，部署后一轮全量 miss（一次性） |
| DB 无迁移 | 只删列映射不 DROP；`scan_results.key_levels` 新行写 NULL，历史行照常 |

## 5. 验证清单

1. `python -m compileall app` + 全模块导入 sweep（backend、celery_worker）；
2. grep 关键位符号清零（只允许 database.py 注释残留）；
3. `_detect` 微验证（合成K线）：破20根通道高+1.3×量+多头EMA → breakout/resistance/无 key_levels 键；信号K线自身即通道高 → 不触发；末根锤子线+多头EMA → 形态信号/support/strength≥0.6；
4. 前端 `npx tsc --noEmit && npm run build`；
5. 部署后：config GET/PUT 往返、klines 端点有 swings 无 key_levels、手动扫描行语义、单币强制 AI 分析走通、DB 三列仍在且新行 key_levels 为 NULL。
