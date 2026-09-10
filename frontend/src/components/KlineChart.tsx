import { useEffect, useRef } from 'react'
import {
  createChart,
  CandlestickSeries,
  LineStyle,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  type IPriceLine,
} from 'lightweight-charts'
import { scanApi } from '../api/scan'
import type { AIAnalysis, Kline } from '../types'

interface Props {
  symbol: string
  limit?: number
  ai?: AIAnalysis
}

const CHART_HEIGHT = 560 // 容器无高度时的兜底值

export default function KlineChart({ symbol, limit = 100, ai }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const priceLinesRef = useRef<IPriceLine[]>([])
  const redrawFnRef = useRef<(() => void) | null>(null)

  // 初始化图表 + 拉取数据
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      layout: {
        background: { color: '#131722' },
        textColor: '#d1d4dc',
        fontFamily: 'monospace',
      },
      grid: {
        vertLines: { color: '#1e222d' },
        horzLines: { color: '#1e222d' },
      },
      rightPriceScale: {
        borderColor: '#2b2b43',
        scaleMargins: { top: 0.05, bottom: 0.05 },
      },
      timeScale: {
        borderColor: '#2b2b43',
        timeVisible: false,
        secondsVisible: false,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#758696' },
        horzLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#758696' },
      },
      width: container.clientWidth || 520,
      height: container.clientHeight || CHART_HEIGHT,
    })

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderVisible: false,
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    })

    chartRef.current = chart
    seriesRef.current = series

    let cancelled = false
    scanApi
      .klines(symbol, limit)
      .then((data) => {
        if (cancelled || !seriesRef.current) return
        const candleData = data.klines.map((k: Kline) => ({
          time: Math.floor(k.time / 1000) as UTCTimestamp,
          open: k.open,
          high: k.high,
          low: k.low,
          close: k.close,
        }))
        seriesRef.current.setData(candleData)
        chart.timeScale().fitContent()
        // 数据加载完成后触发 AI 仓位标注重绘（等待布局完成）
        requestAnimationFrame(() => redrawFnRef.current?.())
      })
      .catch(() => {
        /* ignore */
      })

    // 自适应容器宽高
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
      cancelled = true
      ro.disconnect()
      priceLinesRef.current.forEach((l) => series.removePriceLine(l))
      priceLinesRef.current = []
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [symbol, limit])

  // 更新 AI 价格线 + 区域色块（TradingView 仓位标注风格）
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    const svg = svgRef.current
    if (!chart || !series || !svg) return

    // 清除旧价格线
    priceLinesRef.current.forEach((l) => series.removePriceLine(l))
    priceLinesRef.current = []

    if (!ai) {
      redrawFnRef.current = null
      return
    }

    // 辅助：创建 SVG 元素
    const NS = 'http://www.w3.org/2000/svg'
    const el = (tag: string) => document.createElementNS(NS, tag)

    const fmt = (p: number) =>
      p < 1 ? p.toFixed(6) : p < 100 ? p.toFixed(4) : p.toFixed(2)

    const isLong = ai.direction !== 'short'

    // 颜色配置
    const colorEntry = isLong ? '#26a69a' : '#ef5350'
    const colorSL = '#ef5350'
    const colorTP1 = '#26a69a'
    const colorTP2 = '#00bcd4'
    const labelEntry = isLong ? '做多' : '做空'

    // 固定标注宽度：放在右侧价格轴左边，向左延伸
    const MARK_W = 140       // 区域/线条固定宽度

    const drawPosition = () => {
      while (svg.firstChild) svg.removeChild(svg.firstChild)
      const fullWidth = containerRef.current?.clientWidth || svg.clientWidth || 520
      const height = containerRef.current?.clientHeight || CHART_HEIGHT
      // K 线主区域右边界 = 总宽 - 右侧价格轴宽度
      const priceScaleWidth = chart.priceScale('right').width()
      const rightEdge = Math.max(MARK_W, fullWidth - priceScaleWidth)
      const x0 = rightEdge - MARK_W  // 线条左端
      const x1 = rightEdge           // 线条右端（紧贴价格轴）
      svg.setAttribute('width', String(fullWidth))
      svg.setAttribute('height', String(height))
      svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

      if (ai.entry_price == null) return
      const yEntry = series.priceToCoordinate(ai.entry_price)
      if (yEntry == null) return

      // 先画区域色块（固定宽度，右侧）
      // 止损区：入场 → 止损（红色半透明）
      if (ai.stop_loss != null) {
        const ySL = series.priceToCoordinate(ai.stop_loss)
        if (ySL != null) {
          const top = Math.min(yEntry, ySL)
          const h = Math.abs(ySL - yEntry)
          const r = el('rect')
          r.setAttribute('x', String(x0))
          r.setAttribute('y', String(top))
          r.setAttribute('width', String(MARK_W))
          r.setAttribute('height', String(h))
          r.setAttribute('fill', 'rgba(239, 83, 80, 0.15)')
          svg.appendChild(r)
        }
      }
      // 止盈1区：入场 → 止盈1（绿色半透明）
      if (ai.take_profit_1 != null) {
        const yTP1 = series.priceToCoordinate(ai.take_profit_1)
        if (yTP1 != null) {
          const top = Math.min(yEntry, yTP1)
          const h = Math.abs(yTP1 - yEntry)
          const r = el('rect')
          r.setAttribute('x', String(x0))
          r.setAttribute('y', String(top))
          r.setAttribute('width', String(MARK_W))
          r.setAttribute('height', String(h))
          r.setAttribute('fill', 'rgba(38, 166, 154, 0.15)')
          svg.appendChild(r)
        }
      }
      // 止盈2区：止盈1 → 止盈2（更浅绿）
      if (ai.take_profit_1 != null && ai.take_profit_2 != null) {
        const yTP1 = series.priceToCoordinate(ai.take_profit_1)
        const yTP2 = series.priceToCoordinate(ai.take_profit_2)
        if (yTP1 != null && yTP2 != null) {
          const top = Math.min(yTP1, yTP2)
          const h = Math.abs(yTP2 - yTP1)
          const r = el('rect')
          r.setAttribute('x', String(x0))
          r.setAttribute('y', String(top))
          r.setAttribute('width', String(MARK_W))
          r.setAttribute('height', String(h))
          r.setAttribute('fill', 'rgba(38, 166, 154, 0.08)')
          svg.appendChild(r)
        }
      }

      // 画水平线 + 右侧标签
      const drawLine = (
        y: number,
        color: string,
        label: string,
        price: number,
        dashed: boolean
      ) => {
        // 线（固定宽度，从左到右到价格轴左边界）
        const line = el('line')
        line.setAttribute('x1', String(x0))
        line.setAttribute('y1', String(y))
        line.setAttribute('x2', String(x1))
        line.setAttribute('y2', String(y))
        line.setAttribute('stroke', color)
        line.setAttribute('stroke-width', '1')
        line.setAttribute('shape-rendering', 'crispEdges')
        if (dashed) line.setAttribute('stroke-dasharray', '5,3')
        svg.appendChild(line)

        // 右侧标签盒
        const text = `${label} ${fmt(price)}`
        const font = 'bold 10px monospace'
        const ctx2 = document.createElement('canvas').getContext('2d')!
        ctx2.font = font
        const tw = ctx2.measureText(text).width
        const padX = 4
        const boxH = 13
        const boxW = tw + padX * 2
        const boxX = x1 - boxW  // 右对齐到线条右端

        const rect = el('rect')
        rect.setAttribute('x', String(boxX))
        rect.setAttribute('y', String(y - boxH / 2))
        rect.setAttribute('width', String(boxW))
        rect.setAttribute('height', String(boxH))
        rect.setAttribute('fill', color)
        svg.appendChild(rect)

        const t = el('text')
        t.setAttribute('x', String(boxX + padX))
        t.setAttribute('y', String(y + 3))
        t.setAttribute('fill', '#fff')
        t.setAttribute('font-size', '10')
        t.setAttribute('font-family', 'monospace')
        t.setAttribute('font-weight', 'bold')
        t.textContent = text
        svg.appendChild(t)
      }

      // 入场线（实线，带方向标签）
      drawLine(yEntry, colorEntry, labelEntry, ai.entry_price, false)
      // 止损线（虚线）
      if (ai.stop_loss != null) {
        const ySL = series.priceToCoordinate(ai.stop_loss)
        if (ySL != null) drawLine(ySL, colorSL, '止损', ai.stop_loss, true)
      }
      // 止盈1线（虚线）
      if (ai.take_profit_1 != null) {
        const yTP1 = series.priceToCoordinate(ai.take_profit_1)
        if (yTP1 != null) drawLine(yTP1, colorTP1, '止盈1', ai.take_profit_1, true)
      }
      // 止盈2线（虚线）
      if (ai.take_profit_2 != null) {
        const yTP2 = series.priceToCoordinate(ai.take_profit_2)
        if (yTP2 != null) drawLine(yTP2, colorTP2, '止盈2', ai.take_profit_2, true)
      }
    }

    redrawFnRef.current = drawPosition
    requestAnimationFrame(() => drawPosition())

    // 用 rAF 循环持续重绘，确保价格轴缩放/时间轴缩放/平移时区域都能同步
    let rafId = 0
    const loop = () => {
      drawPosition()
      rafId = requestAnimationFrame(loop)
    }
    rafId = requestAnimationFrame(loop)

    return () => {
      cancelAnimationFrame(rafId)
    }
  }, [ai])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 360 }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
      <svg
        ref={svgRef}
        width="100%"
        height="100%"
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
    </div>
  )
}
