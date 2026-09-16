/**
 * 交易复盘K线图（交易记录列表点击币种弹窗）
 * - 数据：后端 /trades/{id}/klines 一次返回——开单前 100 根 + 持仓期间直到止损/平仓，
 *   附开单时初始止损止盈（OPEN 事件快照）与关键事件（开仓/TP1/TP2/止损成交）
 * - 区域：止损区（入场→SL，红半透明）、止盈1/2区（绿半透明），从开仓K线延伸到图右缘
 * - 线：入场（实线，方向色）、止损/止盈（虚线），右侧价签
 * - 事件：箭头标注在对应K线上（开仓蓝、止盈涨色、止损跌色），多空方向决定上下位置
 * - 配色跟随全局涨跌方案（红涨绿跌 / 绿涨红跌）
 */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import { Alert, Empty, Spin, Tag, Typography } from 'antd'
import {
  createChart,
  createTextWatermark,
  createSeriesMarkers,
  CandlestickSeries,
  LineStyle,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import { scanApi } from '../api/scan'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import { bj, dayjs, BJ } from '../utils/dayjs'
import type { TradeKlineData, TradeKlineEvent } from '../types'

const { Text } = Typography

const CHART_HEIGHT = 520 // 容器无高度时的兜底值

const CANDLE_COLORS: Record<ColorScheme, { up: string; down: string }> = {
  'red-up': { up: '#ef5350', down: '#26a69a' },
  'green-up': { up: '#26a69a', down: '#ef5350' },
}

const fmtPrice = (p: number) => (p < 1 ? p.toFixed(6) : p < 100 ? p.toFixed(4) : p.toFixed(2))
const fmtTime = (sec: number) => dayjs.unix(sec).tz(BJ).format('MM-DD HH:mm')
// 后端 UTC naive → 毫秒（统一按 UTC 解析）
const utcMs = (v: string) => dayjs.utc(v).valueOf()

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

// 悬浮玻璃拟态标签（与 KlineChart 同风格）
const GLASS: CSSProperties = {
  background: 'rgba(19, 23, 34, 0.72)',
  backdropFilter: 'blur(10px)',
  WebkitBackdropFilter: 'blur(10px)',
  border: '1px solid rgba(66, 74, 96, 0.45)',
  borderRadius: 8,
  boxShadow: '0 4px 16px rgba(0, 0, 0, 0.25)',
}

export default function TradeReplayChart({ tradeId }: { tradeId: string }) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const [data, setData] = useState<TradeKlineData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 拉取复盘数据（tradeId 变化重拉）
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setData(null)
    scanApi.trades
      .klines(tradeId)
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.detail || '复盘K线获取失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [tradeId])

  const isLong = data?.direction !== 'short'

  // 初始化图表 + 写入K线与事件标记
  useEffect(() => {
    const container = containerRef.current
    if (!container || !data) return

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.VerticalGradient, topColor: '#1c2231', bottomColor: '#12151f' },
        textColor: '#c9cfdd',
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: '#1d2332' },
        horzLines: { color: '#1d2332' },
      },
      rightPriceScale: {
        borderColor: '#252b3b',
        entireTextOnly: true,
        scaleMargins: { top: 0.18, bottom: 0.08 },
      },
      timeScale: {
        borderColor: '#252b3b',
        timeVisible: false,
        secondsVisible: false,
        rightOffset: 4,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#363c4e' },
        horzLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#363c4e' },
      },
      localization: {
        priceFormatter: (p: number) => fmtPrice(p),
      },
      width: container.clientWidth || 520,
      height: container.clientHeight || CHART_HEIGHT,
    })

    // 主图中央水印（币种 + 方向）
    createTextWatermark(chart.panes()[0], {
      lines: [
        {
          text: `${data.symbol} ${isLong ? '做多' : '做空'}`,
          color: 'rgba(148, 158, 184, 0.09)',
          fontSize: 44,
          fontStyle: 'bold',
          fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        },
      ],
    })

    const candleColors = CANDLE_COLORS[colorScheme]
    const series = chart.addSeries(CandlestickSeries, {
      upColor: candleColors.up,
      downColor: candleColors.down,
      borderVisible: false,
      wickUpColor: candleColors.up,
      wickDownColor: candleColors.down,
    })
    chartRef.current = chart
    seriesRef.current = series

    const candleData = data.klines.map((k) => ({
      time: Math.floor(k.time / 1000) as UTCTimestamp,
      open: k.open,
      high: k.high,
      low: k.low,
      close: k.close,
    }))
    series.setData(candleData)
    // 复盘视图固定展示全部根数（开单前100根 + 持仓段，量级适中）
    chart.timeScale().setVisibleLogicalRange({ from: -2, to: candleData.length + 4 })

    // 事件箭头：事件时间向下取整到所在K线（事件落在K线中间，取该K线）
    const markers: SeriesMarker<UTCTimestamp>[] = []
    for (const ev of data.events) {
      const m = eventMarker(ev, isLong, colorScheme)
      if (!m) continue
      const evMs = utcMs(ev.created_at)
      // 找事件时刻所在的K线（time <= evMs 的最后一根；早于首根则取首根）
      let time = candleData[0]?.time
      for (const c of candleData) {
        if (c.time * 1000 <= evMs) time = c.time
        else break
      }
      if (time == null) continue
      markers.push({ ...m, time, size: 1 })
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number))
    if (markers.length > 0) createSeriesMarkers(series, markers)

    // 自适应容器宽高（弹窗内）
    const resize = () => {
      if (container && chartRef.current) {
        chartRef.current.applyOptions({
          width: container.clientWidth,
          height: container.clientHeight || CHART_HEIGHT,
        })
      }
    }
    const ro = new ResizeObserver(resize)
    ro.observe(container)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
    // colorScheme 变化时重建（联动K线颜色与箭头配色）
  }, [data, colorScheme, isLong])

  // 区域色块 + 风控线（SVG 覆盖层，rAF 循环重绘保证缩放/平移同步）
  const drawZones = useCallback(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    const svg = svgRef.current
    const container = containerRef.current
    if (!chart || !series || !svg || !container || !data) return

    while (svg.firstChild) svg.removeChild(svg.firstChild)

    const isL = data.direction !== 'short'
    const { up, down } = SCHEME_UP_DOWN[colorScheme]
    const colorEntry = isL ? up : down
    const colorSL = down
    const colorTP = up

    const fullWidth = container.clientWidth || 520
    const height = container.clientHeight || CHART_HEIGHT
    const priceScaleWidth = chart.priceScale('right').width()
    const rightEdge = Math.max(20, fullWidth - priceScaleWidth)
    svg.setAttribute('width', String(fullWidth))
    svg.setAttribute('height', String(height))
    svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

    if (data.entry_price == null) return

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
      const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
      r.setAttribute('x', String(x0))
      r.setAttribute('y', String(Math.min(y1, y2)))
      r.setAttribute('width', String(rightEdge - x0))
      r.setAttribute('height', String(Math.abs(y2 - y1)))
      r.setAttribute('fill', fill)
      svg.appendChild(r)
    }
    drawZone(data.entry_price, data.stop_loss, 'rgba(239, 83, 80, 0.15)')
    drawZone(data.entry_price, data.tp1, 'rgba(38, 166, 154, 0.15)')
    drawZone(data.tp1, data.tp2, 'rgba(38, 166, 154, 0.08)')

    // 水平线 + 右侧价签
    const drawLine = (price: number | null, color: string, label: string, dashed: boolean) => {
      if (price == null) return
      const y = series.priceToCoordinate(price)
      if (y == null) return
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line')
      line.setAttribute('x1', String(x0))
      line.setAttribute('y1', String(y))
      line.setAttribute('x2', String(rightEdge))
      line.setAttribute('y2', String(y))
      line.setAttribute('stroke', color)
      line.setAttribute('stroke-width', '1')
      line.setAttribute('shape-rendering', 'crispEdges')
      if (dashed) line.setAttribute('stroke-dasharray', '5,3')
      svg.appendChild(line)

      const text = `${label} ${fmtPrice(price)}`
      const font = 'bold 10px monospace'
      const ctx = document.createElement('canvas').getContext('2d')!
      ctx.font = font
      const tw = ctx.measureText(text).width
      const boxW = tw + 8
      const boxH = 13
      const boxX = rightEdge - boxW

      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
      rect.setAttribute('x', String(boxX))
      rect.setAttribute('y', String(y - boxH / 2))
      rect.setAttribute('width', String(boxW))
      rect.setAttribute('height', String(boxH))
      rect.setAttribute('fill', color)
      svg.appendChild(rect)

      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text')
      t.setAttribute('x', String(boxX + 4))
      t.setAttribute('y', String(y + 3))
      t.setAttribute('fill', '#fff')
      t.setAttribute('font-size', '10')
      t.setAttribute('font-family', 'monospace')
      t.setAttribute('font-weight', 'bold')
      t.textContent = text
      svg.appendChild(t)
    }
    drawLine(data.entry_price, colorEntry, isL ? '开多' : '开空', false)
    drawLine(data.stop_loss, colorSL, '止损', true)
    drawLine(data.tp1, colorTP, '止盈1', true)
    drawLine(data.tp2, colorTP, '止盈2', true)
  }, [data, colorScheme])

  useEffect(() => {
    if (!data) return
    // 立即绘制一次（不依赖 rAF：后台/被遮挡标签页 rAF 会暂停，导致区域一直空白）
    drawZones()
    let rafId = 0
    const loop = () => {
      drawZones()
      rafId = requestAnimationFrame(loop)
    }
    rafId = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(rafId)
  }, [data, drawZones])

  if (loading) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', height: 420 }}>
        <Spin size="large" />
        <div style={{ marginTop: 12, color: '#8b93a7', fontSize: 12 }}>
          拉取复盘K线中（历史区间直连交易所，稍慢属正常）
        </div>
      </div>
    )
  }
  if (error) {
    return <Alert type="warning" showIcon message="复盘K线获取失败" description={error} />
  }
  if (!data || data.klines.length === 0) {
    return <Empty description="无K线数据" style={{ marginTop: 60 }} />
  }

  const statusPnl =
    data.exit_reason != null ? ` · ${data.exit_reason}` : ''

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 420 }}>
      {/* 左上角图例：区间说明 + 首尾时间 */}
      <div
        style={{
          position: 'absolute',
          top: 8,
          left: 8,
          zIndex: 20,
          ...GLASS,
          padding: '4px 10px',
          fontSize: 11,
          lineHeight: '18px',
          pointerEvents: 'none',
          color: '#8b93a7',
        }}>
        <span style={{ color: '#e8ebf2', fontWeight: 600 }}>{data.symbol}</span>{' '}
        <Tag color={isLong ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down} style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '16px' }}>
          {isLong ? '多' : '空'}
        </Tag>{' '}
        <span>
          {data.interval} · 开单前100根 + 持仓全程{statusPnl}
        </span>
        <div>
          {fmtTime(Math.floor(data.klines[0].time / 1000))} →{' '}
          {fmtTime(Math.floor(data.klines[data.klines.length - 1].time / 1000))} · 共 {data.klines.length} 根
        </div>
        <div>
          {isLong ? '开多' : '开空'}{' '}
          <span style={{ color: '#c9cfdd' }}>{data.entry_price != null ? fmtPrice(data.entry_price) : '—'}</span>
          {data.stop_loss != null && (
            <>
              {' '}· 止损 <span style={{ color: SCHEME_UP_DOWN[colorScheme].down }}>{fmtPrice(data.stop_loss)}</span>
            </>
          )}
          {data.tp1 != null && (
            <>
              {' '}· 止盈1 <span style={{ color: SCHEME_UP_DOWN[colorScheme].up }}>{fmtPrice(data.tp1)}</span>
            </>
          )}
          <span style={{ marginLeft: 8, color: '#5a6377' }}>{bj(data.opened_at).format('MM-DD HH:mm')} 开仓</span>
        </div>
      </div>
      <div
        ref={containerRef}
        style={{
          width: '100%',
          height: '100%',
          minHeight: 420,
          borderRadius: 8,
          overflow: 'hidden',
          border: '1px solid #232838',
        }}
      />
      <svg
        ref={svgRef}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          height: '100%',
          pointerEvents: 'none',
          zIndex: 10,
        }}
      />
      <Text type="secondary" style={{ position: 'absolute', bottom: 6, right: 10, fontSize: 10, zIndex: 20, color: '#5a6377' }}>
        初始止损止盈（开单时快照）· 跟踪止损移动见操作历史
      </Text>
    </div>
  )
}
