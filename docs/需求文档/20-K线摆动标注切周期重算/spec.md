# 20 · K 线图切周期摆动标注（HH/LH/HL/LL）重算修复

- 日期：2026-10-29
- 状态：待评审
- 范围：前端 `KlineChart` / `ChartCore`；后端不动
- 关联：`docs/需求文档/11-回踩组合确认展示口径`（摆动标注渲染口径）

## 1. 问题

K 线图从 1h 切到 4h（以及反向切换）后，高点/低点摆动结构标注（HH/LH/HL/LL）
没有随新周期重新计算：切档窗口内旧周期的标注继续显示在旧 K 线上，新周期响应到达后
若走缓存优先 / 刷新被丢弃的路径，标注缺失或仍停留在上一周期口径。

## 2. 现状与根因（代码级）

摆动标注来自后端随 K 线一起返回的 `swings`（`backend/app/api/scan.py:2429`，
`swing_annotations(klines, ...)` 按本次返回的 klines 计算，后端口径正确），
前端在 `ChartCore` 的 data effect 里转成 lightweight-charts markers
（`frontend/src/components/ChartCore.tsx:637-663`）。问题出在「标注与 K 线数据没有
绑定同一个数据源」：

1. **切档瞬间没有作废旧周期标注**
   `KlineChart` 的周期按钮直接 `onIntervalChange={(v) => setReqInterval(v)}`。
   切档时按设计保留旧图平滑替换，但旧图上的 HH/LH/HL/LL 也是旧周期的；
   新响应到达前它们继续显示，视觉上就是「切到 4h 但高低点没重算」。
2. **缓存优先分支不带 `swings`**
   `frontend/src/components/KlineChart.tsx:873`：
   `applyData({ symbol: sym, interval: iv, klines: cached })` —— 只有 K 线没有标注，
   依赖稍后的 fresh 响应补齐标注；一旦 fresh 被 `tailRegressed` 丢弃（893 行）或
   返回空（913 行），该周期就一直没有正确的摆动标注。
3. **空数据时 marker 不清理**
   `ChartCore` data effect 在 `data.klines.length === 0` 时直接 return（601 行），
   摆动 markers 不会被清空，上一周期的标注可残留在空数据/新周期首屏状态上。

## 3. 修复方案（前端，最小改动）

### 3.1 `frontend/src/components/KlineChart.tsx`

新增切档入口 `handleIntervalChange`，切档前先把当前数据上的 `swings` 清空
（K 线与视口保留，符合「切周期不清旧图」的既有交互）：

```tsx
// 周期切换：先作废旧周期的摆动标注（HH/LH/HL/LL 必须按周期重算），
// 再切档拉新数据；旧 K 线与视口保留，不整块 loading
const handleIntervalChange = useCallback(
  (v: string) => {
    const cur = dataRef.current
    if (cur && cur.interval !== v) {
      applyData({ ...cur, swings: undefined }, 0, true)
    }
    setReqInterval(v)
  },
  [applyData],
)
```

`ChartCore` 的 `onIntervalChange` 由 `(v) => setReqInterval(v)` 改为
`handleIntervalChange`。

缓存优先分支（873 行）显式声明无标注，避免后续合并误带旧值：

```tsx
applyData({ symbol: sym, interval: iv, klines: cached, swings: undefined })
```

### 3.2 `frontend/src/components/ChartCore.tsx`

data effect 的空数据分支改为先清摆动 markers 再返回：

```tsx
if (!series || !chart || !data) return
// 空数据（新周期首屏尚无 K 线）：只清摆动标注，保留旧图，
// 避免上一周期的 HH/LH/HL/LL 残留在新周期上
if (data.klines.length === 0) {
  markersApiRef.current?.setMarkers(extraMarkers ?? [])
  return
}
```

### 3.3 后端

不改。`GET /api/scans/klines/{symbol}?interval=4h` 已按 4h klines 计算 `swings`
（`scan.py:2429`），KlineCache 也已按 interval 分键（`exchange_pool._get_cached_klines`）。

## 4. 非目标

- 不改摆动算法与参数（`find_swing_points` / `merge_swings` / `recent_swings` / `swing_order`）
- 不改 K 线拉取、缓存与合并逻辑（`klineDb` / `mergeTailFresh` / `tailRegressed`）
- 不新增依赖，不改后端接口契约

## 5. 验证

1. `cd frontend && npx tsc --noEmit` 通过。
2. Network 对比同一币种：`/api/scans/klines/XXX?interval=1h` 与 `?interval=4h`
   返回的 `swings` 时间/价格不同（确认后端重算）。
3. 关注页（`cacheHistory`）：1h 记下 HH/LH 的时间与价格 → 切 4h：
   - 切档瞬间旧标注消失，旧 K 线与视口保留；
   - 4h 响应到达后显示 4h 自己的摆动标注，位置/价格与 1h 不同；
   - 切回 1h 同样重算。
4. 漏斗抽屉（`<KlineChart symbol={klineSymbol} limit={500} />`）：同上。
5. 降级场景（fresh 失败/被 `tailRegressed` 丢弃）：新周期上不残留旧周期标注
   （允许暂时无标注），错误提示行为不变。

## 6. 验收标准

- 1h ↔ 4h ↔ 1d 任意切换后，图上 HH/LH/HL/LL 始终与当前周期 K 线同源：
  时间点落在当前周期的 K 线上、价格与当前周期摆动点一致。
- 切档不再出现「新周期 K 线 + 旧周期高低点」的混搭画面。
- 既有交互不回退：切周期不清旧图、不整块 loading、视口不跳。

## 7. 风险与影响面

- 仅影响摆动标注的渲染时序；K 线数据、指标、命中覆盖层、AI 仓位线均不涉及。
- `handleIntervalChange` 会触发一次同 K 线的 `setData`（keepViewport=true），
  开销可忽略；若后续发现多余重绘，可改为独立的 `swings` 覆盖状态。
