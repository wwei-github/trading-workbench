# 任务拆分 · 20 K 线摆动标注切周期重算

## [x] T1 切档入口先作废旧周期摆动标注
- 文件：`frontend/src/components/KlineChart.tsx`
- 内容：
  - 新增 `handleIntervalChange(v)`：`dataRef.current.interval !== v` 时先
    `applyData({ ...cur, swings: undefined }, 0, true)`，再 `setReqInterval(v)`
  - `ChartCore` 的 `onIntervalChange` 改为传入 `handleIntervalChange`
- 验收：`npx tsc --noEmit` 通过；切档瞬间旧 HH/LH/HL/LL 消失，旧 K 线与视口保留

## [x] T2 缓存优先分支显式无 swings
- 文件：`frontend/src/components/KlineChart.tsx:873`
- 内容：`applyData({ symbol: sym, interval: iv, klines: cached, swings: undefined })`
- 验收：关注页 4h 首屏不出现上一周期标注

## [x] T3 空数据清摆动 marker
- 文件：`frontend/src/components/ChartCore.tsx` data effect
- 内容：`data.klines.length === 0` 时 `markersApiRef.current?.setMarkers(extraMarkers ?? [])` 后 return
- 验收：空数据状态不残留旧周期标注

## [x] T4 验证与回归（tsc / build / 后端 1h↔4h swings 对比 / 页面与 API 可达）
- `cd frontend && npx tsc --noEmit`
- Network 对比同币种 `interval=1h` / `interval=4h` 的 `swings` 不同
- 关注页 + 漏斗抽屉手动切换 1h ↔ 4h ↔ 1d：
  - 标注与当前周期同源
  - 切周期不清旧图、不整块 loading、视口不跳
- 降级场景（fresh 失败）不出现跨周期混搭标注

## 依赖
- T1 → T4；T2、T3 独立，可与 T1 并行
