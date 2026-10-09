/**
 * K 线图（全站唯一组件，2026-10-09 封装层合一）
 * 图表能力（指标/图例/工具栏/仓位价位线/SVG 覆盖层）全部在 ChartCore，
 * 本组件只负责数据拉取与两种数据源模式的差异注入：
 * - 扫描模式（symbol）：实时K线（扫描表/待确认池/搜索/关注列表共用），
 *   AI 仓位标注；拉取根数 limit 可配
 * - 复盘模式（tradeId）：/trades/{id}/klines 一次返回——开单前 100 根 +
 *   持仓期间直到止损/平仓；事件箭头（extraMarkers）+ 复盘区域覆盖层
 *   （止损区/止盈区/现止损琥珀线）；界面件（水印/图例/工具栏/摆动标注/
 *   可视范围）与扫描模式完全一致，交易信息由图上价签与区域承载
 * 交互统一（同 2026-10-08 复盘图口径）：切周期不清旧数据不整块 loading，
 * 旧图保留、新数据到达后 ChartCore 原地替换；仅首拉（无数据）整块反馈。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Empty, Spin } from 'antd'
import type { SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { scanApi } from '../api/scan'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import { fmtPrice, pctDelta, rrRatio } from './klineShared'
import { dayjs } from '../utils/dayjs'
import ChartCore, { type OverlayCtx } from './ChartCore'
import type { AIAnalysis, KlineData, TradeKlineData, TradeKlineEvent } from '../types'

const CHART_HEIGHT = 560 // 容器无高度时的兜底值（各挂载点容器均有自身高度）

// 后端 UTC naive → 毫秒（统一按 UTC 解析）
const utcMs = (v: string) => dayjs.utc(v).valueOf()

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

  // RR 徽标（TV 仓位框 riskRewardRatio，与 ChartCore AI 标注同款）：
  // 区域水平居中、入场↔止盈1（缺则入场↔止损）垂直居中
  const rr = rrRatio(ep, data.stop_loss, data.tp1)
  if (rr != null) {
    const anchor = data.tp1 ?? data.stop_loss
    const yA = series.priceToCoordinate(anchor!)
    if (yA != null) {
      const text = `RR 1:${rr.toFixed(1)}`
      const ctx2 = document.createElement('canvas').getContext('2d')!
      ctx2.font = 'bold 10px monospace'
      const boxW = ctx2.measureText(text).width + 10
      const boxH = 15
      const cx = x0 + (rightEdge - x0) / 2
      const cy = (yEntry + yA) / 2
      const rect = el('rect')
      rect.setAttribute('x', String(cx - boxW / 2))
      rect.setAttribute('y', String(cy - boxH / 2))
      rect.setAttribute('width', String(boxW))
      rect.setAttribute('height', String(boxH))
      rect.setAttribute('fill', 'rgba(13, 17, 27, 0.85)')
      rect.setAttribute('stroke', '#2a2e39')
      rect.setAttribute('rx', '3')
      svg.appendChild(rect)
      const t = el('text')
      t.setAttribute('x', String(cx - boxW / 2 + 5))
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

// ── 组件本体 ─────────────────────────────────────────────────────────

interface Props {
  // 数据源二选一（同实例固定一种模式）
  symbol?: string // 扫描模式：实时K线 + AI 仓位标注
  tradeId?: string // 复盘模式：开单前100根 + 持仓全程 + 事件/区域标注
  limit?: number // 扫描模式拉取根数（默认 500）
  ai?: AIAnalysis // 扫描模式 AI 仓位标注
  // 变化时强制重新拉取 K 线（关注列表手动刷新用）
  refreshKey?: number
}

export default function KlineChart({ symbol, tradeId, limit = 500, ai, refreshKey = 0 }: Props) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const isReplay = !!tradeId
  const [data, setData] = useState<KlineData | TradeKlineData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 「更新」按钮进行中状态
  const [updating, setUpdating] = useState(false)
  // 周期切换：reqInterval 为拉取周期（复盘模式 null=跟随服务端默认
  // cfg.kline_interval，可配非 1h/4h；扫描模式固定 '1h' 起步），serverInterval
  // 为服务端实际返回档位。两者分离：首拉固化 serverInterval 不触发重拉
  const [reqInterval, setReqInterval] = useState<string | null>(isReplay ? null : '1h')
  const [serverInterval, setServerInterval] = useState<string>('1h')

  // 拉取 K 线（数据源/周期/refreshKey 变化重拉；首拉整块 loading，
  // 有数据时旧图保留平滑替换）
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const req = isReplay
      ? scanApi.trades.klines(tradeId!, reqInterval ?? undefined)
      : scanApi.klines(symbol!, limit, false, reqInterval as '1h' | '4h')
    req
      .then((d) => {
        if (!cancelled) {
          setData(d)
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
  }, [isReplay, symbol, tradeId, limit, refreshKey, reqInterval])

  // 数据源切换（换币/换单）：立即清旧数据防串图（周期切换不清，旧图平滑替换）
  useEffect(() => {
    setData(null)
    setError(null)
  }, [symbol, tradeId])

  // 「更新」按钮：扫描模式=跳过缓存直连交易所拉最新K线；复盘模式=重拉
  // （在跑单的持仓区间会延长）
  const handleUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      const d = isReplay
        ? await scanApi.trades.klines(tradeId!, reqInterval ?? undefined)
        : await scanApi.klines(symbol!, limit, true, reqInterval as '1h' | '4h')
      setData(d)
      setServerInterval(d.interval)
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'K线更新失败')
    } finally {
      setUpdating(false)
    }
  }, [isReplay, symbol, tradeId, limit, reqInterval])

  // ── 复盘模式派生件（仅 tradeId 模式计算）────────────────────────────
  const replayData = isReplay ? (data as TradeKlineData | null) : null
  const isLong = replayData?.direction !== 'short'

  // 周期切换档位（复盘模式）：服务端实际档 + 1h/4h，去重保序；
  // 扫描模式固定 1h/4h（ChartCore 缺省档）
  const intervalOptions = useMemo(() => {
    if (!isReplay) return undefined
    const vals = [serverInterval, '1h', '4h']
    return [...new Set(vals)].map((v) => ({ label: v, value: v }))
  }, [isReplay, serverInterval])

  // 事件箭头标记（useMemo 保持引用稳定：ChartCore 以引用判断是否重应用标记）
  const extraMarkers = useMemo<SeriesMarker<UTCTimestamp>[]>(() => {
    if (!replayData) return []
    const markers: SeriesMarker<UTCTimestamp>[] = []
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
    return markers.sort((a, b) => (a.time as number) - (b.time as number))
  }, [replayData, isLong, colorScheme])

  // 复盘区域覆盖层回调（ChartCore 每帧在空白 SVG 上调用）
  const drawZones = useCallback(
    (ctx: OverlayCtx) => {
      if (replayData) drawReplayZones(replayData, colorScheme, ctx)
    },
    [replayData, colorScheme],
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
        // 可视范围不传 from：两模式同为「最近约 130 根」缺省（复盘不再全区间压缩）
        ai={isReplay ? undefined : ai}
        extraMarkers={isReplay ? extraMarkers : undefined}
        drawOverlay={isReplay ? drawZones : undefined}
        height={CHART_HEIGHT}
        onUpdate={handleUpdate}
        updating={updating}
        interval={serverInterval}
        onIntervalChange={(v) => setReqInterval(v)}
        intervalOptions={intervalOptions}
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
