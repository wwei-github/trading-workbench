import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Dropdown, InputNumber, Segmented, Tag, Typography } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type SeriesType,
  type UTCTimestamp,
  type IPriceLine,
} from 'lightweight-charts'
import { scanApi } from '../api/scan'
import {
  useScanStore,
  type ColorScheme,
} from '../stores/scanStore'
import {
  INDICATOR_CATALOG,
  type IndicatorContext,
  type IndicatorResult,
} from '../constants/indicators'
import type { AIAnalysis, Kline, KeyLevel } from '../types'

const { Text } = Typography

interface Props {
  symbol: string
  limit?: number
  ai?: AIAnalysis
  keyLevels?: KeyLevel[]
  // 变化时强制重新拉取 K 线（关注列表手动刷新用）
  refreshKey?: number
}

const CHART_HEIGHT = 560 // 容器无高度时的兜底值

// 关键位 kind → 中文标签（图表线条/勾选开关共用）
const KIND_LABEL: Record<string, string> = {
  prev_high: '前高',
  prev_low: '前低',
  support: '支撑',
  resistance: '压力',
  range_top: '区间顶',
  range_bottom: '区间底',
}

// 每个角色（支撑/压力）最多显示的关键位条数：只画距当前价最近的
const MAX_LINES_PER_ROLE = 2

// 涨跌配色方案：红涨绿跌（国内习惯，默认）/ 绿涨红跌（国际习惯）
// 仅作用于 K 线实体；关键位/AI 仓位线保持语义色不变
const CANDLE_COLORS: Record<ColorScheme, { up: string; down: string }> = {
  'red-up': { up: '#ef5350', down: '#26a69a' },
  'green-up': { up: '#26a69a', down: '#ef5350' },
}

export default function KlineChart({ symbol, limit = 500, ai, keyLevels, refreshKey = 0 }: Props) {
  // 全局涨跌配色 / 技术指标（store 共享，切换后所有图表同步生效）
  const colorScheme = useScanStore((s) => s.colorScheme)
  const setColorScheme = useScanStore((s) => s.setColorScheme)
  const indicatorSettings = useScanStore((s) => s.indicatorSettings)
  const toggleIndicator = useScanStore((s) => s.toggleIndicator)
  const setIndicatorParams = useScanStore((s) => s.setIndicatorParams)
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const priceLinesRef = useRef<IPriceLine[]>([])
  const keyLineLinesRef = useRef<IPriceLine[]>([])
  // 技术指标 series（目录驱动），effect 重建时统一清理
  const indicatorSeriesRef = useRef<ISeriesApi<SeriesType>[]>([])
  const redrawFnRef = useRef<(() => void) | null>(null)
  const [error, setError] = useState<string | null>(null)
  // K 线最新收盘价（作为"当前价"，用于挑选最近的关键位）
  const [lastClose, setLastClose] = useState<number | null>(null)
  // 隐藏的关键位类型（勾选开关）
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set())
  // 已加载的 K 线（完整 OHLCV），供指标计算与标注使用
  const [candlePoints, setCandlePoints] = useState<
    { time: UTCTimestamp; open: number; high: number; low: number; close: number; volume: number }[]
  >([])

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

    const candleColors = CANDLE_COLORS[useScanStore.getState().colorScheme]
    const series = chart.addSeries(CandlestickSeries, {
      upColor: candleColors.up,
      downColor: candleColors.down,
      borderVisible: false,
      wickUpColor: candleColors.up,
      wickDownColor: candleColors.down,
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
        setLastClose(candleData[candleData.length - 1]?.close ?? null)
        // 直接从原始数据取完整 OHLCV（candleData 是给 series 用的精简结构）
        setCandlePoints(
          data.klines.map((k: Kline) => ({
            time: Math.floor(k.time / 1000) as UTCTimestamp,
            open: k.open,
            high: k.high,
            low: k.low,
            close: k.close,
            volume: k.volume,
          })),
        )
        setError(null)
        // 数据加载完成后触发 AI 仓位标注重绘（等待布局完成）
        requestAnimationFrame(() => redrawFnRef.current?.())
      })
      .catch((e) => {
        // 展示后端 503/4xx 的 detail（如交易所封禁时长）
        setError(e?.response?.data?.detail || 'K线数据加载失败')
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
  }, [symbol, limit, refreshKey])

  // 切换涨跌配色：直接改 series 选项，所有图表实例同步生效，无需重建图表
  useEffect(() => {
    const c = CANDLE_COLORS[colorScheme]
    seriesRef.current?.applyOptions({
      upColor: c.up,
      downColor: c.down,
      wickUpColor: c.up,
      wickDownColor: c.down,
    })
  }, [colorScheme])

  // 绘制关键位水平线：只画距当前价最近的 N 条支撑 + N 条压力，可按类型勾选隐藏
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return

    // 清除旧关键位线
    keyLineLinesRef.current.forEach((l) => {
      try {
        series.removePriceLine(l)
      } catch {
        /* series 已销毁 */
      }
    })
    keyLineLinesRef.current = []

    if (!keyLevels || keyLevels.length === 0) return
    // K 线未加载完成时先不画，加载后 lastClose 变化会重跑本 effect
    if (lastClose == null) return

    // 按角色取距当前价最近的 N 条
    const pickNearest = (role: 'support' | 'resistance') =>
      keyLevels
        .filter((lv) => lv.role === role)
        .sort(
          (a, b) => Math.abs(a.price - lastClose) - Math.abs(b.price - lastClose),
        )
        .slice(0, MAX_LINES_PER_ROLE)

    const nearest = [
      ...pickNearest('support'),
      ...pickNearest('resistance'),
    ].filter((lv) => !hiddenKinds.has(lv.kind))

    for (const lv of nearest) {
      const line = series.createPriceLine({
        price: lv.price,
        color: lv.role === 'support' ? '#26a69a' : '#ef5350',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: KIND_LABEL[lv.kind] || lv.kind,
      })
      keyLineLinesRef.current.push(line)
    }

    return () => {
      const s = seriesRef.current
      if (s) {
        keyLineLinesRef.current.forEach((l) => {
          try {
            s.removePriceLine(l)
          } catch {
            /* series 已销毁 */
          }
        })
      }
      keyLineLinesRef.current = []
    }
  }, [keyLevels, hiddenKinds, lastClose, symbol, limit])

  // 渲染技术指标：目录驱动（indicatorts 计算库 + 通用渲染层），无手写指标数学
  // 主图叠加（pane 0）/ 副图（按目录顺序自动分配 pane 1,2,3…）
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || candlePoints.length === 0) return

    // 清理旧指标 series 与已空的副图 pane（pane 0 主图保留）
    indicatorSeriesRef.current.forEach((s) => {
      try {
        chart.removeSeries(s)
      } catch {
        /* 旧图表已销毁 */
      }
    })
    indicatorSeriesRef.current = []
    for (let i = chart.panes().length - 1; i >= 1; i--) {
      try {
        chart.removePane(i)
      } catch {
        /* 已不存在 */
      }
    }

    const ctx: IndicatorContext = {
      opens: candlePoints.map((c) => c.open),
      highs: candlePoints.map((c) => c.high),
      lows: candlePoints.map((c) => c.low),
      closes: candlePoints.map((c) => c.close),
      volumes: candlePoints.map((c) => c.volume),
      cc: CANDLE_COLORS[colorScheme],
    }

    // 为开启的副图指标按目录顺序分配 pane（1 起）
    const subPane: Record<string, number> = {}
    let nextPane = 1
    for (const def of INDICATOR_CATALOG) {
      if (def.pane === 'sub' && indicatorSettings[def.key]?.enabled) {
        subPane[def.key] = nextPane++
      }
    }

    const toLineData = (values: (number | null)[]) =>
      candlePoints
        .map((c, j) => ({ time: c.time, value: values[j] ?? null }))
        .filter(
          (d): d is { time: UTCTimestamp; value: number } =>
            d.value != null && Number.isFinite(d.value),
        )

    for (const def of INDICATOR_CATALOG) {
      const setting = indicatorSettings[def.key]
      if (!setting?.enabled) continue

      let out: IndicatorResult
      try {
        out = def.compute(ctx, setting.params)
      } catch (e) {
        console.error(`指标 ${def.label} 计算失败`, e)
        continue
      }

      const paneIndex = def.pane === 'main' ? 0 : subPane[def.key]

      for (const ln of out.lines) {
        const line = chart.addSeries(
          LineSeries,
          {
            color: ln.color,
            lineWidth: 1,
            lineStyle:
              ln.lineStyle === 'dotted'
                ? LineStyle.Dotted
                : ln.lineStyle === 'dashed'
                  ? LineStyle.Dashed
                  : LineStyle.Solid,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
          },
          paneIndex,
        )
        line.setData(toLineData(ln.values))
        indicatorSeriesRef.current.push(line)
      }

      if (out.bars && out.bars.length > 0) {
        const bars = chart.addSeries(
          HistogramSeries,
          {
            priceLineVisible: false,
            lastValueVisible: false,
            ...(def.volumeFormat ? { priceFormat: { type: 'volume' as const } } : {}),
          },
          paneIndex,
        )
        bars.setData(
          out.bars
            .map((b, j) => ({ time: candlePoints[j].time, value: b.value, color: b.color }))
            .filter((d) => d.value != null && Number.isFinite(d.value)),
        )
        indicatorSeriesRef.current.push(bars)
      }
    }

    // 主图 : 副图 = 3 : 1（逐个设置拉伸比例）
    chart.panes().forEach((pn, i) => pn.setStretchFactor(i === 0 ? 3 : 1))
  }, [candlePoints, indicatorSettings, colorScheme])

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
      {error && (
        <Alert
          type="warning"
          showIcon
          message="K线数据获取失败"
          description={error}
          style={{ marginBottom: 8 }}
        />
      )}
      {/* 左上角工具栏：指标选择 + 关键位类型开关 + 涨跌配色切换 */}
      <div
        style={{
          position: 'absolute',
          top: 8,
          left: 8,
          zIndex: 20,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 4,
          maxWidth: 'calc(100% - 16px)',
          background: 'rgba(19, 23, 34, 0.78)',
          border: '1px solid #2a2e39',
          borderRadius: 6,
          padding: '4px 8px',
        }}>
        {/* 技术指标面板（indicatorts 计算，主图/副图分组，开关 + 参数，全局生效） */}
        <Dropdown
          trigger={['click']}
          destroyPopupOnHide
          dropdownRender={() => (
            <div
              style={{
                background: '#1b1f2a',
                border: '1px solid #2a2e39',
                borderRadius: 6,
                padding: '10px 12px',
                width: 250,
                maxHeight: 420,
                overflowY: 'auto',
              }}>
              {(['main', 'sub'] as const).map((pane) => (
                <div key={pane} style={pane === 'main' ? { marginBottom: 10 } : undefined}>
                  <Text style={{ color: '#9aa3b2', fontSize: 11 }}>
                    {pane === 'main' ? '主图指标' : '副图指标'}
                  </Text>
                  {INDICATOR_CATALOG.filter((d) => d.pane === pane).map((def) => {
                    const setting = indicatorSettings[def.key]
                    return (
                      <div key={def.key} style={{ marginTop: 4 }}>
                        <Checkbox
                          checked={!!setting?.enabled}
                          onChange={() => toggleIndicator(def.key)}>
                          <span style={{ color: '#d1d4dc', fontSize: 12 }}>{def.label}</span>
                        </Checkbox>
                        {setting?.enabled && def.paramLabels.length > 0 && (
                          <div
                            style={{
                              display: 'flex',
                              flexWrap: 'wrap',
                              gap: 4,
                              marginLeft: 22,
                              marginTop: 2,
                            }}>
                            {def.paramLabels.map((pl, i) => (
                              <InputNumber
                                key={i}
                                size="small"
                                min={1}
                                max={499}
                                value={setting.params[i]}
                                style={{ width: 56 }}
                                onChange={(v) => {
                                  const params = [...setting.params]
                                  params[i] = v ?? params[i]
                                  setIndicatorParams(def.key, params)
                                }}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          )}>
          <Button size="small" ghost icon={<PlusOutlined />}>
            指标
          </Button>
        </Dropdown>
        {keyLevels && keyLevels.length > 0 &&
          [...new Set(keyLevels.map((lv) => lv.kind))].map((kind) => {
            const visible = !hiddenKinds.has(kind)
            // 未选中态必须显式配色：antd 亮色主题下默认是深色文字 + 无背景，
            // 叠在深色工具栏背景上会完全看不见
            const style = visible
              ? { color: '#fff' }
              : {
                  color: '#9aa3b2',
                  background: 'rgba(255, 255, 255, 0.06)',
                  border: '1px dashed #3a3f4d',
                }
            return (
              <Tag.CheckableTag
                key={kind}
                checked={visible}
                style={style}
                onChange={(checked) =>
                  setHiddenKinds((prev) => {
                    const next = new Set(prev)
                    if (checked) next.delete(kind)
                    else next.add(kind)
                    return next
                  })
                }>
                {KIND_LABEL[kind] || kind}
              </Tag.CheckableTag>
            )
          })}
        {/* 全局涨跌配色切换（localStorage 持久化，对所有图表生效） */}
        <Segmented
          size="small"
          value={colorScheme}
          options={[
            { label: '红涨绿跌', value: 'red-up' },
            { label: '绿涨红跌', value: 'green-up' },
          ]}
          onChange={(v) => setColorScheme(v as ColorScheme)}
          style={{ marginLeft: 4 }}
        />
      </div>
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
