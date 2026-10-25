/**
 * K 线图（全站唯一组件，2026-10-09 封装层合一）
 * 图表能力（指标/图例/工具栏/仓位价位线/SVG 覆盖层）全部在 ChartCore，
 * 本组件只负责数据拉取与两种数据源模式的差异注入：
 * - 扫描模式（symbol）：实时K线（扫描表/待确认池/搜索/关注列表共用），
 *   AI 仓位标注；拉取根数 limit 可配
 * - 复盘模式（tradeId）：/trades/{id}/klines 一次返回——开单前 200 根（EMA144
 *   热身需 ~143 根）+ 持仓期间直到止损/平仓；事件箭头（extraMarkers）+ 复盘区域覆盖层
 *   （止损区/止盈区/现止损琥珀线）；界面件（水印/图例/工具栏/摆动标注/
 *   可视范围）与扫描模式完全一致，交易信息由图上价签与区域承载
 * 交互统一（同 2026-10-08 复盘图口径）：切周期不清旧数据不整块 loading，
 * 旧图保留、新数据到达后 ChartCore 原地替换；仅首拉（无数据）整块反馈。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Empty, Spin } from 'antd'
import type { SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { scanApi } from '../api/scan'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import {
  INTERVAL_SEC,
  getMeta,
  isTailFresh,
  loadLatest,
  loadOlder,
  mergeOlderBars,
  mergeTailFresh,
  saveOlder,
  saveTail,
  tailHasGap,
  type KlineInterval,
} from '../utils/klineDb'
import { fmtPrice, pctDelta, rrRatio } from './klineShared'
import { dayjs } from '../utils/dayjs'
import ChartCore, { type OverlayCtx } from './ChartCore'
import { SIGNAL_TYPE_MAP } from '../constants/labels'
import type { AIAnalysis, HitHistoryItem, HitHistoryTrade, Kline, KlineData, TradeKlineData, TradeKlineEvent } from '../types'

const CHART_HEIGHT = 560 // 容器无高度时的兜底值（各挂载点容器均有自身高度）

// 后端 UTC naive → 毫秒（统一按 UTC 解析）
const utcMs = (v: string) => dayjs.utc(v).valueOf()

// 十六进制色 → 带透明度 rgba（胶囊底色「淡」用：半透明主题色 + 白字）
const hexRgba = (hex: string, alpha: number) => {
  const h = hex.replace('#', '')
  return `rgba(${parseInt(h.slice(0, 2), 16)}, ${parseInt(h.slice(2, 4), 16)}, ${parseInt(h.slice(4, 6), 16)}, ${alpha})`
}

// 命中历史 marker 配色（2026-10-15）：命中=蓝、触碰=紫（两者区分）
const HIT_MARK_COLOR = '#1677ff'
const TOUCH_MARK_COLOR = '#722ed1'
// 标注区背景色：同色系高透明度填充（把命中/触碰K线的区域醒目衬出来）
// 2026-10-16：命中侧背景再淡化（0.35→0.20），避免底色过深压住K线
const HIT_MARK_FILL = 'rgba(22, 119, 255, 0.20)'
const TOUCH_MARK_FILL = 'rgba(114, 46, 209, 0.35)'

// ── 复盘模式部件（仅 tradeId 模式使用）────────────────────────────────

// 现止损线颜色：琥珀色，与初始止损（跌色）区分
const CUR_SL_COLOR = '#faad14'
// 价签最小间距与错位步长（px）：现止损=保本价时与开仓线同高，需上下错开
const TAG_GAP = 13
const TAG_SHIFT = 14

// 事件 → 图上箭头标记的位置与文案（多空方向决定箭头在K线上方还是下方）
function eventMarker(
  ev: TradeKlineEvent,
  isLong: boolean,
  scheme: ColorScheme,
): { position: 'aboveBar' | 'belowBar'; shape: 'arrowUp' | 'arrowDown'; color: string; text: string } | null {
  const { up, down } = SCHEME_UP_DOWN[scheme]
  const fill = ev.detail?.fill_price
  const priceSuffix = typeof fill === 'number' ? ` ${fmtPrice(fill)}` : ''
  switch (ev.event_type) {
    case 'OPEN':
      return isLong
        ? { position: 'belowBar', shape: 'arrowUp', color: '#1677ff', text: '开仓' }
        : { position: 'aboveBar', shape: 'arrowDown', color: '#1677ff', text: '开仓' }
    case 'TP1_FILL':
      return isLong
        ? { position: 'aboveBar', shape: 'arrowDown', color: up, text: `TP1${priceSuffix}` }
        : { position: 'belowBar', shape: 'arrowUp', color: up, text: `TP1${priceSuffix}` }
    case 'TP2_FILL':
      return isLong
        ? { position: 'aboveBar', shape: 'arrowDown', color: up, text: `TP2${priceSuffix}` }
        : { position: 'belowBar', shape: 'arrowUp', color: up, text: `TP2${priceSuffix}` }
    case 'SL_FILL':
      return isLong
        ? { position: 'belowBar', shape: 'arrowUp', color: down, text: `止损${priceSuffix}` }
        : { position: 'aboveBar', shape: 'arrowDown', color: down, text: `止损${priceSuffix}` }
    default:
      return null
  }
}

// 复盘特有覆盖层：区域色块 + 风控线（ChartCore 每帧在空白 SVG 上调用）：
// 止损区（入场→SL，红半透明）、止盈1/2区（绿半透明）从开仓K线延伸到图右缘；
// 入场/止损/止盈水平线 + 右侧价签；现止损琥珀线（跟踪移动后）
function drawReplayZones(data: TradeKlineData, colorScheme: ColorScheme, ctx: OverlayCtx) {
  const { chart, series, svg, container } = ctx

  const isL = data.direction !== 'short'
  const { up, down } = SCHEME_UP_DOWN[colorScheme]
  const colorEntry = isL ? up : down
  const colorSL = down
  const colorTP = up

  const NS = 'http://www.w3.org/2000/svg'
  const el = (tag: string) => document.createElementNS(NS, tag)

  const fullWidth = container.clientWidth || 520
  const height = container.clientHeight || CHART_HEIGHT
  const priceScaleWidth = chart.priceScale('right').width()
  const rightEdge = Math.max(20, fullWidth - priceScaleWidth)
  svg.setAttribute('width', String(fullWidth))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

  if (data.entry_price == null) return
  const yEntry = series.priceToCoordinate(data.entry_price)
  if (yEntry == null) return

  // 区域左边界 = 开仓K线的 x 坐标（找不到则从图左缘开始）
  const openMs = data.opened_at ? utcMs(data.opened_at) : null
  let x0 = 0
  if (openMs != null) {
    // K线时间升序：开仓K线 = time <= openMs 的最后一根
    let openTime: number | null = null
    for (const k of data.klines) {
      if (k.time <= openMs) openTime = k.time
      else break
    }
    if (openTime != null) {
      const cx = chart.timeScale().timeToCoordinate(Math.floor(openTime / 1000) as UTCTimestamp)
      if (cx != null) x0 = cx
    }
  }
  x0 = Math.max(0, Math.min(x0, rightEdge - 20))

  // 区域色块（从开仓K线延伸到价格轴）
  const drawZone = (p1: number | null, p2: number | null, fill: string) => {
    if (p1 == null || p2 == null) return
    const y1 = series.priceToCoordinate(p1)
    const y2 = series.priceToCoordinate(p2)
    if (y1 == null || y2 == null) return
    const r = el('rect')
    r.setAttribute('x', String(x0))
    r.setAttribute('y', String(Math.min(y1, y2)))
    r.setAttribute('width', String(rightEdge - x0))
    r.setAttribute('height', String(Math.abs(y2 - y1)))
    r.setAttribute('fill', fill)
    svg.appendChild(r)
  }
  drawZone(data.entry_price, data.stop_loss, 'rgba(239, 83, 80, 0.15)')
  drawZone(data.entry_price, data.tp1, 'rgba(38, 166, 154, 0.15)')
  // tp2 为 0/null（未设止盈二）时不绘制 TP2 区域与线
  const hasTP2 = data.tp2 != null && data.tp2 > 0
  if (hasTP2) drawZone(data.tp1, data.tp2, 'rgba(38, 166, 154, 0.08)')

  // 水平线 + 右侧价签（tagDy：价签垂直偏移，防同价位标签重叠；返回价签中心 y 供后续防撞）
  const tagYs: number[] = []
  const drawLine = (price: number | null, color: string, label: string, dashed: boolean, tagDy = 0) => {
    if (price == null) return null
    const y = series.priceToCoordinate(price)
    if (y == null) return null
    const line = el('line')
    line.setAttribute('x1', String(x0))
    line.setAttribute('y1', String(y))
    line.setAttribute('x2', String(rightEdge))
    line.setAttribute('y2', String(y))
    line.setAttribute('stroke', color)
    line.setAttribute('stroke-width', '1.5')
    line.setAttribute('shape-rendering', 'crispEdges')
    if (dashed) line.setAttribute('stroke-dasharray', '5,3')
    svg.appendChild(line)

    const text = `${label} ${fmtPrice(price)}`
    const font = 'bold 10px monospace'
    const ctx2 = document.createElement('canvas').getContext('2d')!
    ctx2.font = font
    const tw = ctx2.measureText(text).width
    const boxW = tw + 8
    const boxH = 13
    const boxX = rightEdge - boxW
    const tagY = y + tagDy

    const rect = el('rect')
    rect.setAttribute('x', String(boxX))
    rect.setAttribute('y', String(tagY - boxH / 2))
    rect.setAttribute('width', String(boxW))
    rect.setAttribute('height', String(boxH))
    rect.setAttribute('fill', color)
    svg.appendChild(rect)

    const t = el('text')
    t.setAttribute('x', String(boxX + 4))
    t.setAttribute('y', String(tagY + 3))
    t.setAttribute('fill', '#fff')
    t.setAttribute('font-size', '10')
    t.setAttribute('font-family', 'monospace')
    t.setAttribute('font-weight', 'bold')
    t.textContent = text
    svg.appendChild(t)
    tagYs.push(tagY)
    return tagY
  }
  drawLine(data.entry_price, colorEntry, isL ? '开多' : '开空', false)
  const ep = data.entry_price
  drawLine(data.stop_loss, colorSL, data.stop_loss != null ? `止损 ${pctDelta(ep, data.stop_loss)}` : '止损', false)
  drawLine(data.tp1, colorTP, data.tp1 != null ? `止盈1 ${pctDelta(ep, data.tp1)}` : '止盈1', false)
  if (hasTP2) drawLine(data.tp2, colorTP, `止盈2 ${pctDelta(ep, data.tp2!)}`, false)

  // 现止损（跟踪移动后的最新位置）：与初始止损不同才加画（琥珀色区分）；
  // 价签与已有标签同高时上下错位（保本止损=开仓价的场景）
  const curSl = data.current_stop_loss
  if (curSl != null && data.stop_loss != null && Math.abs(curSl - data.stop_loss) > 1e-12) {
    const cy = series.priceToCoordinate(curSl)
    if (cy != null) {
      const clash = tagYs.find((uy) => Math.abs(uy - cy) < TAG_GAP)
      const dy = clash == null ? 0 : clash > cy ? -TAG_SHIFT : TAG_SHIFT
      drawLine(curSl, CUR_SL_COLOR, `现止损 ${pctDelta(ep, curSl)}`, false, dy)
    }
  }

  // RR 徽标（2026-10-17 改版）：无底色纯文字，右对齐到区域右缘（与右侧价签同一条
  // 右边界，留 5px 内缩）；垂直仍在入场↔止盈1（缺则入场↔止损）中线
  const rr = rrRatio(ep, data.stop_loss, data.tp1)
  if (rr != null) {
    const anchor = data.tp1 ?? data.stop_loss
    const yA = series.priceToCoordinate(anchor!)
    if (yA != null) {
      const text = `RR 1:${rr.toFixed(1)}`
      const ctx2 = document.createElement('canvas').getContext('2d')!
      ctx2.font = 'bold 10px monospace'
      const boxW = ctx2.measureText(text).width + 10
      const cy = (yEntry + yA) / 2
      const t = el('text')
      t.setAttribute('x', String(rightEdge - boxW + 5))
      t.setAttribute('y', String(cy + 3.5))
      t.setAttribute('fill', '#d1d4dc')
      t.setAttribute('font-size', '10')
      t.setAttribute('font-family', 'monospace')
      t.setAttribute('font-weight', 'bold')
      t.textContent = text
      svg.appendChild(t)
    }
  }
}

// 复盘模式不再有专属界面件（信息卡/脚注/水印后缀已移除，界面与扫描图完全一致）：
// 交易信息由图上内容承载——开多/开空价签、止损/止盈线与区域、事件箭头、摆动结构标注

// 命中历史专属覆盖层（2026-10-15，docs/22 §5.3）：时间裁剪的开单区间——
// 与复盘全区间价格带不同，横向左边界=开仓K线、右边界=平仓K线；
// 未平仓（OPENED/TP1_HIT/TP2_HIT）或只有部分目标时，右边界延续到图右缘（开放右端）。
// 纵向价格带沿用复盘配色（止损红 / 止盈1 绿 / 止盈2 浅绿）+ 水平线与百分比价签
function drawHitOverlay(
  klines: Kline[],
  trade: HitHistoryTrade,
  colorScheme: ColorScheme,
  ctx: OverlayCtx,
) {
  const { chart, series, svg, container } = ctx

  const isL = trade.direction !== 'short'
  const { up, down } = SCHEME_UP_DOWN[colorScheme]
  const colorEntry = isL ? up : down
  const colorSL = down
  const colorTP = up

  const NS = 'http://www.w3.org/2000/svg'
  const el = (tag: string) => document.createElementNS(NS, tag)

  const fullWidth = container.clientWidth || 520
  const height = container.clientHeight || CHART_HEIGHT
  const priceScaleWidth = chart.priceScale('right').width()
  const rightEdge = Math.max(20, fullWidth - priceScaleWidth)
  svg.setAttribute('width', String(fullWidth))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

  if (trade.entry_price == null) return
  const yEntry = series.priceToCoordinate(trade.entry_price)
  if (yEntry == null) return

  // 时间 → x 坐标：取 time <= ms 的最后一根K线（找不到回落 fallback）
  const xAt = (ms: number | null, fallback: number): number => {
    if (ms == null) return fallback
    let t: number | null = null
    for (const k of klines) {
      if (k.time <= ms) t = k.time
      else break
    }
    if (t == null) return fallback
    const cx = chart.timeScale().timeToCoordinate(Math.floor(t / 1000) as UTCTimestamp)
    return cx == null ? fallback : cx
  }

  const openMs = trade.opened_at ? utcMs(trade.opened_at) : null
  const x0 = Math.max(0, Math.min(xAt(openMs, 0), rightEdge - 10))
  // 右边界：已平仓（CLOSED/FAILED 且有平仓时刻）→ 平仓K线；否则延续到图右缘
  const isClosed = (trade.status === 'CLOSED' || trade.status === 'FAILED') && !!trade.closed_at
  const x1 = isClosed
    ? Math.max(x0 + 2, Math.min(xAt(trade.closed_at ? utcMs(trade.closed_at) : null, rightEdge), rightEdge))
    : rightEdge

  // 区域色块（x0→x1 限定在持仓区间内）
  const drawZone = (p1: number | null, p2: number | null, fill: string) => {
    if (p1 == null || p2 == null) return
    const y1 = series.priceToCoordinate(p1)
    const y2 = series.priceToCoordinate(p2)
    if (y1 == null || y2 == null) return
    const r = el('rect')
    r.setAttribute('x', String(x0))
    r.setAttribute('y', String(Math.min(y1, y2)))
    r.setAttribute('width', String(x1 - x0))
    r.setAttribute('height', String(Math.abs(y2 - y1)))
    r.setAttribute('fill', fill)
    svg.appendChild(r)
  }
  drawZone(trade.entry_price, trade.stop_loss, 'rgba(239, 83, 80, 0.15)')
  drawZone(trade.entry_price, trade.take_profit_1, 'rgba(38, 166, 154, 0.15)')
  const hasTP2 = trade.take_profit_2 != null && trade.take_profit_2 > 0
  if (hasTP2) drawZone(trade.take_profit_1, trade.take_profit_2, 'rgba(38, 166, 154, 0.08)')

  // 水平线 + 右侧价签（与复盘同款；横线限定在持仓区间 x0→x1 内）。
  // tagDy：价签垂直偏移，防同价位标签重叠（保本止损=开仓价时与开仓线同高）
  const tagYs: number[] = []
  const drawLine = (price: number | null, color: string, label: string, dashed = false, tagDy = 0) => {
    if (price == null) return
    const y = series.priceToCoordinate(price)
    if (y == null) return
    const line = el('line')
    line.setAttribute('x1', String(x0))
    line.setAttribute('y1', String(y))
    line.setAttribute('x2', String(x1))
    line.setAttribute('y2', String(y))
    line.setAttribute('stroke', color)
    line.setAttribute('stroke-width', '1.5')
    line.setAttribute('shape-rendering', 'crispEdges')
    if (dashed) line.setAttribute('stroke-dasharray', '5,3')
    svg.appendChild(line)

    const text = `${label} ${fmtPrice(price)}`
    const c2d = document.createElement('canvas').getContext('2d')!
    c2d.font = 'bold 10px monospace'
    const boxW = c2d.measureText(text).width + 8
    const boxH = 13
    const boxX = rightEdge - boxW
    const tagY = y + tagDy
    const rect = el('rect')
    rect.setAttribute('x', String(boxX))
    rect.setAttribute('y', String(tagY - boxH / 2))
    rect.setAttribute('width', String(boxW))
    rect.setAttribute('height', String(boxH))
    rect.setAttribute('fill', color)
    svg.appendChild(rect)
    const t = el('text')
    t.setAttribute('x', String(boxX + 4))
    t.setAttribute('y', String(tagY + 3))
    t.setAttribute('fill', '#fff')
    t.setAttribute('font-size', '10')
    t.setAttribute('font-family', 'monospace')
    t.setAttribute('font-weight', 'bold')
    t.textContent = text
    svg.appendChild(t)
    tagYs.push(tagY)
  }
  const ep = trade.entry_price
  drawLine(ep, colorEntry, isL ? '开多' : '开空')
  drawLine(trade.stop_loss, colorSL, trade.stop_loss != null ? `初始止损 ${pctDelta(ep, trade.stop_loss)}` : '初始止损')
  drawLine(trade.take_profit_1, colorTP, trade.take_profit_1 != null ? `止盈1 ${pctDelta(ep, trade.take_profit_1)}` : '止盈1')
  if (hasTP2) drawLine(trade.take_profit_2, colorTP, `止盈2 ${pctDelta(ep, trade.take_profit_2!)}`)

  // 移动止损（SL_MOVE 后的跟踪止损现值）：与初始止损不同才加画（琥珀色虚线区分，
  // 与复盘图同款）；同高时价签上下错位避让
  const curSl = trade.current_stop_loss
  if (curSl != null && trade.stop_loss != null && Math.abs(curSl - trade.stop_loss) > 1e-12) {
    const cy = series.priceToCoordinate(curSl)
    if (cy != null) {
      const clash = tagYs.find((uy) => Math.abs(uy - cy) < TAG_GAP)
      const dy = clash == null ? 0 : clash > cy ? -TAG_SHIFT : TAG_SHIFT
      drawLine(curSl, CUR_SL_COLOR, `移动止损 ${pctDelta(ep, curSl)}`, true, dy)
    }
  }
}

// ── 命中历史图形化标注（2026-10-15，docs/22）：图形 + 文字组合（非纯文字）──
// 命中K线 = 蓝色「瞄准框」（四角括号）+ 准星图标胶囊（靶心脉标 + 「命中」）
// 触碰K线 = 紫色「感应框」（虚线圆角框）+ 脉冲图标胶囊（同心圆环 + 「触碰」）
// 徽标位置跟随命中方向（多→K线下方 / 空→K线上方），越界自动翻面；
// 颜色 + 形状 + 文字三重区分
const MARK_PAD = 3 // 标注框相对K线高/低点的外扩（px）
const MARK_BADGE = 18 // 徽标（图标区）边长（px）
// 胶囊底色不透明度：半透明主题色 + 白色文字/图标，底色更淡更柔和（2026-10-16）
const PILL_ALPHA = 0.7
// 徽标与标注框的间距（px，2026-10-16 调大：徽标远离K线，经箭头指向K线，
// 避免遮挡K线本体；下跌命中→上方 / 上涨命中→下方，由 preferBelow 决定）
const MARK_GAP = 72
// 各周期一根K线的毫秒跨度（命中锚定用：信号时刻映射到当前图表周期开口）
const INTERVAL_MS: Record<string, number> = { '1h': 3600_000, '4h': 4 * 3600_000, '1d': 86400_000 }
// 扫描周期跨度（与后端 cfg.kline_interval 对齐，当前固定 1h）
const SCAN_INTERVAL_MS = 3600_000
const MARK_ARROW = 6 // 导引线箭头半宽（px）

function drawHitMarks(
  klines: Kline[],
  marks: Array<{ ms: number; color: string; fill: string; label: string; kind: 'hit' | 'touch'; preferBelow: boolean }>,
  ctx: OverlayCtx,
) {
  const { chart, series, svg, container } = ctx
  const NS = 'http://www.w3.org/2000/svg'
  const el = (tag: string) => document.createElementNS(NS, tag)

  const fullWidth = container.clientWidth || 520
  const height = container.clientHeight || CHART_HEIGHT
  svg.setAttribute('width', String(fullWidth))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

  // 已放置徽标的占位矩形：连续多根同侧标注时，后放者逐档外移，防止徽标相互遮挡
  const placed: Array<{ x: number; y: number; w: number; h: number }> = []
  // 每枚标注的几何（第一趟算好后暂存，供第二/三趟分层绘制）
  type MarkGeom = {
    color: string
    label: string
    kind: 'hit' | 'touch'
    cx: number
    yTop: number
    yBot: number
    sideBelow: boolean
    badgeCy: number
    badgeX: number
    pillW: number
    b: number
  }
  const geoms: MarkGeom[] = []

  for (const m of marks) {
    // 时刻所在K线（time <= ms 的最后一根；早于首根则无标注）
    let idx = -1
    for (let i = 0; i < klines.length; i++) {
      if (klines[i].time <= m.ms) idx = i
      else break
    }
    // 时间口径（2026-10-16）：命中 ms 已在构建 marks 时锚定到「包含信号时刻
    // 的周期K线开口」；touchTime=触碰K线开盘时间（stage1_candle_open）精确
    // 指向目标K线。两者到此均直接定位，不再回退
    if (idx < 0) continue
    const k = klines[idx]
    const cx = chart.timeScale().timeToCoordinate(Math.floor(k.time / 1000) as UTCTimestamp)
    const yH = series.priceToCoordinate(k.high)
    const yL = series.priceToCoordinate(k.low)
    if (cx == null || yH == null || yL == null) continue

    // 标注框：覆盖该根K线并外扩；半宽取相邻K线间距的一半（回退 6px）
    const yTop = Math.min(yH, yL) - MARK_PAD
    const yBot = Math.max(yH, yL) + MARK_PAD
    let half = 6
    if (idx + 1 < klines.length) {
      const nx = chart.timeScale().timeToCoordinate(Math.floor(klines[idx + 1].time / 1000) as UTCTimestamp)
      if (nx != null) half = Math.max(4, Math.min(12, Math.abs(nx - cx) / 2))
    }
    const x0 = cx - half
    const x1 = cx + half

    // 背景色块：同色系低透明度衬底（先画，位于描边框之下）
    const fillRect = el('rect')
    fillRect.setAttribute('x', String(x0))
    fillRect.setAttribute('y', String(yTop))
    fillRect.setAttribute('width', String(x1 - x0))
    fillRect.setAttribute('height', String(yBot - yTop))
    fillRect.setAttribute('rx', '2')
    fillRect.setAttribute('fill', m.fill)
    svg.appendChild(fillRect)

    if (m.kind === 'touch') {
      // 感应框：虚线圆角框（圈住触碰K线）
      const r = el('rect')
      r.setAttribute('x', String(x0))
      r.setAttribute('y', String(yTop))
      r.setAttribute('width', String(x1 - x0))
      r.setAttribute('height', String(yBot - yTop))
      r.setAttribute('rx', '4')
      r.setAttribute('fill', 'none')
      r.setAttribute('stroke', m.color)
      r.setAttribute('stroke-width', '1.5')
      r.setAttribute('stroke-dasharray', '4,3')
      svg.appendChild(r)
    }
    // 命中K线不再描边框（2026-10-16）：去掉上下蓝色边框，仅保留背景色块

    // 徽标（图标 + 文字胶囊）：跟随方向（多→下方 / 空→上方）
    // 连续多根K线同侧标注时，徽标逐档外移，避免与已在场的徽标相互遮挡（2026-10-16）
    const b = MARK_BADGE

    // 胶囊宽度 = 图标区 + 间距 + 文字宽 + 内边距
    const c2d = document.createElement('canvas').getContext('2d')!
    c2d.font = 'bold 11px monospace'
    const pillW = Math.round(b + 3 + c2d.measureText(m.label).width + 6)
    const badgeX = Math.max(2, Math.min(cx - pillW / 2, fullWidth - pillW - 2))

    // 候选位置：优先 m.preferBelow 侧，逐档外移（步长=徽标高+间距）；该侧排不下再翻面
    const step = b + 8
    const cands: Array<{ sideBelow: boolean; gap: number }> = []
    for (let k = 0; k < 6; k++) cands.push({ sideBelow: m.preferBelow, gap: MARK_GAP + k * step })
    for (let k = 0; k < 6; k++) cands.push({ sideBelow: !m.preferBelow, gap: MARK_GAP + k * step })

    let sideBelow = m.preferBelow
    let badgeCy = sideBelow ? yBot + MARK_GAP + b / 2 : yTop - MARK_GAP - b / 2
    let placedOk = false
    for (const c of cands) {
      const cy = c.sideBelow ? yBot + c.gap + b / 2 : yTop - c.gap - b / 2
      const top = cy - b / 2
      if (top < 2 || top + b > height - 2) continue // 越出画布
      const rect = { x: badgeX, y: top, w: pillW, h: b }
      const clash = placed.some(
        (p) => !(rect.x + rect.w <= p.x || p.x + p.w <= rect.x || rect.y + rect.h <= p.y || p.y + p.h <= rect.y),
      )
      if (clash) continue
      sideBelow = c.sideBelow
      badgeCy = cy
      placed.push(rect)
      placedOk = true
      break
    }
    if (!placedOk) {
      // 兜底：所有候选均冲突/越界时，按原逻辑贴边放置
      if (sideBelow && yBot + MARK_GAP + b > height - 2) sideBelow = false
      if (!sideBelow && yTop - MARK_GAP - b < 2) sideBelow = true
      badgeCy = sideBelow ? yBot + MARK_GAP + b / 2 : yTop - MARK_GAP - b / 2
      placed.push({ x: badgeX, y: badgeCy - b / 2, w: pillW, h: b })
    }
    geoms.push({ color: m.color, label: m.label, kind: m.kind, cx, yTop, yBot, sideBelow, badgeCy, badgeX, pillW, b })
  }

  // ── 第二趟：导引线 + 箭头（2026-10-16 分层绘制）────────────────────
  // 先于所有徽标胶囊绘制：任何一条导引线/箭头都处于徽标文案之下，
  // 连续多根标注时不会再出现「下方标注的箭头压在上方标注文字之上」的情况
  for (const gm of geoms) {
    const { cx, yTop, yBot, sideBelow, badgeCy, b, color } = gm
    // sideBelow=true（徽标在K线下方）：线从徽标顶向上延伸到 K线框底，箭头尖端在 yBot 朝上
    // sideBelow=false（徽标在K线上方）：线从徽标底向下延伸到 K线框顶，箭头尖端在 yTop 朝下
    const tipY = sideBelow ? yBot : yTop // 箭头尖端 y（贴着K线框边）
    const lineEndY = sideBelow ? tipY + MARK_ARROW * 1.6 : tipY - MARK_ARROW * 1.6
    const g = el('line')
    g.setAttribute('x1', String(cx))
    g.setAttribute('y1', String(sideBelow ? badgeCy - b / 2 : badgeCy + b / 2))
    g.setAttribute('x2', String(cx))
    g.setAttribute('y2', String(lineEndY))
    g.setAttribute('stroke', color)
    g.setAttribute('stroke-width', '1.5')
    svg.appendChild(g)
    // 实心三角箭头（尖端指向K线框）
    const arrow = el('polygon')
    const aHalf = MARK_ARROW
    const aH = MARK_ARROW * 1.6
    arrow.setAttribute(
      'points',
      sideBelow
        ? `${cx},${tipY} ${cx - aHalf},${tipY + aH} ${cx + aHalf},${tipY + aH}`
        : `${cx},${tipY} ${cx - aHalf},${tipY - aH} ${cx + aHalf},${tipY - aH}`,
    )
    arrow.setAttribute('fill', color)
    svg.appendChild(arrow)
  }

  // ── 第三趟：徽标胶囊（底 + 文字 + 图形图标），最后绘制、压在最上层 ──
  for (const gm of geoms) {
    const { color, label, kind, badgeX, badgeCy, pillW, b } = gm
    const bcx = badgeX + b / 2 // 图标中心

    // 胶囊底（半透明主题色，圆角=半高；白色文字/图标仍清晰）
    const bg = el('rect')
    bg.setAttribute('x', String(badgeX))
    bg.setAttribute('y', String(badgeCy - b / 2))
    bg.setAttribute('width', String(pillW))
    bg.setAttribute('height', String(b))
    bg.setAttribute('rx', String(b / 2))
    bg.setAttribute('fill', hexRgba(color, PILL_ALPHA))
    svg.appendChild(bg)

    // 胶囊内白色文字（命中 / 触碰）
    const txt = el('text')
    txt.setAttribute('x', String(badgeX + b + 3))
    txt.setAttribute('y', String(badgeCy + 4))
    txt.setAttribute('fill', '#fff')
    txt.setAttribute('font-size', '11')
    txt.setAttribute('font-family', 'monospace')
    txt.setAttribute('font-weight', 'bold')
    txt.textContent = label
    svg.appendChild(txt)

    // 胶囊内白色图标（图形化，与文字并存）
    const icon = (tag: 'circle' | 'line') => {
      const e = el(tag)
      e.setAttribute('stroke', '#fff')
      e.setAttribute('fill', 'none')
      e.setAttribute('stroke-width', '1.5')
      e.setAttribute('stroke-linecap', 'round')
      return e
    }
    if (kind === 'touch') {
      // 脉冲：同心圆环
      const c1 = icon('circle')
      c1.setAttribute('cx', String(bcx)); c1.setAttribute('cy', String(badgeCy)); c1.setAttribute('r', '4.6')
      svg.appendChild(c1)
      const c2 = icon('circle')
      c2.setAttribute('cx', String(bcx)); c2.setAttribute('cy', String(badgeCy)); c2.setAttribute('r', '1.7')
      c2.setAttribute('fill', '#fff')
      svg.appendChild(c2)
    } else {
      // 准星：靶心环 + 四向刻度
      const ring = icon('circle')
      ring.setAttribute('cx', String(bcx)); ring.setAttribute('cy', String(badgeCy)); ring.setAttribute('r', '3.2')
      svg.appendChild(ring)
      for (const [dx, dy] of [[0, -1], [0, 1], [-1, 0], [1, 0]]) {
        const tk = icon('line')
        tk.setAttribute('x1', String(bcx + dx * 4.2)); tk.setAttribute('y1', String(badgeCy + dy * 4.2))
        tk.setAttribute('x2', String(bcx + dx * 7)); tk.setAttribute('y2', String(badgeCy + dy * 7))
        svg.appendChild(tk)
      }
      const dot = icon('circle')
      dot.setAttribute('cx', String(bcx)); dot.setAttribute('cy', String(badgeCy)); dot.setAttribute('r', '1.3')
      dot.setAttribute('fill', '#fff')
      svg.appendChild(dot)
    }
  }
}

// ── 组件本体 ─────────────────────────────────────────────────────────

// 有效命中行（外部显式传入单条 / 自动拉取最近 3 天列表 统一结构）
interface EffHit {
  id: string | null // 命中对应 scan_result.id（自动拉取时有值；用于与实际单归属比对）
  hitTime: string | null
  touchTime: string | null
  direction: 1 | -1 | 0
  trade: HitHistoryTrade | null
  signalType: string | null
}

interface Props {
  // 数据源二选一（同实例固定一种模式）
  symbol?: string // 扫描模式：实时K线 + AI 仓位标注
  tradeId?: string // 复盘模式：开单前200根 + 持仓全程 + 事件/区域标注
  limit?: number // 扫描模式拉取根数（默认 500）
  defaultInterval?: '1h' | '4h' | '1d' // 扫描模式缺省周期（默认 1h；关注页用 1d 看日K）
  ai?: AIAnalysis // 扫描模式 AI 仓位标注
  // 变化时强制重新拉取 K 线（关注列表手动刷新用）
  refreshKey?: number
  // 命中历史（2026-10-15，docs/22）：扫描模式下追加命中/触碰K线 marker
  // 与时间裁剪的开单区间；hitDirection 决定 marker 位于K线上方还是下方
  hitTime?: string | null // 命中K线时刻（ScanResult.created_at）
  touchTime?: string | null // 触碰K线时刻（回踩路径 stage1_candle_open）
  hitDirection?: 1 | -1 | 0 // 命中方向（1 多 / -1 空 / 0 未知）
  tradeOverlay?: HitHistoryTrade | null // 关联开单（时间裁剪区间）
  hitSignalType?: string | null // 命中信号类型（出点类型，追加在「命中」标签后）
  // 当前扫描结果 id（扫描模式）：该命中已有实际成交单时抑制 AI 计划线，
  // 只保留实际单覆盖层，避免同一笔在图上出现「做多」「开多」两组入场线
  scanResultId?: string
  // 历史缓存（2026-10-25 关注页）：开启后走 IndexedDB 缓存优先 + 左滑历史翻页。
  // 仅扫描模式有效；复盘模式（tradeId）仍走原有一次拉取
  cacheHistory?: boolean
  // 左滑每页根数（缓存不足时请求更早一页）
  pageSize?: number
}

export default function KlineChart({
  symbol, tradeId, limit = 500, defaultInterval = '1h', ai, refreshKey = 0,
  hitTime, touchTime, hitDirection = 0, tradeOverlay, hitSignalType, scanResultId,
  cacheHistory = false, pageSize = 1000,
}: Props) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const isReplay = !!tradeId
  const [data, setData] = useState<KlineData | TradeKlineData | null>(null)
  // 左滑 prepend 根数（与 data 同批更新）：ChartCore 据此保持视口不跳回最新
  const [prependCount, setPrependCount] = useState(0)
  // 尾部刷新但头部未变：保持视口原地不动（不套 corridor）
  const [keepViewport, setKeepViewport] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 缓存模式：数据源/周期变化用 epoch 失效在途请求，防止旧响应覆盖新周期
  const dataRef = useRef<KlineData | TradeKlineData | null>(null)
  const epochRef = useRef(0)
  const loadingMoreRef = useRef(false)
  const noMoreRef = useRef(false)
  // 更新 data 与 dataRef（同一批 setState：ChartCore 一次 render 同时拿到
  // 新数据与 prependCount，不会出现「新数据 + 旧 prepend」的错位帧）
  const applyData = useCallback(
    (
      next: KlineData | TradeKlineData | null,
      prepend = 0,
      keep = prepend > 0,
    ) => {
      dataRef.current = next
      setData(next)
      setPrependCount(prepend)
      setKeepViewport(keep)
    },
    [],
  )
  // 是否启用历史缓存（仅扫描模式）
  const cacheOn = !isReplay && !!symbol && cacheHistory
  // 「更新」按钮进行中状态
  const [updating, setUpdating] = useState(false)
  // 周期切换：reqInterval 为拉取周期（复盘模式 null=跟随服务端默认
  // cfg.kline_interval，可配非 1h/4h；扫描模式由 defaultInterval 指定，默认 '1h'），
  // serverInterval 为服务端实际返回档位。两者分离：首拉固化 serverInterval 不触发重拉
  const [reqInterval, setReqInterval] = useState<string | null>(
    isReplay ? null : defaultInterval,
  )
  const [serverInterval, setServerInterval] = useState<string>(
    isReplay ? '1h' : defaultInterval,
  )
  // 缓存键周期（仅缓存模式使用；扫描模式下拉档位固定 1h/4h/1d）
  const cacheInterval = (reqInterval ?? defaultInterval) as KlineInterval

  // 自动命中历史（2026-10-16）：所有扫描模式（symbol）K线图缺省显示该币最近
  // 3 天命中/触碰标注；外部已显式传入（HitHistoryPanel 指定行）时以传入为准，
  // 不再重复拉取；复盘模式（tradeId）已由事件箭头承载，不再叠加
  const [autoHits, setAutoHits] = useState<HitHistoryItem[]>([])
  const externalHit = !!(hitTime || touchTime)
  useEffect(() => {
    if (isReplay || !symbol || externalHit) return
    let cancelled = false
    const start = dayjs.utc().subtract(3, 'day').toISOString()
    scanApi
      .hitHistory({ symbol, start, page: 1, page_size: 20, sort_by: 'created_at', order: 'desc' })
      .then((r) => {
        if (!cancelled) setAutoHits(r.items ?? [])
      })
      .catch(() => {
        if (!cancelled) setAutoHits([])
      })
    return () => {
      cancelled = true
    }
  }, [isReplay, symbol, externalHit])
  useEffect(() => {
    setAutoHits([])
  }, [symbol])

  // 有效命中列表：外部显式传入（单条）优先，否则取自动拉取的最近 3 天列表
  const effHits: EffHit[] = useMemo(() => {
    if (externalHit) {
      return [{
        id: null,
        hitTime: hitTime ?? null,
        touchTime: touchTime ?? null,
        direction: hitDirection,
        trade: tradeOverlay ?? null,
        signalType: hitSignalType ?? null,
      }]
    }
    return autoHits.map((h) => ({
      id: h.id ?? null,
      hitTime: h.created_at ?? null,
      touchTime: h.stage1_candle_open ?? null,
      direction: (h.direction ?? 0) as 1 | -1 | 0,
      trade: h.trade ?? null,
      signalType: h.signal_type ?? null,
    }))
  }, [externalHit, hitTime, touchTime, hitDirection, tradeOverlay, hitSignalType, autoHits])

  // 同一扫描结果已开单时，AI 建议线（做多/止损/止盈）与实际成交线（开多/止损/止盈）
  // 是同一次决策的两个阶段，图上只保留实际成交数据（2026-10-22 用户拍板）
  const suppressAiPlan = useMemo(
    () => !!scanResultId && effHits.some((h) => h.id === scanResultId && !!h.trade),
    [scanResultId, effHits],
  )

  // 拉取 K 线（数据源/周期/refreshKey 变化重拉；首拉整块 loading，
  // 有数据时旧图保留平滑替换）
  useEffect(() => {
    if (cacheOn) return // 缓存模式由下方专用 effect 负责
    let cancelled = false
    setLoading(true)
    const req = isReplay
      ? scanApi.trades.klines(tradeId!, reqInterval ?? undefined)
      : scanApi.klines(symbol!, limit, false, reqInterval as '1h' | '4h' | '1d')
    req
      .then((d) => {
        if (!cancelled) {
          applyData(d)
          setError(null)
          setServerInterval(d.interval)
        }
      })
      .catch((e) => {
        // 展示后端 503/4xx 的 detail（如交易所封禁时长）
        if (!cancelled) setError(e?.response?.data?.detail || 'K线数据获取失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [cacheOn, isReplay, symbol, tradeId, limit, refreshKey, reqInterval, applyData])

  // 缓存模式（关注页）：缓存优先立即出图 → 后台刷新尾部并合并/落盘；
  // epoch 序号在币种/周期/刷新变化时作废在途响应，避免串图
  useEffect(() => {
    if (!cacheOn || !symbol) return
    const sym = symbol
    const iv = cacheInterval
    const epoch = ++epochRef.current
    let cancelled = false
    loadingMoreRef.current = false
    noMoreRef.current = false
    setLoading(true)

    const run = async () => {
      // 1) 缓存优先：先给已有历史（可能为空），首屏立即渲染
      let cached: Kline[] = []
      let meta: Awaited<ReturnType<typeof getMeta>> = null
      try {
        cached = await loadLatest(sym, iv, limit)
        meta = await getMeta(sym, iv)
      } catch {
        /* 缓存不可用：直接走网络 */
      }
      if (cancelled || epoch !== epochRef.current) return
      noMoreRef.current = !!meta?.noMore
      if (cached.length) {
        applyData({ symbol: sym, interval: iv, klines: cached })
        setServerInterval(iv)
        // 尾部仍新鲜（60s/10min 内）就不闪刷新提示
        setLoading(!isTailFresh(meta, iv))
      }

      // 2) 尾部刷新：新页为权威，覆盖未收盘根，更早历史保留
      try {
        const fresh = await scanApi.klines(sym, limit, false, iv)
        if (cancelled || epoch !== epochRef.current) return
        if (fresh.klines.length) {
          // 以当前展示数据为基准合并（用户可能已在等网络时左滑加载了更早历史），
          // 尾部新页权威覆盖；有断层则丢弃旧段并复位 noMore（允许重新翻历史）。
          // live 必须与当前 symbol/interval 同源，否则（切周期瞬间）会混入异周期数据
          const live = dataRef.current as KlineData | null
          const liveSame =
            !!live && live.symbol === sym && live.interval === iv && live.klines.length > 0
          const base = liveSame ? live.klines : cached
          if (liveSame && tailHasGap(base, fresh.klines, iv)) noMoreRef.current = false
          const merged = mergeTailFresh(base, fresh.klines, iv)
          // 头部未变（用户已翻出的更早历史仍保留）时保持视口；否则回最新视图
          const headKept = liveSame && merged[0]?.time === live.klines[0].time
          applyData(
            {
              symbol: fresh.symbol,
              interval: fresh.interval,
              klines: merged,
              swings: fresh.swings,
            },
            0,
            headKept,
          )
          setServerInterval(fresh.interval)
          void saveTail(sym, iv, fresh.klines)
        } else if (!cached.length) {
          applyData({ symbol: fresh.symbol, interval: fresh.interval, klines: [] })
        }
        setError(null)
      } catch (e: any) {
        if (!cancelled && epoch === epochRef.current) {
          setError(e?.response?.data?.detail || 'K线数据获取失败')
        }
      } finally {
        if (!cancelled && epoch === epochRef.current) setLoading(false)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [cacheOn, symbol, limit, refreshKey, reqInterval, cacheInterval, applyData])

  // 数据源切换（换币/换单）：立即清旧数据防串图（周期切换不清，旧图平滑替换）
  useEffect(() => {
    applyData(null)
    setError(null)
    noMoreRef.current = false
    loadingMoreRef.current = false
  }, [symbol, tradeId, applyData])

  // 左滑到左沿：缓存优先取更早一页；缓存不足再按 end_time 请求交易所历史页。
  // 只接受与当前最早一根严格相邻的页（差一个周期），有缺口即停止，避免图上串段
  const handleReachLeft = useCallback(async () => {
    if (!cacheOn || !symbol) return
    if (loadingMoreRef.current || noMoreRef.current) return
    const live = dataRef.current as KlineData | null
    // 只对当前 symbol/interval 的同源数据翻页（切周期瞬间旧图仍在，不能混用）
    if (!live || live.symbol !== symbol || live.interval !== cacheInterval || !live.klines.length) {
      return
    }
    const sym = symbol
    const iv = cacheInterval
    const epoch = epochRef.current
    const oldestMs = live.klines[0].time
    const stepMs = INTERVAL_SEC[iv] * 1000
    loadingMoreRef.current = true
    try {
      const cachedOlder = await loadOlder(sym, iv, oldestMs, pageSize)
      if (epoch !== epochRef.current) return
      if (cachedOlder.length) {
        const lastCached = cachedOlder[cachedOlder.length - 1].time
        if (lastCached + stepMs === oldestMs) {
          const cur = dataRef.current as KlineData | null
          // 等缓存期间可能发生尾部断层重建（头部变了）：本页已不连续，丢弃
          if (!cur || cur.symbol !== sym || cur.interval !== iv || cur.klines[0].time !== oldestMs) {
            return
          }
          applyData(
            { ...cur, klines: mergeOlderBars(cur.klines, cachedOlder) },
            cachedOlder.length,
          )
          return
        }
        // 缓存段与当前最早一根不连续：忽略该段，继续走交易所拉正确的一页
      }

      const meta = await getMeta(sym, iv)
      if (epoch !== epochRef.current) return
      if (meta?.noMore) {
        noMoreRef.current = true
        return
      }

      const resp = await scanApi.klinesBefore(sym, pageSize, oldestMs - 1, iv)
      if (epoch !== epochRef.current) return
      const before = resp.klines.filter((k) => k.time < oldestMs)
      if (!before.length) {
        noMoreRef.current = true
        void saveOlder(sym, iv, [], false)
        return
      }
      const lastNew = before[before.length - 1].time
      if (lastNew + stepMs !== oldestMs) {
        noMoreRef.current = true
        return
      }
      const cur = dataRef.current as KlineData | null
      // 等网络期间可能发生尾部断层重建（头部变了）：本页已不连续，丢弃不写缓存
      if (!cur || cur.symbol !== sym || cur.interval !== iv || cur.klines[0].time !== oldestMs) {
        return
      }
      applyData(
        { ...cur, klines: mergeOlderBars(cur.klines, before) },
        before.length,
      )
      // 交易所明确没有更早数据 → 本会话直接停；否则允许继续左滑
      if (resp.has_more === false) noMoreRef.current = true
      void saveOlder(sym, iv, before, resp.has_more !== false)
    } catch (e: any) {
      if (epoch === epochRef.current) {
        setError(e?.response?.data?.detail || '更早K线获取失败')
      }
    } finally {
      loadingMoreRef.current = false
    }
  }, [cacheOn, symbol, cacheInterval, pageSize, applyData])

  // 「更新」按钮：扫描模式=跳过缓存直连交易所拉最新K线；复盘模式=重拉
  // （在跑单的持仓区间会延长）。缓存模式下只刷新尾部，已加载的更早历史保留
  const handleUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      if (isReplay) {
        const d = await scanApi.trades.klines(tradeId!, reqInterval ?? undefined)
        applyData(d)
        setServerInterval(d.interval)
      } else if (cacheOn) {
        const iv = cacheInterval
        const epoch = epochRef.current
        const fresh = await scanApi.klines(symbol!, limit, true, iv)
        if (epoch !== epochRef.current) return // 期间切了币种/周期：丢弃旧响应
        const live = dataRef.current as KlineData | null
        const liveSame =
          !!live && live.symbol === symbol && live.interval === iv && live.klines.length > 0
        if (liveSame && tailHasGap(live.klines, fresh.klines, iv)) {
          noMoreRef.current = false
        }
        const merged = liveSame
          ? mergeTailFresh(live.klines, fresh.klines, iv)
          : fresh.klines
        const headKept = liveSame && merged[0]?.time === live.klines[0].time
        applyData({ ...fresh, klines: merged }, 0, headKept)
        setServerInterval(fresh.interval)
        void saveTail(symbol!, iv, fresh.klines)
      } else {
        const d = await scanApi.klines(symbol!, limit, true, reqInterval as '1h' | '4h' | '1d')
        applyData(d)
        setServerInterval(d.interval)
      }
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'K线更新失败')
    } finally {
      setUpdating(false)
    }
  }, [isReplay, cacheOn, symbol, tradeId, limit, reqInterval, cacheInterval, applyData])

  // ── 复盘模式派生件（仅 tradeId 模式计算）────────────────────────────
  const replayData = isReplay ? (data as TradeKlineData | null) : null
  const isLong = replayData?.direction !== 'short'
  // 两模式 klines 同构（KlineData / TradeKlineData 均有 klines）
  const klines: Kline[] = data?.klines ?? []

  // 周期切换档位（复盘模式）：1h/4h/1d 固定顺序，服务端实际档（cfg.kline_interval
  // 可配非 1h/4h/1d，如 15m/30m）按固定档序就近插入——之前把 serverInterval
  // 放在最前，切到 4h 后列表变 [4h,1h]，选中档跑到了 1h 前面（位置漂移）
  // 扫描模式固定 1h/4h/1d（ChartCore 缺省档；2026-10-14 加入 1d 日K）
  const intervalOptions = useMemo(() => {
    if (!isReplay) return undefined
    const base = ['1h', '4h', '1d']
    const extra = base.includes(serverInterval) ? [] : [serverInterval]
    return [...new Set([...base, ...extra])]
      .map((v) => ({ label: v, value: v }))
  }, [isReplay, serverInterval])

  // K 线标记（useMemo 保持引用稳定：ChartCore 以引用判断是否重应用标记）：
  // 复盘模式=事件箭头；命中历史的命中/触碰标注改由 SVG 图形层绘制（见 drawHitMarks）
  const extraMarkers = useMemo<SeriesMarker<UTCTimestamp>[]>(() => {
    const markers: SeriesMarker<UTCTimestamp>[] = []
    if (replayData) {
      for (const ev of replayData.events) {
        const m = eventMarker(ev, isLong, colorScheme)
        if (!m) continue
        const evMs = utcMs(ev.created_at)
        // 找事件时刻所在的K线（time <= evMs 的最后一根；早于首根则取首根）
        let time = replayData.klines[0] ? (Math.floor(replayData.klines[0].time / 1000) as UTCTimestamp) : undefined
        for (const k of replayData.klines) {
          if (k.time <= evMs) time = Math.floor(k.time / 1000) as UTCTimestamp
          else break
        }
        if (time == null) continue
        markers.push({ ...m, time, size: 1 })
      }
    }
    return markers.sort((a, b) => (a.time as number) - (b.time as number))
  }, [replayData, isLong, colorScheme])

  // 命中历史可视起点（2026-10-15）：命中/触碰较早时把可视窗口左沿收到其附近，
  // 保证标注入镜；命中在最近约 130 根内则维持默认窗口（取两者的更靠前者）
  const overlayFrom = useMemo(() => {
    if (isReplay || !klines.length || !effHits.length) return undefined
    const cands = effHits
      .flatMap((h) => [h.hitTime, h.touchTime])
      .filter((v): v is string => !!v)
      .map(utcMs)
    if (!cands.length) return undefined
    const earliest = Math.min(...cands)
    let idx = 0
    for (let i = 0; i < klines.length; i++) {
      if (klines[i].time <= earliest) idx = i
      else break
    }
    return Math.min(Math.max(0, klines.length - 130), Math.max(0, idx - 5))
  }, [isReplay, klines, effHits])

  // 区域覆盖层回调（ChartCore 每帧在空白 SVG 上调用）：
  // 复盘模式=全区间价格带；命中历史=时间裁剪的开单区间 + 命中/触碰图形化标注
  const drawZones = useCallback(
    (ctx: OverlayCtx) => {
      if (replayData) {
        drawReplayZones(replayData, colorScheme, ctx)
        return
      }
      if (!klines.length) return
      // 每条命中的关联开单区间（多条命中可能多个区间）
      for (const h of effHits) {
        if (h.trade) drawHitOverlay(klines, h.trade, colorScheme, ctx)
      }
      const marks: Array<{ ms: number; color: string; fill: string; label: string; kind: 'hit' | 'touch'; preferBelow: boolean }> = []
      const ivMs = INTERVAL_MS[serverInterval] ?? INTERVAL_MS['1h']
      for (const h of effHits) {
        // 命中方向：1 多（上涨）→ K线下方； -1 空（下跌）→ K线上方
        const preferBelow = h.direction >= 0
        // 命中标签后追加信号类型（出点类型，2026-10-16）
        const sigLabel = h.signalType ? (SIGNAL_TYPE_MAP[h.signalType]?.label ?? h.signalType) : ''
        const hitLabel = sigLabel ? `命中 · ${sigLabel}` : '命中'
        // 命中锚定（2026-10-16）：hitTime=扫描发现时刻，扫描周期固定 1h，
        // 信号K线=扫描周期最后一根已收盘K线（开口=hitTime 所在小时开口-1h）；
        // 再映射到当前图表周期：取「包含信号时刻」的周期K线——1h 图=已收盘信号根；
        // 4h/1d 图=包含它的那根（可能未收盘，照样标注）
        if (h.hitTime) {
          const sigMs = Math.floor(utcMs(h.hitTime) / SCAN_INTERVAL_MS) * SCAN_INTERVAL_MS - SCAN_INTERVAL_MS
          const anchorMs = Math.floor(sigMs / ivMs) * ivMs
          marks.push({ ms: anchorMs, color: HIT_MARK_COLOR, fill: HIT_MARK_FILL, label: hitLabel, kind: 'hit', preferBelow })
        }
        if (h.touchTime) marks.push({ ms: utcMs(h.touchTime), color: TOUCH_MARK_COLOR, fill: TOUCH_MARK_FILL, label: '触碰', kind: 'touch', preferBelow })
      }
      if (marks.length) drawHitMarks(klines, marks, ctx)
    },
    [replayData, colorScheme, effHits, klines, serverInterval],
  )

  // 无数据（首拉中/首拉失败/新周期返回空）：整块反馈；
  // 有数据时（切周期/更新）旧图保留，错误只出图上方窄条
  if (!data || data.klines.length === 0) {
    if (loading) {
      return (
        <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', height: 420 }}>
          <Spin size="large" />
          <div style={{ marginTop: 12, color: '#8b93a7', fontSize: 12 }}>拉取K线中…</div>
        </div>
      )
    }
    if (error) {
      return <Alert type="warning" showIcon message={isReplay ? '复盘K线获取失败' : 'K线数据获取失败'} description={error} />
    }
    return <Empty description="无K线数据" style={{ marginTop: 60 }} />
  }

  const watermark = data.symbol

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 360 }}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message={isReplay ? '复盘K线更新失败' : 'K线更新失败'}
          description={error}
          style={{ marginBottom: 8 }}
        />
      )}
      <ChartCore
        // chartKey 不含周期：图表实例跨周期复用——切周期时新数据由 ChartCore
        // 的 data effect 原地 setData（可视范围按时间锚定恢复，指标同提交重建）
        chartKey={isReplay ? tradeId! : data.symbol}
        watermark={watermark}
        label={data.symbol}
        data={data}
        // 可视起点：复盘缺省「最近约 130 根」；命中历史由 overlayFrom 把左沿
        // 收到命中/触碰K线附近（保证标注入镜），其余扫描场景同缺省
        from={overlayFrom}
        ai={isReplay || suppressAiPlan ? undefined : ai}
        // 复盘=事件箭头；命中历史走 SVG 图形标注（见 drawOverlay）
        extraMarkers={isReplay ? extraMarkers : undefined}
        drawOverlay={isReplay || effHits.length > 0 ? drawZones : undefined}
        height={CHART_HEIGHT}
        onUpdate={handleUpdate}
        updating={updating}
        interval={serverInterval}
        onIntervalChange={(v) => setReqInterval(v)}
        intervalOptions={intervalOptions}
        onReachLeft={cacheOn ? handleReachLeft : undefined}
        prependCount={cacheOn ? prependCount : 0}
        keepViewport={cacheOn ? keepViewport : false}
      />
      {/* 切周期/更新重拉中：旧图保留，右上角轻量反馈（不遮工具栏与价格轴） */}
      {loading && (
        <div
          style={{
            position: 'absolute',
            top: 6,
            right: 66,
            zIndex: 30,
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '2px 10px',
            background: 'rgba(19, 23, 34, 0.72)',
            borderRadius: 10,
            fontSize: 12,
            color: '#8b93a7',
          }}>
          <Spin size="small" /> 拉取K线中…
        </div>
      )}
    </div>
  )
}
