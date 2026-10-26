# 11 - 回踩组合确认展示口径：实施任务

> 前置：用户已批准 `spec.md`（2026-10-26）。纪律：不改判定逻辑（4h 门 / 质量分 / 提示词）、
> 不回填历史行、每任务红→绿→提交，逐任务可独立验收。

## 全局约束

- 每个任务的测试必须先失败再实现（TDD），失败输出的报错文本要贴进提交说明前的自检里。
- 三列全部可空或有默认（旧行 + `watchlist.py` 手写插入不能因此报错）。
- 展示判据统一为「形态根早于信号根」：`pattern_candle_open < signal_candle_open`
  ⇔ `entry_lag_pct IS NOT NULL`；`golden_combo` 只留痕，不参与渲染。
- 前端不重复实现阈值，只读后端 `entry_lag_warn`。
- 中文注释沿用仓库风格：写「为什么」+ 日期 + `docs/11`。

---

## T1. 证据层：`offset` + 触发返回三键

**文件**
- 改：`backend/app/services/strategy/candlestick.py`（`resolve_golden_evidence`，:752 起，规则1 :806-808 / 规则2 :810-818 / 规则3 :820-823）
- 改：`backend/app/services/strategy/trigger.py`（行2 返回 :1005-1017、行3/行4 返回 :1050-1064）
- 测：`backend/tests/test_trigger_two_stage.py`（复用 `_kl/_base/_hammer_at/_trend_candle_at/_shooting_star_at`，:761-795）

**接口**
- 产出：`golden` dict 新增键 `offset: int`（`-2` 当根 / `-3` 前根），签名不变（仍 2-tuple）。
- 产出：`detect_trigger` 返回 dict 新增 `golden_combo: bool`、`pattern_candle_open: datetime | None`、`entry_lag_pct: float | None`（仅形态在 `-3` 时非空）。

**步骤**

- [ ] 写失败测试：`resolve_golden_evidence` 三条规则的 `offset`

```python
def test_resolve_rule_offsets():
    """docs/11：三条规则各自标注证据K线相对下标，供下游定位形态根"""
    kl = _kl(*_base())
    g1, n1 = trig.resolve_golden_evidence(
        kl, "bullish", [{"pattern": "bullish_engulfing", "direction": "bullish"}])
    assert (g1["offset"], n1) == (-2, "")
    kl2 = _kl(*_base())
    _shooting_star_at(kl2, -3)
    _shooting_star_at(kl2, -2)
    g2, n2 = trig.resolve_golden_evidence(
        kl2, "bearish", [{"pattern": "shooting_star", "direction": "bearish"}])
    assert (g2["offset"], n2) == (-2, "双流星组合")
    kl3 = _kl(*_base())
    _hammer_at(kl3, -3)
    _trend_candle_at(kl3, -2, direction=1)
    g3, n3 = trig.resolve_golden_evidence(
        kl3, "bullish", [{"pattern": "trend_candle", "direction": "bullish"}])
    assert (g3["offset"], g3["pattern"]) == (-3, "hammer")
    assert n3 == "前根锤形线+当根趋势K"
```

- [ ] 写失败测试：触发返回三键与滞后公式

```python
def test_combo_trigger_reports_pattern_anchor_and_lag():
    kl = _kl(*_base())
    _hammer_at(kl, -3)
    _trend_candle_at(kl, -2, direction=1)
    t, _op = trig.detect_trigger(kl, 1)
    assert t["golden_combo"] is True
    assert t["pattern_candle_open"] == datetime.utcfromtimestamp(float(kl[-3][0]) / 1000)
    expect = (float(kl[-2][4]) / float(kl[-3][4]) - 1) * 100  # 多头：收盘更低=更便宜=负
    assert t["entry_lag_pct"] == pytest.approx(expect, abs=1e-4)
    assert t["entry_lag_pct"] < 0


def test_single_pattern_trigger_has_no_lag():
    kl = _kl(*_base())
    _hammer_at(kl, -2)          # 当根锤形直通（规则1，offset=-2）
    t, _op = trig.detect_trigger(kl, 1)
    assert t is not None and t["golden_combo"] is False
    assert t["pattern_candle_open"] == datetime.utcfromtimestamp(float(kl[-2][0]) / 1000)
    assert t["entry_lag_pct"] is None
```

- [ ] 跑测试确认失败

```bash
cd backend && python -m pytest tests/test_trigger_two_stage.py -q -k "offset or lag"
```

预期：`KeyError: 'offset'`（规则1 返回 dict 尚无该键）、`KeyError: 'pattern_candle_open'`。

- [ ] 实现 `candlestick.resolve_golden_evidence` 三处返回

```python
# 规则1（:806-808）
return {**golden, "offset": -2}, ""
# 规则2（:810-818）
if prev:
    return {**golden, "offset": -2}, "双流星组合"
# 规则3（:820-823）
return ({"pattern": prev["pattern"], "direction": wanted,
         "strength": prev["strength"], "offset": -3},
        f"前根{label}+当根趋势K")
```

并在 docstring 补一行：`返回 (golden, note)；golden 含 offset（证据K线相对下标：-2 当根 / -3 前根），
供 trigger 定位形态K线（docs/11 组合展示口径）`。

- [ ] 实现 `trigger.detect_trigger` 两处返回新增三键（行2 与行3/行4 各一份，代码相同）

```python
# 组合确认口径（2026-10-26，docs/11）：pattern 落在信号根之前时，把形态真实
# 所在K线与确认滞后一起送出去；形态在当根（offset=-2）时 pattern_candle_open
# 就是信号根，滞后无意义置 None
_pat_off = golden.get("offset", -2)
_pattern_open = datetime.utcfromtimestamp(float(klines[_pat_off][0]) / 1000)
_lag = None
if _pat_off == -3:
    _lag = round(
        direction * (float(klines[-2][4]) / float(klines[_pat_off][4]) - 1) * 100, 4)
```

返回 dict 里对应位置加：`"pattern_candle_open": _pattern_open, "entry_lag_pct": _lag,`（`golden_combo`
键两处已存在，不用改）。

- [ ] 跑测试确认通过

```bash
cd backend && python -m pytest tests/test_trigger_two_stage.py -q
```

- [ ] 提交

```bash
git add backend/app/services/strategy/candlestick.py backend/app/services/strategy/trigger.py backend/tests/test_trigger_two_stage.py
git commit -m "feat(11): 金K证据带 offset，触发返回形态K线与确认滞后"
```

---

## T2. 落库层：三列 + 迁移 + 扫描写入

**文件**
- 改：`backend/app/models/scan.py`（`ScanResult`，:31-79，插在 `signal_created_at` 之前）
- 改：`backend/app/database.py`（`_run_migrations`）
- 改：`backend/app/services/scanner.py`（`_eval_symbol` 返回 dict :611-645、`_save_results` :1112 起）

**接口**
- 消费：T1 的 `trig["golden_combo"|"pattern_candle_open"|"entry_lag_pct"]`。
- 产出：`ScanResult.golden_combo: bool`、`ScanResult.pattern_candle_open: datetime | None`、`ScanResult.entry_lag_pct: float | None`。

**步骤**

- [ ] 写失败测试（放 `backend/tests/test_hit_history.py` 末尾，`_mk_result` 已能构造行）

> 2026-10-26 口径修正：本任务只验**落库层**（DB 写入/读取），API 响应字段
> （`golden_combo`/`pattern_candle_open`/`entry_lag_pct`/`entry_lag_warn`）属于 T3，
> 断言放在 T3 的 `test_entry_lag_warn_threshold`，保证两个任务各自能独立变绿。

```python
def test_combo_columns_persist(client):
    """docs/11：组合确认三列可写可读；非组合行恒为 False/None（API 断言见 T3）"""
    _http, db = client
    rec = ScanRecord(scan_type="scheduled")
    db.add(rec)
    db.flush()
    ts = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    combo = _mk_result(db, rec.id, "REZUSDT", ts)
    combo.signal_created_at = ts
    combo.golden_combo = True
    combo.pattern_candle_open = ts - timedelta(hours=2)
    combo.entry_lag_pct = 1.588
    plain = _mk_result(db, rec.id, "BTCUSDT", ts - timedelta(minutes=1))
    db.commit()
    db.refresh(combo)
    db.refresh(plain)
    assert combo.golden_combo is True
    assert combo.pattern_candle_open == ts - timedelta(hours=2)
    assert float(combo.entry_lag_pct) == pytest.approx(1.588)
    assert plain.golden_combo is False
    assert plain.pattern_candle_open is None
    assert plain.entry_lag_pct is None
```

- [ ] 跑测试确认失败

```bash
cd backend && python -m pytest tests/test_hit_history.py -q -k combo_columns
```

预期：`TypeError: 'golden_combo' is an invalid keyword argument for ScanResult`。

- [ ] 加模型列（`ScanResult`，紧跟 `signal_created_at` 注释块之后）

```python
# 组合确认口径（2026-10-26，docs/11）：回踩金K走「前根形态+当根趋势K」时
# pattern 落在信号根的前一根。golden_combo 标记组合路径（规则2 双流星 /
# 规则3 前根+趋势K，仅留痕）；pattern_candle_open 指向 pattern 真实所在K线
# （有 pattern 即落，形态在当根时=信号根）；entry_lag_pct = 形态收盘→信号
# 收盘的方向归一漂移（正=对本次交易不利），仅形态早于信号根时计算。旧行
# golden_combo=FALSE、其余 NULL
golden_combo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
pattern_candle_open: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
entry_lag_pct: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)
```

（`models/scan.py` 未导入 `text`，故不用 `server_default`——旧行的默认值由迁移里的
`DEFAULT FALSE` 承担；sqlite 内存库由 `Base.metadata.create_all` 建表且 ORM 插入自带该值，
测试不依赖 `_run_migrations`。）

- [ ] 加迁移（`backend/app/database.py` 的 `_run_migrations` 末尾，沿用既有 `conn.execute(text(...))` 风格）

```python
# scan_results 组合确认三列（2026-10-26，docs/11）：旧行 golden_combo=FALSE、
# pattern_candle_open/entry_lag_pct NULL，前端据此回退到「单形态、锚信号根」
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS golden_combo BOOLEAN NOT NULL DEFAULT FALSE"))
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS pattern_candle_open TIMESTAMP"))
conn.execute(text(
    "ALTER TABLE scan_results "
    "ADD COLUMN IF NOT EXISTS entry_lag_pct NUMERIC(10,4)"))
```

- [ ] 扫描写入：`_eval_symbol` 返回 dict 的 `"pattern"` 行之后加三键

```python
"golden_combo": bool(trig.get("golden_combo")),
"pattern_candle_open": trig.get("pattern_candle_open"),
"entry_lag_pct": trig.get("entry_lag_pct"),
```

- [ ] 扫描写入：`_save_results` 的 `pattern=h.get("pattern"),` 之后加三参

```python
golden_combo=bool(h.get("golden_combo")),
pattern_candle_open=h.get("pattern_candle_open"),
entry_lag_pct=h.get("entry_lag_pct"),
```

- [ ] 跑测试确认通过

```bash
cd backend && python -m pytest tests/test_hit_history.py -q
```

- [ ] 提交

```bash
git add backend/app/models/scan.py backend/app/database.py backend/app/services/scanner.py backend/tests/test_hit_history.py
git commit -m "feat(11): scan_results 落组合确认三列（迁移幂等）"
```

---

## T3. 接口层：hit-history 四字段 + 阈值常量

**文件**
- 改：`backend/app/config.py`（`REPEAT_WINDOW_HOURS` 附近，:42）
- 改：`backend/app/api/scan.py`（hit-history items :2009-2028）

**接口**
- 产出：`GET /api/scans/hit-history` 每项多 `golden_combo: bool`、`pattern_candle_open: str | null`、
  `entry_lag_pct: float | null`、`entry_lag_warn: bool`。
- 产出：`config.settings.COMBO_LAG_WARN_PCT = 0.8`。

**步骤**

- [ ] 写失败测试（追加到 `test_hit_history.py`）

```python
def test_entry_lag_warn_threshold(client):
    """docs/11：滞后 ≥ COMBO_LAG_WARN_PCT(0.8%) 才告警；负值（更便宜）不告警"""
    http, db = client
    rec = ScanRecord(scan_type="scheduled")
    db.add(rec)
    db.flush()
    ts = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for i, lag in enumerate((1.59, 0.79, -2.0)):
        r = _mk_result(db, rec.id, f"LAG{i}USDT", ts - timedelta(minutes=i))
        r.signal_created_at = ts - timedelta(minutes=i)
        r.golden_combo = True
        r.pattern_candle_open = ts - timedelta(minutes=i, hours=1)
        r.entry_lag_pct = lag
    db.commit()
    res = http.get("/api/scans/hit-history", params={
        "start": (ts - timedelta(days=1)).isoformat() + "Z",
        "end": (ts + timedelta(minutes=1)).isoformat() + "Z",
    }).json()
    warn = {it["symbol"]: it["entry_lag_warn"] for it in res["items"]}
    assert warn == {"LAG0USDT": True, "LAG1USDT": False, "LAG2USDT": False}
    # 响应字段存在性 + 原值透传（2026-10-26 口径修正：落库断言在 T2，这里只验 API 层）
    by_sym = {it["symbol"]: it for it in res["items"]}
    assert by_sym["LAG0USDT"]["golden_combo"] is True
    assert by_sym["LAG0USDT"]["entry_lag_pct"] == pytest.approx(1.59)
    assert by_sym["LAG0USDT"]["pattern_candle_open"] == (
        ts - timedelta(hours=1)).isoformat()
```

- [ ] 跑测试确认失败：`python -m pytest tests/test_hit_history.py -q -k lag_warn` → `KeyError: 'entry_lag_warn'`

- [ ] 加配置常量（`config.py`，紧跟 `REPEAT_WINDOW_HOURS`）

```python
# 组合确认滞后告警线（%，2026-10-26，docs/11）：回踩金K「前根形态+当根趋势K」
# 组合确认会比当根确认晚一根K线，入场价随之漂移。本例 REZUSDT 漂移 1.59%、
# 止损不变，等于白让 1.59% 成本。取 0.8（<半根 1h 典型振幅）避免噪声告警
COMBO_LAG_WARN_PCT: float = 0.8
```

- [ ] 接口补字段（`backend/app/api/scan.py` items 的 `"pattern": r.pattern,` 之后）

```python
# 组合确认展示口径（2026-10-26，docs/11）：形态真实所在K线 + 确认滞后；
# entry_lag_warn 由后端单点判定，前端只渲染不重算阈值
"golden_combo": bool(r.golden_combo),
"pattern_candle_open": r.pattern_candle_open.isoformat() if r.pattern_candle_open else None,
"entry_lag_pct": float(r.entry_lag_pct) if r.entry_lag_pct is not None else None,
"entry_lag_warn": (r.entry_lag_pct is not None
                   and float(r.entry_lag_pct) >= settings.COMBO_LAG_WARN_PCT),
```

（`settings` 已在 `scan.py` 导入；若该文件用局部 `cfg = get_settings()` 的写法，则用同一变量名。）

- [ ] 跑测试确认通过

```bash
cd backend && python -m pytest tests/test_hit_history.py -q
```

- [ ] 提交

```bash
git add backend/app/config.py backend/app/api/scan.py backend/tests/test_hit_history.py
git commit -m "feat(11): hit-history 返回形态根与确认滞后，阈值后端单点判定"
```

---

## T4. 飞书命中卡片：`(前根)`

**文件**
- 改：`backend/app/services/scanner.py`（`_scan_hit_card`，:89-95）
- 测：`backend/tests/test_scan_hit_card.py`

**步骤**

- [ ] 写失败测试

```python
def test_card_marks_prev_bar_pattern():
    """docs/11：形态在信号根之前时形态名带 (前根)；单形态不带"""
    card = _scan_hit_card([
        {**_hit("REZUSDT", "trigger_pullback_confirm", "hammer", 82),
         "golden_combo": True, "entry_lag_pct": 1.588},
        {**_hit("BTCUSDT", "trigger_pullback_confirm", "hammer", 70),
         "golden_combo": False, "entry_lag_pct": None},
    ])
    assert card["elements"][0]["columns"][1]["elements"][0]["content"] == "回踩金K确认·锤形线(前根)"
    assert card["elements"][1]["columns"][1]["elements"][0]["content"] == "回踩金K确认·锤形线"
```

- [ ] 跑测试确认失败

```bash
cd backend && python -m pytest tests/test_scan_hit_card.py -q -k prev_bar
```

预期：列文案为 `回踩金K确认·锤形线`（缺 `(前根)`）→ AssertionError。

- [ ] 实现

```python
if h.get("pattern"):
    sig += f"·{PATTERN_LABEL_MAP.get(h['pattern'], h['pattern'])}"
    # 组合确认（2026-10-26，docs/11）：形态在信号根的前一根，卡片 dict 无
    # signal_candle_open，用等价判据 entry_lag_pct 非空（构造上只有形态早于
    # 信号根才算滞后）
    if h.get("entry_lag_pct") is not None:
        sig += "(前根)"
```

- [ ] 跑测试确认通过

```bash
cd backend && python -m pytest tests/test_scan_hit_card.py -q
```

- [ ] 提交

```bash
git add backend/app/services/scanner.py backend/tests/test_scan_hit_card.py
git commit -m "feat(11): 飞书命中卡片组合确认标 (前根)"
```

---

## T5. 前端：类型 + 图表双标

**文件**
- 改：`frontend/src/types/index.ts`（`HitHistoryItem`，:370-390）
- 改：`frontend/src/components/KlineChart.tsx`（`EffHit` :636-646、Props :659、effHits :756/:767、
  `drawHitMarks` 类型 :410/:429/:1093 与分支 :479/:605、常量区 :52、`overlayFrom` :1064-1078、`drawZones` :1093-1120）

**接口**
- 消费：T3 的四个响应字段。
- 产出：图表主标 `命中 · 信号类型·形态(前根)` + 形态根弱色副标 `形态根·形态名`。

**步骤**

- [ ] 类型（`types/index.ts`，`pattern` 字段之后）

```ts
  // 组合确认口径（2026-10-26，docs/11）：pattern 真实所在K线与确认滞后；
  // golden_combo 仅留痕，展示与否看 pattern_candle_open 是否早于 signal_candle_open
  golden_combo: boolean
  pattern_candle_open: string | null
  entry_lag_pct: number | null
  entry_lag_warn: boolean
```

- [ ] 常量（`KlineChart.tsx` 的 `HIT_MARK_COLOR` / `TOUCH_MARK_COLOR` 旁）

```ts
// 形态根副标（2026-10-26，docs/11）：组合确认时 pattern 在信号根的前一根，
// 用中性灰弱色，避免与蓝色主标、紫色触碰标抢注意力
const PATTERN_MARK_COLOR = '#8b93a7'
const PATTERN_MARK_FILL = 'rgba(139,147,167,0.10)'
```

- [ ] `EffHit` 与 Props 增字段

```ts
  goldenCombo: boolean // 组合确认（仅留痕；渲染判据用 patternCandleOpen）
  patternCandleOpen: string | null // pattern 真实所在K线开盘时间（缺省=同信号根）
```

Props 侧：`hitGoldenCombo?: boolean`、`hitPatternCandleOpen?: string | null`，并加进解构默认值。
`effHits` 两处归一补齐：外部用 `hitGoldenCombo ?? false` / `hitPatternCandleOpen ?? null`，
自动拉取用 `h.golden_combo ?? false` / `h.pattern_candle_open ?? null`。

- [ ] `kind` 扩一档：`'hit' | 'touch' | 'pattern'`（三处类型标注 :410 / :429 / :1093）。
  `drawHitMarks` 内 `m.kind === 'touch'` 的两处分支（:479 摆放、:605 绘制）复制成
  `m.kind === 'touch' || m.kind === 'pattern'`（同几何、颜色与填充已随 mark 传入，副标自然弱化）。

- [ ] `drawZones` 主标文案 + 副标

```tsx
        // 组合确认（2026-10-26，docs/11）：(前根) 与形态根副标由「两根K是否真的
        // 不同」派生，不看 goldenCombo——4h/1d 图两根常落同一根，映射后相等则只留主标
        const pSlotMs = h.patternCandleOpen
          ? Math.floor(utcMs(h.patternCandleOpen) / ivMs) * ivMs
          : null
        const onPrev = pSlotMs != null && sigMs != null && pSlotMs < Math.floor(sigMs / ivMs) * ivMs
        const hitLabel = parts.length
          ? `命中 · ${parts.join('·')}${onPrev ? '(前根)' : ''}`
          : '命中'
```

主标 push 之后、`touchTime` push 之前加：

```tsx
        if (onPrev) {
          marks.push({
            ms: pSlotMs, color: PATTERN_MARK_COLOR, fill: PATTERN_MARK_FILL,
            label: patternLabel ? `形态根·${patternLabel}` : '形态根',
            kind: 'pattern', preferBelow,
          })
        }
```

- [ ] `overlayFrom` 候选时间集并入副标时间（`flatMap` 里加 `h.patternCandleOpen`）

```tsx
      .flatMap((h) => [h.hitTime, h.touchTime, h.patternCandleOpen])
```

- [ ] 类型检查 + 构建

```bash
cd frontend && npm run build   # build = tsc && vite build，类型错即失败
```

- [ ] 提交

```bash
git add frontend/src/types/index.ts frontend/src/components/KlineChart.tsx
git commit -m "feat(11): 图表命中主标标(前根)+形态根弱色副标"
```

---

## T6. 前端：命中历史列表与详情抽屉

**文件**
- 改：`frontend/src/components/HitHistoryPanel.tsx`（形态列 :143-150、详情抽屉 :270-300）

**步骤**

- [ ] 形态列

```tsx
    {
      title: '形态', dataIndex: 'pattern', key: 'pattern', width: 130,
      render: (v: string | null, r) => {
        if (!v) return <Text type="secondary">-</Text>
        const s = patternStyle(v, colorScheme)
        // 组合确认（2026-10-26，docs/11）：形态在信号根的前一根，列宽同步放宽
        const isPrev = r.entry_lag_pct != null
        return <Tag color={s.color}>{isPrev ? `前根·${s.label}` : s.label}</Tag>
      },
    },
```

- [ ] 抽屉传参（`<KlineChart ... />` 内，`hitPattern` 之后）

```tsx
                hitGoldenCombo={detail.golden_combo}
                hitPatternCandleOpen={detail.pattern_candle_open}
```

- [ ] 抽屉时间信息区（「触碰」Tag 之后）追加形态根与滞后

```tsx
              {detail.pattern_candle_open && detail.entry_lag_pct != null && (
                <span>
                  <Tag color="#8b93a7">形态K</Tag>
                  {bj(detail.pattern_candle_open).format('YYYY-MM-DD HH:mm')}
                  <Text type={detail.entry_lag_warn ? 'warning' : 'secondary'}>
                    {` · 确认滞后 ${detail.entry_lag_pct > 0 ? '+' : ''}${detail.entry_lag_pct.toFixed(2)}%${detail.entry_lag_warn ? ' ⚠' : ''}`}
                  </Text>
                </span>
              )}
```

- [ ] 类型检查 + 构建

```bash
cd frontend && npm run build   # build = tsc && vite build，类型错即失败
```

- [ ] 提交

```bash
git add frontend/src/components/HitHistoryPanel.tsx
git commit -m "feat(11): 命中历史形态列标前根，抽屉显示形态K与确认滞后"
```

---

## T7. 文档同步 + 全量验收

**文件**
- 改：`docs/信号与形态/02-信号触发器.md`、`docs/信号与形态/11-金K形态库.md`、
  `docs/前端与查询/14-命中历史查询.md`、`docs/前端与查询/08-前端界面.md`

**步骤**

- [ ] `02-信号触发器.md` §5.4 规则3 后补「展示口径」小节：`golden_combo` / `pattern_candle_open` /
  `entry_lag_pct` 三字段、`(前根)` 文案、阈值 `COMBO_LAG_WARN_PCT=0.8`。
- [ ] `11-金K形态库.md` 补一段：组合确认时形态名锚在形态根，图表另画弱色副标。
- [ ] `14-命中历史查询.md` 响应字段表增四字段，并写明「4h/1d 图映射后同槽则只留主标」。
- [ ] `08-前端界面.md` 图表标注图例增「形态根（灰）」。
- [ ] 重新生成 wiki

```bash
node scripts/build_docs_wiki.mjs --quiet
```

- [ ] 全量验收

```bash
cd backend && python -m pytest tests/test_trigger_two_stage.py tests/test_hit_history.py tests/test_scan_hit_card.py -q
cd ../frontend && npm run build
cd .. && node scripts/build_docs_wiki.mjs --quiet
```

- [ ] 提交

```bash
git add docs docs-wiki
git commit -m "docs(11): 同步组合确认展示口径至信号/前端文档与 wiki"
```

---

## 线上核验（部署后，非代码任务）

- [ ] REZUSDT.P 那条命中重开详情：主标 `命中 · 回踩确认·锤形线(前根)`，02:00 处灰标
  `形态根·锤形线`，抽屉显示 `形态K 10-26 02:00 · 确认滞后 +1.59% ⚠`。
- [ ] 切 4h 图：两根映射同槽，只剩主标（带 `(前根)`），无重叠灰标。
- [ ] 飞书卡片：含 `(前根)` 的行不换行错位。
