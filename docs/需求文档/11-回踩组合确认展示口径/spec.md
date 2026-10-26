# 11 - 回踩组合确认展示口径（Spec）

> 状态：待评审 | 起草 2026-10-26 | 触发案件：REZUSDT.P 2026-10-26 04:00 UTC 命中

## 0. 结论（回答用户）

**判定的没错，是展示把一个「两根K的证据组合」压成了一根K的形态名。**

08:00–12:00（北京）那根 4h 收线前，4h 状态机是 `macro_cross_bull`（宏观金叉后第 9 根，`CROSS_FRESH_BARS=10`），前置门 `htf_state_lock` 双向禁开——11:00 那轮锤形线当根确认的机会被整轮丢掉。12:00 那根 4h 收线使 age 9→10 出交叉窗口，状态落 `aligned_bull`，门开；但信号根已经推进到 03:00 趋势K，锤形线退到 `-3`，命中 `candlestick.resolve_golden_evidence` 规则3「前根锤形线 + 当根趋势K」组合确认，`pattern` 按设计落前根形态。

后端 `pattern=hammer` 是对的，`signal_candle_open=03:00` 也是对的——**两者指向不同的K线**，而前端把 `pattern` 标签钉在 `signal_candle_open` 上，于是看起来像「这根趋势K是锤形线」。

本方案不改判定，只做两件事：
1. **把「形态在哪根、确认在哪根」作为事实落库**，前端画出两个标（主标=信号根，弱色副标=形态根），文案带 `(前根)`；
2. **把组合确认的时间代价显式化**（形态K收盘 → 信号K收盘的漂移 %，本例 +1.59%），让"晚一根"从隐形变可见。

4h 门是否放开另立议题（本案的 11:00 缺失），需回放全样本验证，不在本方案内。

## 1. 背景与现状

### 1.1 用户反馈

> 「查看线上数据 REZUSDT.P，这个币种刚刚命中，显示的锤形线，但是上一根收盘K线是趋势K线，上上一根才是锤形线，分析这个错误」

实测（Binance fapi REZUSDT 1h）：

| 时间(UTC) | OHLC | 实体% | 下影% | 上影% | 判定 |
|---|---|---|---|---|---|
| 10-26 02:00 | O0.004561 H0.004603 L0.004444 C0.004597 | 22.6 | 73.6 | 3.8 | 锤形线（下影/实体 3.25、阳线 → strength 0.9） |
| 10-26 03:00 | O0.004596 H0.004692 L0.004583 C0.004670 | 67.9 | 11.9 | 20.2 | 趋势K（≥60%、顺向） |

用户描述与数据完全一致：形态在 02:00（-3），命中锚在 03:00（-2）。

### 1.2 代码根因

证据链三段，每段单独看都对：

1. `backend/app/services/strategy/candlestick.py:806-823` 规则3：当根无金K、前根锤形线、当根顺向趋势K → 返回 `pattern=前根形态`、`note="前根锤形线+当根趋势K"`。注释自述「pattern 落前根形态（前端展示/AI 提示词兼容）」。
2. `backend/app/services/strategy/trigger.py:1014` / `:1062`：把 `golden["pattern"]` 写进 `trig["pattern"]`，`golden_combo=bool(golden_note)`。
3. `backend/app/services/scanner.py` 落库时 `golden_combo` **没有列可存**，只有 `pattern` 进了 `ScanResult.pattern`；`backend/app/api/scan.py:2009-2028` 的 hit-history 只回 `pattern` + `signal_candle_open`。

于是前端 `frontend/src/components/KlineChart.tsx:1093-1120` 只能：`anchorMs = floor(signal_candle_open / 周期) * 周期`，`label = "命中 · 回踩确认·锤形线"`——**形态名和锚点来自两个不同的K线，且组合语义在传输链路上丢失**（commit `d0b2d94` 追加形态名时引入）。

`frontend/src/components/HitHistoryPanel.tsx:144-147` 的形态列、`backend/app/services/scanner.py:69-116` 的飞书卡片同样是单形态名，三处同口径误读。

### 1.3 现有可复用能力

- `patterns` 由 `detect_all_patterns(klines, idx=-2)` 产出，`golden` 命中哪条规则在函数内是已知的（只差没往外传）。
- `klines[-3][0]` 即形态根开盘时间，`klines[-3][4]` 即形态根收盘价——漂移 % 无需额外数据源。
- `backend/app/database.py:23` 有幂等 `_run_migrations`（`ADD COLUMN IF NOT EXISTS`）惯例，加列零停机、零数据迁移。
- 响应式派生在 `backend/app/services/scanner.py:609-640` 的 `_eval_symbol` 返回 dict 中统一组织，落库与卡片共用同一份 dict。

## 2. 目标

1. 命中历史 / 图表 / 飞书卡片三处一致表明**组合确认的两根K线**：形态根 + 确认根，文案带 `(前根)`。
2. 图表在形态根位置画出弱色副标，主标仍锚信号根（执行时刻不偏移）。
3. 命中条目提供组合确认的时间代价：`pattern_candle_open`、`entry_lag_pct`（方向归一后的不利漂移 %），超阈值 0.8% 打「形态确认滞后」提示。
4. 历史行（无新列数据）行为与现状完全一致，不报错、不误标。
5. 全链路可测：新增/调整单测覆盖组合与非组合两条路径。

## 3. 非目标

1. **不改 4h 前置门**（`htf_context.block_both` 仍只放行 `aligned_*`）。`macro_cross_*` 是否对回踩族放行需单独回放验证（见 §8 观察项）。
2. **不改 pending 待确认池语义**（`pullback_pendings` 表不加列，Stage-1 行三字段恒空）。
3. **不改 AI 提示词与 narrator 文案结构**（`pattern` 语义不变，只多了展示前缀；提示词渲染仍读 `pattern`，`(前根)` 只在人类界面出现）。
4. **不回填历史命中数据**（旧行无 `klines` 快照，无法可靠重算；且这些行已过期）。
5. 不改 `signal_reason`（`combo` 场景 reason 已含「前根锤形线+当根趋势K」，前端不解析该文本）。

## 4. 判定口径

### 4.1 字段口径

| 字段 | 类型 | 含义 | 无证据/旧行时 |
|---|---|---|---|
| `golden_combo` | bool | 本次形态确认是否走组合路径（规则2 双流星 / 规则3 前根+趋势K） | `False` |
| `pattern_candle_open` | datetime | **`pattern` 实际所在K线的开盘时间**（扫描周期 1h，已收盘）；有 `pattern` 即落 | `NULL`（无 pattern 或旧行） |
| `entry_lag_pct` | numeric(10,4) | 形态K收盘 → 信号K收盘的价格漂移，**方向归一**：`direction * (signal_close / pattern_close - 1) * 100`；仅形态在信号根之前时才计算 | `NULL` |

**展示判据不看 `golden_combo`，看两根K是否真的不同**：`(前根)` 文案与形态根副标的唯一条件是
`pattern_candle_open < signal_candle_open`（各自映射到当前图表周期后比较）。`golden_combo` 只作
数据留痕（区分规则2/3），不参与渲染——理由见 §4.2：双流星虽然 `golden_combo=True`，但形态就在
信号根上，不该标「前根」。

等价判据（同一事实的两面，实现时择一即可）：`pattern_candle_open < signal_candle_open`
⇔ `entry_lag_pct IS NOT NULL`（构造上只有当形态早于信号根才计算滞后）。卡片/表格这类拿不到
`signal_candle_open` 的场景用后者；图表因为要按当前周期落槽去重，用前者。

`entry_lag_pct > 0` 恒表示「对本次交易不利的漂移」（多头追高 / 空头追低）。本例 = `+1 * (0.004670/0.004597 - 1) * 100 = +1.588`。

### 4.2 形态根从哪来

`resolve_golden_evidence` 三条返回路径各自的 `offset`：

| 规则 | 证据所在K线 | `offset` | `pattern_candle_open` | `(前根)`/副标 | 例 |
|---|---|---|---|---|---|
| 1 当根金K直通 | `-2` | `-2` | = 信号根开盘 | 否 | 当根看涨吞没 |
| 2 双流星组合 | 当根 `-2`（前根 `-3` 为校验） | `-2` | = 信号根开盘 | 否 | 当根流星 + 前根流星 |
| 3 前根形态 + 当根趋势K | `-3` | `-3` | = 前根开盘 | **是** | 本例锤形线 |

规则2 的 `offset` 取 `-2`：双流星的**确认形态**是当根那颗，前根只是加强；`golden_combo=True`
但锚点与单根一致，前端比较两根时间戳后自然不会加 `(前根)`——不需要前端再判规则。

### 4.3 滞后提示阈值

`COMBO_LAG_WARN_PCT = 0.8`（后端常量，`app/config.py`）。`entry_lag_pct >= 0.8` 时 API 返回 `entry_lag_warn=True`，前端只读该布尔渲染 ⚠，**不在前端重复实现阈值**。

0.8% 的来源：1h ATR 中位量级（本例 02:00 那根振幅 3.4%、收盘成交 0.004597→0.004670 = 1.59%，约为 1/4 根振幅）；阈值取"半根典型振幅以下"以避免噪声告警，同时能抓住本例。

## 5. 实现改动

### 5.1 `backend/app/services/strategy/candlestick.py`

`resolve_golden_evidence`（:752）三条返回路径带上 `offset`，保持 2-tuple 签名不变：

```python
# 规则1
if golden and golden["pattern"] not in SINGLE_SHADOW_PATTERNS:
    return {**golden, "offset": -2}, ""
# 规则2
if prev:
    return {**golden, "offset": -2}, "双流星组合"
# 规则3
return ({"pattern": prev["pattern"], "direction": wanted,
         "strength": prev["strength"], "offset": -3},
        f"前根{label}+当根趋势K")
```

同时更新 docstring：返回的 `golden` 含 `offset`（证据K线相对下标）。

### 5.2 `backend/app/services/strategy/trigger.py`

在 `detect_trigger` 的两处金K确认返回（:1005-1017 行2 当根触碰、:1050-1064 行3/行4 pending 确认）增三键：

```python
_pat_off = golden.get("offset", -2)
# 形态K与滞后：pattern_candle_open 有形态即落；滞后仅在形态早于信号根时才有意义
_pattern_open = datetime.utcfromtimestamp(float(klines[_pat_off][0]) / 1000)
_lag = None
if _pat_off == -3:
    _pat_close = float(klines[_pat_off][4])
    _sig_close = float(klines[-2][4])
    _lag = round(direction * (_sig_close / _pat_close - 1) * 100, 4)
return {
    ...,
    "golden_combo": bool(golden_note),
    "pattern_candle_open": _pattern_open,
    "entry_lag_pct": _lag,
    ...
}
```

`direction` 沿用函数入参（1/-1），与 `_hit_direction` 同符号口径。行5 的 pending upsert 分支不产出
这三个键（首触根尚无形态）。

### 5.3 `backend/app/models/scan.py`

`ScanResult` 增三列（全部可空/有默认，避免旧行与 `watchlist.py:236` 的手写插入出错）：

```python
# 组合确认口径（2026-10-26，docs/11）：回踩金K走「前根形态+当根趋势K」时，
# pattern 落在信号根的前一根——本列标记组合路径，pattern_candle_open 指向
# 形态真实所在K线，entry_lag_pct 记录确认滞后导致的不利价格漂移
golden_combo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
pattern_candle_open: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
entry_lag_pct: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)
```

### 5.4 `backend/app/database.py`

`_run_migrations` 追加（沿用现有注释风格，注明版本）：

```python
# scan_results 加组合确认口径列（2026-10-26，docs/11）：回踩金K「前根形态+
# 当根趋势K」时 pattern 所在K线与滞后漂移；旧行 golden_combo=FALSE、其余 NULL
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS golden_combo BOOLEAN NOT NULL DEFAULT FALSE"
))
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS pattern_candle_open TIMESTAMP"
))
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS entry_lag_pct NUMERIC(10,4)"
))
```

### 5.5 `backend/app/services/scanner.py`

三处：

1. `_eval_symbol` 返回 dict（:609-640）增 `"golden_combo": bool(trig.get("golden_combo"))`、`"pattern_candle_open": trig.get("pattern_candle_open")`、`"entry_lag_pct": trig.get("entry_lag_pct")`。
2. `_save_results`（:1112）`ScanResult(...)` 增同名三参。
3. `_scan_hit_card`（:89-95）形态文案：形态早于信号根时追加 `(前根)`（用 `entry_lag_pct`
   等价判据，卡片 dict 里没有 `signal_candle_open`）：

```python
if h.get("pattern"):
    sig += f"·{PATTERN_LABEL_MAP.get(h['pattern'], h['pattern'])}"
    if h.get("entry_lag_pct") is not None:
        sig += "(前根)"
```

（卡片三列 `flex_mode=trisect` 等分，`(前根)` 增 6 字符；上线后目测一次换行情况。）

### 5.6 `backend/app/api/scan.py`

`hit-history`（:2009-2028）items 增三字段：

```python
"golden_combo": bool(r.golden_combo),
"pattern_candle_open": r.pattern_candle_open.isoformat() if r.pattern_candle_open else None,
"entry_lag_pct": float(r.entry_lag_pct) if r.entry_lag_pct is not None else None,
"entry_lag_warn": (r.entry_lag_pct is not None
                   and float(r.entry_lag_pct) >= COMBO_LAG_WARN_PCT),
```

### 5.7 `frontend/src/types/index.ts`

`HitHistoryItem`（:370-390）增：`golden_combo: boolean`、`pattern_candle_open: string | null`、`entry_lag_pct: number | null`、`entry_lag_warn: boolean`。

### 5.8 `frontend/src/components/KlineChart.tsx`

1. `EffHit`（:636-646）增 `goldenCombo: boolean`、`patternCandleOpen: string | null`；外部 `Props` 增 `hitGoldenCombo?: boolean`、`hitPatternCandleOpen?: string | null`；`effHits` 两处归一（:756 外部、:767 自动拉取）补齐。
2. 标注类型 `kind` 扩为 `'hit' | 'touch' | 'pattern'`（:410 / :429 / :1093）。`pattern` 走弱色：新增 `PATTERN_MARK_COLOR = '#8b93a7'`、`PATTERN_MARK_FILL = 'rgba(139,147,167,0.10)'`，`drawHitMarks` 中 `m.kind === 'pattern'` 的处理与 `touch` 同形（:479 / :605 的分支复制），仅颜色/字号降级。
3. `drawZones`（:1105-1118）：

```tsx
// (前根) 与副标由两根K是否真的不同派生，不看 goldenCombo：
// 4h/1d 图上两根常落同一根（映射后相等）→ 自然只留主标
const pSlotMs = h.patternCandleOpen
  ? Math.floor(utcMs(h.patternCandleOpen) / ivMs) * ivMs : null
const onPrev = pSlotMs != null && sigMs != null && pSlotMs < anchorMs
const hitLabel = parts.length
  ? `命中 · ${parts.join('·')}${onPrev ? '(前根)' : ''}` : '命中'
...
if (onPrev) {
  marks.push({ ms: pSlotMs, color: PATTERN_MARK_COLOR, fill: PATTERN_MARK_FILL,
               label: patternLabel ? `形态根·${patternLabel}` : '形态根',
               kind: 'pattern', preferBelow })
}
```

4. `overlayFrom`（:1064-1078）把 `patternCandleOpen` 并入候选时间集，保证副标在可视窗口内。

### 5.9 `frontend/src/components/HitHistoryPanel.tsx`

1. 形态列（:143-150）：`r.entry_lag_pct != null`（=形态早于信号根）时渲染 `前根·{label}`
   （用 `<Tag>` 内文本前缀，不加新列）。
2. 详情抽屉（:270-300）：`KlineChart` 增传 `hitGoldenCombo` / `hitPatternCandleOpen`；时间信息区追加：

```tsx
{detail.pattern_candle_open && detail.entry_lag_pct != null && (
  <span>
    <Tag color="#8b93a7">形态K</Tag>
    {bj(detail.pattern_candle_open).format('YYYY-MM-DD HH:mm')}
    {detail.entry_lag_pct != null && (
      <Text type={detail.entry_lag_warn ? 'warning' : 'secondary'}>
        （确认滞后 {detail.entry_lag_pct > 0 ? '+' : ''}{detail.entry_lag_pct.toFixed(2)}%
        {detail.entry_lag_warn ? ' ⚠' : ''}）
      </Text>
    )}
  </span>
)}
```

### 5.10 不改动的文件

- `backend/app/services/strategy/technical.py`（4h 状态机 / `block_both` / 质量分）
- `backend/app/services/strategy/ema_gate.py`
- `backend/app/models/scan.py` 的 `PullbackPending`（Stage-1 不加列）
- AI 提示词相关（`ai_analyzer.py` / `narrator.py`）

## 6. 测试

### 6.1 `backend/tests/test_trigger_two_stage.py`（复用既有 `_kl/_base/_hammer_at/_trend_candle_at/_shooting_star_at` 夹具，不另起文件）

在 `test_hammer_plus_trend_candle_confirms` / `test_double_shooting_star_confirms_short`
等既有用例上追加断言，并给 `resolve_golden_evidence` 直测区补 `offset` 断言：

| 用例 | 断言 |
|---|---|
| 规则1 直通（当根吞没） | `golden["offset"] == -2`；`golden_combo is False`；`pattern_candle_open == klines[-2] 开盘`；`entry_lag_pct is None` |
| 规则2 双流星 | `offset == -2`；`golden_combo is True`；`pattern_candle_open == klines[-2] 开盘`；`entry_lag_pct is None`（形态在当根，无滞后） |
| 规则3 组合（前根锤 + 当根趋势K） | `golden["offset"] == -3`；`golden_combo is True`；`pattern_candle_open == klines[-3] 开盘`；`entry_lag_pct ≈ 1.588` |
| 空头镜像（前根流星 + 当根阴趋势K） | `entry_lag_pct` 在入场对空单不利时为正（信号收盘低于形态收盘 → `-1 × 负值 = 正`） |

### 6.2 既有测试文件

- `backend/tests/test_hit_history.py`：增三字段存在性 + 旧行（三列 NULL）回退断言
  （`golden_combo=False`、其余 `None`、`entry_lag_warn=False`）+ 组合行 `entry_lag_warn` 触发断言。
- `backend/tests/test_scan_hit_card.py`：`entry_lag_pct` 非空时列文案带 `(前根)`，为空时不带。

### 6.3 验收命令

```bash
cd backend && python -m pytest tests/test_trigger_two_stage.py tests/test_hit_history.py tests/test_scan_hit_card.py -q
cd frontend && npx tsc --noEmit && npm run build
node scripts/build_docs_wiki.mjs --quiet
```

## 7. 文档同步

| 文档 | 改动 |
|---|---|
| `docs/信号与形态/02-信号触发器.md` | §5.4 规则3 补「展示口径」：`golden_combo` / `pattern_candle_open` / `(前根)` 文案约定 |
| `docs/信号与形态/11-金K形态库.md` | 补组合确认的锚点与滞后字段说明 |
| `docs/前端与查询/14-命中历史查询.md` | hit-history 响应字段表增四字段 + 副标规则（含 4h/1d 重合去重） |
| `docs/前端与查询/08-前端界面.md` | 图表标注图例增「形态根」弱色副标 |
| `docs-wiki/` | `node scripts/build_docs_wiki.mjs`（增量重生成） |

## 8. 风险、回滚与观察

| 风险 | 处置 |
|---|---|
| 迁移时间 | 三列均可空/有默认，`ADD COLUMN IF NOT EXISTS` 幂等；启动即迁移，无需停服 |
| 4h/1d 图两标重合 | 已去重（`pSlotMs < anchorMs` 才画副标），只留主标 |
| 旧前端读新后端 | 新字段多出即忽略，无破坏 |
| 新前端读旧后端（回滚中） | `golden_combo`/`entry_lag_pct` 缺失走 `undefined` → falsy，等同现状 |
| 回滚 | 回退代码即可；三列留在库内无害 |
| 飞书卡片换行 | 上线后目测一条含 `(前根)` 的卡片 |

**观察项（本方案外，另立议题）**：11:00 那轮 `htf_state_lock` 拦掉的锤形线当根确认——`technical.py` 的质量分矩阵把 `macro_cross_*` 当趋势级事件给 +12 最高档，与前置门整态拒绝自相矛盾。是否对**回踩族**放开 `macro_cross_*`（突破族保持只放行 `aligned_*`），须先跑 `backend/scripts/` 下的回放脚本在全样本上量化命中数/胜率/入场漂移，再拍板。

## 9. 决策清单（待确认）

1. 锚点不动（主线仍锚信号根，形态根加弱色副标），还是反向（主线锚形态根）？→ 建议不动：命中的执行时刻是信号根，挪走会与开单/成交图标错位。
2. 文案形态：`回踩确认·锤形线(前根)`（半角括号、追加在后）→ 建议确认。
3. `entry_lag_pct` 阈值 0.8% 与后端单点返回 `entry_lag_warn` → 建议确认。
4. `(前根)` 与形态根副标由 `pattern_candle_open < signal_candle_open` 派生（不看 `golden_combo`），
   双流星等同锚不自标 → 建议确认。
