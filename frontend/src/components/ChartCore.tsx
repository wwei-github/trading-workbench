/**
 * K 线图核心组件（2026-09-27 统一组件）
 * - 全站唯一 K 线图组件 KlineChart（symbol 扫描 / tradeId 复盘双数据源）共用：
 *   图表生命周期 / 摆动标注 / 技术指标（目录驱动）/
 *   工具栏（指标下拉 + 更新按钮）/ OHLC 图例 / AI 仓位标注 / SVG 覆盖层
 * - 差异通过 props 注入：复盘图传 drawOverlay（区域色块）+ extraMarkers
 *   （事件箭头）+ headerExtra（复盘信息面板）；数据拉取由父组件负责
 */
import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Button, Checkbox, Dropdown, Input, InputNumber, Popconfirm, Radio, Tooltip, Typography } from 'antd'
import {
  ClearOutlined,
  DeleteOutlined,
  FallOutlined,
  FontSizeOutlined,
  FormOutlined,
  MessageOutlined,
  PlusOutlined,
  RiseOutlined,
  SyncOutlined,
  TableOutlined,
  TagOutlined,
} from '@ant-design/icons'
import { DrawingManager, type Drawing, type DrawingKind, type MagnetMode, type TableCellRef } from 'lightweight-charts-drawing'
import {
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesType,
  type Time,
  type SeriesMarker,
  type UTCTimestamp,
  type IPriceLine,
  type MouseEventParams,
  type CandlestickData,
} from 'lightweight-charts'
import {
  applyCandleScheme,
  applyCorridorView,
  CANDLE_COLORS,
  createTradeChart,
  fmtPrice,
  fmtTime,
  fmtVol,
  GLASS,
} from './klineShared'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import {
  INDICATOR_CATALOG,
  type IndicatorContext,
  type IndicatorResult,
} from '../constants/indicators'
import type { AIAnalysis, Kline, KlineData } from '../types'

const { Text } = Typography

// 图表数据（KlineData / TradeKlineData 的图表相关子集，两者 klines 同为 Kline[]）
export interface ChartCoreData {
  klines: Kline[]
  swings?: KlineData['swings']
}

// SVG 覆盖层绘制上下文（AI 仓位标注与自定义覆盖层共用）
export interface OverlayCtx {
  chart: IChartApi
  series: ISeriesApi<'Candlestick'>
  svg: SVGSVGElement
  container: HTMLDivElement
}

interface Props {
  // 变化时重建图表（币种 / 交易ID）；数据拉取与清空由父组件负责
  chartKey: string
  // 主图中央水印（扫描图=币种；复盘图=币种+方向）
  watermark: string
  // 图例标题（同 chartKey 场景下一般与水印同源）
  label: string
  data: ChartCoreData | null
  // 可视起点逻辑下标：缺省=最近 130 根；复盘图传 -2（左侧留白看全程）
  from?: number
  // AI 仓位标注（入场/止损/止盈线+区域，扫描结果图用）
  ai?: AIAnalysis
  // 额外 K 线标记（复盘图事件箭头）；需父组件 useMemo 保持引用稳定
  extraMarkers?: SeriesMarker<UTCTimestamp>[]
  // 自定义 SVG 覆盖层（复盘图区域色块/风控线），每帧在空白画布上调用
  drawOverlay?: (ctx: OverlayCtx) => void
  // 左上角堆叠里的额外面板（复盘图信息卡：区间/三价摘要），渲染在图例下方
  headerExtra?: ReactNode
  // 提供则显示「更新」按钮（父组件负责重拉数据）
  onUpdate?: () => void
  updating?: boolean
  // 周期切换（2026-09-28 扫描图用；2026-10-08 复盘图也用）：提供 onIntervalChange
  // 则显示切换按钮组，值变化由父组件重拉数据；档位默认 1h/4h，可用
  // intervalOptions 定制（复盘图需含服务端默认档，cfg.kline_interval 可配非 1h/4h）
  interval?: string
  onIntervalChange?: (v: string) => void
  intervalOptions?: { label: string; value: string }[]
  // 容器无高度时的兜底高度
  height: number
}

const CHART_HEIGHT_FALLBACK = 560

// ── 画线工具（docs/18：lightweight-charts-drawing 0.2.4，TV 风格手绘标注）──
// 四组：几何 / 斐波·测量 / 文本·标注 / 仓位预演（组间分隔线）；不追求 86 种全集
const DRAW_TOOLS: { kind: DrawingKind; label: string; tip: string; icon?: ReactNode; sep?: boolean }[] = [
  // 几何
  { kind: 'trend-line', label: '╱', tip: '趋势线' },
  { kind: 'horizontal-line', label: '─', tip: '水平线' },
  { kind: 'horizontal-ray', label: '→', tip: '水平射线' },
  { kind: 'vertical-line', label: '│', tip: '垂直线' },
  { kind: 'rectangle', label: '▭', tip: '矩形' },
  { kind: 'parallel-channel', label: '⫽', tip: '平行通道' },
  { kind: 'brush', label: '✎', tip: '画笔' },
  // 斐波 / 测量
  { kind: 'fib-retracement', label: '₣', tip: '斐波那契回调', sep: true },
  { kind: 'trend-based-fib-extension', label: '₣+', tip: '斐波那契扩展（三点：目标位投射）' },
  { kind: 'price-range', label: '↔', tip: '价格区间测量（涨跌幅/价差）' },
  // 文本 / 标注
  { kind: 'text', label: '', tip: '文本', icon: <FontSizeOutlined />, sep: true },
  { kind: 'note', label: '', tip: '便签', icon: <FormOutlined /> },
  { kind: 'price-note', label: '', tip: '价格便签', icon: <TagOutlined /> },
  { kind: 'callout', label: '', tip: '气泡标注', icon: <MessageOutlined /> },
  { kind: 'arrow', label: '↗', tip: '箭头' },
  { kind: 'table', label: '', tip: '表格（点选单元格编辑）', icon: <TableOutlined /> },
  // 仓位预演（1 点放置，自带入场/止损/止盈框与 RR 徽标）
  { kind: 'long-position', label: '', tip: '多单预演（入场/止损/止盈框）', icon: <RiseOutlined />, sep: true },
  { kind: 'short-position', label: '', tip: '空单预演（入场/止损/止盈框）', icon: <FallOutlined /> },
]
const MAGNET_CYCLE: MagnetMode[] = ['off', 'weak', 'strong']
const MAGNET_TIP: Record<MagnetMode, string> = { off: '磁吸：关', weak: '磁吸：弱（吸K线）', strong: '磁吸：强（吸开收高低）' }
// 画线持久化 key：按图表主体（币种/交易）分区，时间锚定跨周期有效
const drawingsLSKey = (chartKey: string) => `kline-drawings:${chartKey}`

// 文本/表格宿主编辑器状态（textEdit/tableEdit 事件打开；pane 坐标定位）
interface InlineEditor {
  mode: 'text' | 'table'
  id: string
  x: number
  y: number
  w: number
  h: number
  value: string
  // text 模式：被编辑的完整 drawing（update 用）
  drawing?: Drawing
  // table 模式：被编辑的单元格 [row, col]
  cell?: TableCellRef
  // 放置流（文本为空提交 → 移除，TV 行为）；编辑流空提交 = 清空文本
  isNew: boolean
}

// 从序列尾部找最后一个有效值（指标前段常有空值）
const lastFinite = (values: (number | null)[]): number | null => {
  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i]
    if (v != null && Number.isFinite(v)) return v
  }
  return null
}

// 图例中的一条指标线（悬浮时取悬浮位置的值，未悬浮取最新值）
interface LegendIndicator {
  name: string
  color: string
  value: number | null
}

interface LegendData {
  time: number
  o: number
  h: number
  l: number
  c: number
  v: number | null
  chg: number
  inds: LegendIndicator[]
}

// AI 仓位价格线 + 区域色块（TradingView 仓位标注风格，从 KlineChart 原样迁入）
function drawAiPosition(
  ctx: OverlayCtx, ai: AIAnalysis | undefined, label: string, colorScheme: ColorScheme,
) {
  const { chart, series, svg, container } = ctx
  // 无 AI 建议或 AI 属于其他币种：不画（SVG 已被调用方清空）
  if (!ai || ai.symbol !== label) return

  const NS = 'http://www.w3.org/2000/svg'
  const el = (tag: string) => document.createElementNS(NS, tag)

  const isLong = ai.direction !== 'short'

  // 颜色配置（跟随全局红涨绿跌/绿涨红跌方案：止盈=涨色、止损=跌色、入场=方向色）
  const { up, down } = SCHEME_UP_DOWN[colorScheme]
  const colorEntry = isLong ? up : down
  const colorSL = down
  const colorTP1 = up
  const colorTP2 = up
  const labelEntry = isLong ? '做多' : '做空'

  // 固定标注宽度：放在右侧价格轴左边，向左延伸
  const MARK_W = 140
  const fullWidth = container.clientWidth || 520
  const height = container.clientHeight || CHART_HEIGHT_FALLBACK
  const priceScaleWidth = chart.priceScale('right').width()
  const rightEdge = Math.max(MARK_W, fullWidth - priceScaleWidth)
  const x0 = rightEdge - MARK_W // 线条左端
  const x1 = rightEdge // 线条右端（紧贴价格轴）
  svg.setAttribute('width', String(fullWidth))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${fullWidth} ${height}`)

  if (ai.entry_price == null) return
  const yEntry = series.priceToCoordinate(ai.entry_price)
  if (yEntry == null) return

  // 先画区域色块（固定宽度，右侧）
  const zone = (p1: number, p2: number, fill: string) => {
    const y1 = series.priceToCoordinate(p1)
    const y2 = series.priceToCoordinate(p2)
    if (y1 == null || y2 == null) return
    const r = el('rect')
    r.setAttribute('x', String(x0))
    r.setAttribute('y', String(Math.min(y1, y2)))
    r.setAttribute('width', String(MARK_W))
    r.setAttribute('height', String(Math.abs(y2 - y1)))
    r.setAttribute('fill', fill)
    svg.appendChild(r)
  }
  if (ai.stop_loss != null) zone(ai.entry_price, ai.stop_loss, 'rgba(239, 83, 80, 0.15)')
  if (ai.take_profit_1 != null) zone(ai.entry_price, ai.take_profit_1, 'rgba(38, 166, 154, 0.15)')
  // 止盈2区：tp2 为 0/null（未设止盈二）时不绘制
  const tp2 = ai.take_profit_2
  const hasTP2 = tp2 != null && tp2 > 0
  if (ai.take_profit_1 != null && hasTP2) zone(ai.take_profit_1, tp2!, 'rgba(38, 166, 154, 0.08)')

  // 画水平线 + 右侧标签（text 已含价格，如「止损 99.1234」）
  const drawLine = (y: number, color: string, text: string, dashed: boolean) => {
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

    const font = 'bold 10px monospace'
    const ctx2 = document.createElement('canvas').getContext('2d')!
    ctx2.font = font
    const tw = ctx2.measureText(text).width
    const padX = 4
    const boxH = 13
    const boxW = tw + padX * 2
    const boxX = x1 - boxW // 右对齐到线条右端

    const rect = el('rect')
    rect.setAttribute('x', String(boxX))
    rect.setAttribute('y', String(y - boxH / 2))
    rect.setAttribute('width', String(boxW))
    rect.setAttribute('height', String(boxH))
    rect.setAttribute('fill', color)
    svg.appendChild(rect)

    const txt = el('text')
    txt.setAttribute('x', String(boxX + padX))
    txt.setAttribute('y', String(y + 3))
    txt.setAttribute('fill', '#fff')
    txt.setAttribute('font-size', '10')
    txt.setAttribute('font-family', 'monospace')
    txt.setAttribute('font-weight', 'bold')
    txt.textContent = text
    svg.appendChild(txt)
  }

  // 入场线（实线，带方向标签）
  drawLine(yEntry, colorEntry, `${labelEntry} ${fmtPrice(ai.entry_price)}`, false)
  // 止损线（虚线）
  if (ai.stop_loss != null) {
    const ySL = series.priceToCoordinate(ai.stop_loss)
    if (ySL != null) drawLine(ySL, colorSL, `止损 ${fmtPrice(ai.stop_loss)}`, true)
  }
  // 止盈1线（虚线）
  if (ai.take_profit_1 != null) {
    const yTP1 = series.priceToCoordinate(ai.take_profit_1)
    if (yTP1 != null) drawLine(yTP1, colorTP1, `止盈1 ${fmtPrice(ai.take_profit_1)}`, true)
  }
  // 止盈2线（虚线；tp2 为 0/null 时不绘制）
  if (hasTP2) {
    const yTP2 = series.priceToCoordinate(tp2!)
    if (yTP2 != null) drawLine(yTP2, colorTP2, `止盈2 ${fmtPrice(tp2!)}`, true)
  }
}

export default function ChartCore({
  chartKey, watermark, label, data, from,
  ai, extraMarkers, drawOverlay, headerExtra, onUpdate, updating,
  interval, onIntervalChange, intervalOptions, height,
}: Props) {
  // 全局涨跌配色（切换入口在页面顶部）/ 技术指标（store 共享，切换后所有图表同步生效）
  const colorScheme = useScanStore((s) => s.colorScheme)
  const indicatorSettings = useScanStore((s) => s.indicatorSettings)
  const toggleIndicator = useScanStore((s) => s.toggleIndicator)
  const setIndicatorParams = useScanStore((s) => s.setIndicatorParams)
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // 技术指标 series + 图例元信息（目录驱动），effect 重建时统一清理
  const indicatorSeriesRef = useRef<ISeriesApi<SeriesType>[]>([])
  const indicatorMetaRef = useRef<
    { series: ISeriesApi<SeriesType>; name: string; color: string; lastValue: number | null }[]
  >([])
  // 摆动结构标注插件实例（更新数据时用 setMarkers 原位替换，不重复创建）
  const markersApiRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  // 画线工具管理器（docs/18）：生命周期与图表实例一致，chartKey 变化销毁重建
  const dmRef = useRef<DrawingManager | null>(null)
  const [activeTool, setActiveTool] = useState<DrawingKind | null>(null)
  const [magnet, setMagnet] = useState<MagnetMode>('off')
  // 文本/表格内联编辑器 + 提交防抖守卫（Enter 提交后失焦不再二次提交）
  const [editor, setEditor] = useState<InlineEditor | null>(null)
  const editorBusyRef = useRef(false)
  // 已加载的 K 线（完整 OHLCV），供指标计算与标注使用
  const [candlePoints, setCandlePoints] = useState<
    { time: UTCTimestamp; open: number; high: number; low: number; close: number; volume: number }[]
  >([])
  // 图例数据：null 表示未悬浮 → 显示最新一根 K 线
  const [legend, setLegend] = useState<LegendData | null>(null)

  // 初始化图表（chartKey 变化重建；水印/兜底高度随用法固定，不入依赖）
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    // 统一图表内核（klineShared）：配置/水印/涨跌配色全局同源
    const { chart, series } = createTradeChart(container, {
      watermark,
      height,
    })
    chartRef.current = chart
    seriesRef.current = series

    // 画线工具管理器（挂在主 series 上的 primitive，与 SVG 业务覆盖层分层共存）；
    // 持久化：localStorage 按图表主体分区，add/update/remove/import/clear 统一走 change 事件
    const dm = new DrawingManager(chart, series)
    dmRef.current = dm
    try {
      const saved = localStorage.getItem(drawingsLSKey(chartKey))
      if (saved) dm.importJSON(saved)
    } catch {
      /* 损坏的存档直接忽略，画线从空白开始 */
    }
    dm.on('change', () => {
      try {
        localStorage.setItem(drawingsLSKey(chartKey), dm.exportJSON())
      } catch {
        /* 存储满/隐私模式：画线仅本次会话有效 */
      }
    })
    // 工具布防状态同步（放置后插件自动解除布防，'tool' 事件回传 null）
    dm.on('tool', (kind) => setActiveTool(kind))
    // 文本族放置/双击 → 打开宿主编辑器（pane 坐标）；提交走 commitEditor
    dm.on('textEdit', (d, screen) => {
      editorBusyRef.current = false
      setEditor({
        mode: 'text',
        id: d.id,
        drawing: d,
        x: screen.x,
        y: screen.y,
        w: 240,
        h: 0,
        value: d.text ?? '',
        isNew: !d.text,
      })
    })
    // 表格单元格点击（选中后）→ 打开单元格编辑器；texts 存于 style.tableCells
    dm.on('tableEdit', (d, cell, box) => {
      editorBusyRef.current = false
      const cells = (d.style as { tableCells?: string[][] }).tableCells ?? []
      setEditor({
        mode: 'table',
        id: d.id,
        cell,
        x: box.left,
        y: box.top,
        w: box.width,
        h: box.height,
        value: cells[cell[0]]?.[cell[1]] ?? '',
        isNew: false,
      })
    })

    // 图例联动：十字线悬浮时显示该根 K 线的 OHLC/成交量 + 各指标线在该位置的值
    const handleCrosshair = (param: MouseEventParams) => {
      if (!param.time || !param.point) {
        setLegend(null)
        return
      }
      const cd = param.seriesData.get(series) as CandlestickData | undefined
      if (!cd) {
        setLegend(null)
        return
      }
      const volMeta = indicatorMetaRef.current.find((m) => m.name === 'VOL')
      let v: number | null = null
      if (volMeta) {
        const vd = param.seriesData.get(volMeta.series) as { value: number } | undefined
        if (vd) v = vd.value
      }
      setLegend({
        time: param.time as number,
        o: cd.open,
        h: cd.high,
        l: cd.low,
        c: cd.close,
        v,
        chg: cd.open !== 0 ? ((cd.close - cd.open) / cd.open) * 100 : 0,
        inds: indicatorMetaRef.current
          .filter((m) => m.name !== 'VOL')
          .map((m) => {
            const d = param.seriesData.get(m.series) as { value: number } | undefined
            return { name: m.name, color: m.color, value: d ? d.value : null }
          }),
      })
    }
    chart.subscribeCrosshairMove(handleCrosshair)

    // 自适应容器宽高
    const resize = () => {
      if (container && chartRef.current) {
        chartRef.current.applyOptions({
          width: container.clientWidth,
          height: container.clientHeight || height,
        })
      }
    }
    const ro = new ResizeObserver(resize)
    ro.observe(container)

    return () => {
      ro.disconnect()
      chart.unsubscribeCrosshairMove(handleCrosshair)
      dmRef.current?.destroy()
      dmRef.current = null
      setActiveTool(null)
      setEditor(null)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      markersApiRef.current = null
      setLegend(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartKey])

  // 切换涨跌配色：直接改 series 选项，所有图表实例同步生效，无需重建图表
  useEffect(() => {
    if (seriesRef.current) applyCandleScheme(seriesRef.current, colorScheme)
  }, [colorScheme])

  // 应用 K 线数据：主图 series + 可视范围 + 摆动/事件标记
  useEffect(() => {
    const series = seriesRef.current
    const chart = chartRef.current
    if (!series || !chart || !data || data.klines.length === 0) return

    const candleData = data.klines.map((k: Kline) => ({
      time: Math.floor(k.time / 1000) as UTCTimestamp,
      open: k.open,
      high: k.high,
      low: k.low,
      close: k.close,
    }))
    series.setData(candleData)
    // 缺省显示最近约 130 根（更易读），往左滚动可看全量历史
    const f = from ?? Math.max(0, candleData.length - 130)
    applyCorridorView(chart, containerRef.current?.clientWidth, candleData.length, f)
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
    setLegend(null)

    // K 线标记：摆动结构（借鉴 Pine 结构标签：高点 HH/LH，低点 HL/LL，各取 5 个）
    // + 父组件额外标记（复盘图事件箭头）
    const sw = data.swings
    const UP = '#22ab94'
    const DOWN = '#f23645'
    const markers: SeriesMarker<UTCTimestamp>[] = [
      ...(sw?.highs ?? []).map((s) => ({
        time: Math.floor(s.time / 1000) as UTCTimestamp,
        position: 'aboveBar' as const,
        shape: 'arrowDown' as const,
        color: s.label === 'LH' ? DOWN : UP,
        text: `${s.label} ${fmtPrice(s.price)}`,
        size: 1,
      })),
      ...(sw?.lows ?? []).map((s) => ({
        time: Math.floor(s.time / 1000) as UTCTimestamp,
        position: 'belowBar' as const,
        shape: 'arrowUp' as const,
        color: s.label === 'HL' ? UP : DOWN,
        text: `${s.label} ${fmtPrice(s.price)}`,
        size: 1,
      })),
      ...(extraMarkers ?? []),
    ].sort((a, b) => (a.time as number) - (b.time as number))
    if (markersApiRef.current) {
      markersApiRef.current.setMarkers(markers)
    } else {
      markersApiRef.current = createSeriesMarkers(series, markers)
    }

    // extraMarkers 由父组件 useMemo 保证引用稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, chartKey, from, extraMarkers])

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
    indicatorMetaRef.current = []
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

    // 汇总图例元信息（effect 末尾一次性写入 ref）
    const metas: typeof indicatorMetaRef.current = []

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
        metas.push({
          series: line,
          name: ln.name ?? def.label,
          color: ln.color,
          lastValue: lastFinite(ln.values),
        })
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
        // 成交量进图例（MACD/AO 柱已有线表达趋势，不重复展示）
        if (def.volumeFormat) {
          metas.push({
            series: bars,
            name: 'VOL',
            color: '#8b93a7',
            lastValue: lastFinite(out.bars.map((b) => b.value)),
          })
        }
      }
    }
    indicatorMetaRef.current = metas

    // 主图 : 副图 = 5 : 1（副图指标默认压低，给主图 K 线留更多高度；
    // 分隔线可拖动，用户可自行调整）
    chart.panes().forEach((pn, i) => pn.setStretchFactor(i === 0 ? 5 : 1))
  }, [candlePoints, indicatorSettings, colorScheme])

  // SVG 覆盖层：AI 仓位标注 + 父组件自定义绘制（复盘区域色块）。
  // 立即绘制一次 + rAF 循环重绘（缩放/平移同步；后台标签页 rAF 暂停时不空白的
  // 场景由立即绘制兜底）。坐标每帧重算，数据/配置变化无需重建循环
  const drawOverlayRef = useRef(drawOverlay)
  drawOverlayRef.current = drawOverlay
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    const svg = svgRef.current
    const container = containerRef.current
    if (!chart || !series || !svg || !container) return

    const draw = () => {
      while (svg.firstChild) svg.removeChild(svg.firstChild)
      const ctx: OverlayCtx = { chart, series, svg, container }
      drawOverlayRef.current?.(ctx)
      drawAiPosition(ctx, ai, label, colorScheme)
    }
    draw()
    let rafId = 0
    const loop = () => {
      draw()
      rafId = requestAnimationFrame(loop)
    }
    rafId = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(rafId)
  }, [ai, label, colorScheme, chartKey, data])

  // ===== 图例渲染数据：悬浮时用图例状态，否则用最新一根 K 线 =====
  const lastCandle = candlePoints[candlePoints.length - 1]
  const scheme = CANDLE_COLORS[colorScheme]
  const legendData: LegendData | null =
    legend ??
    (lastCandle
      ? {
          time: lastCandle.time as number,
          o: lastCandle.open,
          h: lastCandle.high,
          l: lastCandle.low,
          c: lastCandle.close,
          v: lastCandle.volume,
          chg:
            lastCandle.open !== 0
              ? ((lastCandle.close - lastCandle.open) / lastCandle.open) * 100
              : 0,
          inds: indicatorMetaRef.current
            .filter((m) => m.name !== 'VOL')
            .map((m) => ({ name: m.name, color: m.color, value: m.lastValue })),
        }
      : null)
  // 悬浮时成交量取悬浮值；未悬浮时固定显示最新成交量（VOL 指标关闭则隐藏）
  const legendVol = legend ? legend.v : lastCandle && indicatorMetaRef.current.some((m) => m.name === 'VOL') ? lastCandle.volume : null

  // ── 画线工具交互 ──
  const pickTool = (kind: DrawingKind) => {
    const dm = dmRef.current
    if (!dm) return
    const next = activeTool === kind ? null : kind // 再点一次取消布防
    dm.setTool(next)
    setActiveTool(next)
  }
  const cycleMagnet = () => {
    const dm = dmRef.current
    if (!dm) return
    const next = MAGNET_CYCLE[(MAGNET_CYCLE.indexOf(magnet) + 1) % MAGNET_CYCLE.length]
    dm.setMagnet(next)
    setMagnet(next)
  }
  const deleteSelected = () => {
    const dm = dmRef.current
    if (!dm) return
    for (const id of dm.selection()) dm.remove(id)
  }
  const clearDrawings = () => dmRef.current?.clear()

  // ── 文本/表格内联编辑器提交 ──
  // 文本：Enter（无 Shift）/失焦提交；空文本 + 放置流 → 移除（TV 行为），
  //       空文本 + 编辑流 → 清空（渲染占位）；Esc 取消（新画的空文本同样移除）
  const commitText = (value: string) => {
    const dm = dmRef.current
    if (editorBusyRef.current || !dm || !editor) return
    editorBusyRef.current = true
    if (!value.trim()) {
      dm.remove(editor.id)
    } else if (editor.drawing) {
      dm.update({ ...editor.drawing, text: value } as Drawing)
    }
    setEditor(null)
  }
  // 表格：单元格文本写回 + 行列操作后都要 endTableEdit 关闭插件编辑态
  const commitCell = (value: string) => {
    const dm = dmRef.current
    if (editorBusyRef.current || !dm || !editor?.cell) return
    editorBusyRef.current = true
    dm.setTableCellText(editor.id, editor.cell, value)
    dm.endTableEdit()
    setEditor(null)
  }
  const tableOp = (op: 'insert-row' | 'remove-row' | 'insert-column' | 'remove-column') => {
    const dm = dmRef.current
    if (!dm || !editor) return
    dm.tableOp(editor.id, op)
    dm.endTableEdit()
    setEditor(null)
  }
  const cancelEditor = () => {
    const dm = dmRef.current
    if (editorBusyRef.current) return
    editorBusyRef.current = true
    if (editor?.mode === 'table') dm?.endTableEdit()
    // 放置流 Esc：未输入文本的画线移除（TV 行为）
    if (editor?.mode === 'text' && editor.isNew && !editor.value.trim()) dm?.remove(editor.id)
    setEditor(null)
  }

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 360 }}>
      {/* 左上角悬浮层：工具栏 + 额表面板 + 图例（垂直堆叠，自适应换行） */}
      <div
        style={{
          position: 'absolute',
          top: 8,
          left: 8,
          zIndex: 20,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'flex-start',
          gap: 4,
          maxWidth: 'calc(100% - 16px)',
        }}>
        {/* 工具栏：指标选择 + 更新按钮 */}
        <div
          style={{
            ...GLASS,
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            gap: 4,
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
          {/* 画线工具（TV 风格手绘标注，按图表主体持久化）：点击布防、再点取消，
              Esc 取消布防，Delete 删除选中；与业务标注（三线/区域）分层共存 */}
          {DRAW_TOOLS.map((t) => (
            <Fragment key={t.kind}>
              {t.sep && <span style={{ width: 1, height: 16, background: '#2a2e39', margin: '0 2px' }} />}
              <Tooltip title={t.tip}>
                <Button
                  size="small"
                  ghost
                  type={activeTool === t.kind ? 'primary' : 'default'}
                  style={{ minWidth: 26, padding: '0 6px', fontWeight: 600 }}
                  onClick={() => pickTool(t.kind)}>
                  {t.icon || t.label}
                </Button>
              </Tooltip>
            </Fragment>
          ))}
          <Tooltip title={MAGNET_TIP[magnet]}>
            <Button
              size="small"
              ghost
              type={magnet !== 'off' ? 'primary' : 'default'}
              style={{ minWidth: 26, padding: '0 6px', fontWeight: 600 }}
              onClick={cycleMagnet}>
              ⌖
            </Button>
          </Tooltip>
          <Tooltip title="删除选中画线（选中后按 Delete 亦可）">
            <Button size="small" ghost icon={<DeleteOutlined />} onClick={deleteSelected} />
          </Tooltip>
          <Popconfirm title="清空本图全部画线？" okText="清空" cancelText="取消" onConfirm={clearDrawings}>
            <Tooltip title="清空全部画线">
              <Button size="small" ghost icon={<ClearOutlined />} />
            </Tooltip>
          </Popconfirm>
          {onIntervalChange && (
            <Radio.Group
              size="small"
              value={interval ?? '1h'}
              onChange={(e) => onIntervalChange(e.target.value)}
              optionType="button"
              options={
                intervalOptions ?? [
                  { label: '1h', value: '1h' },
                  { label: '4h', value: '4h' },
                ]
              }
            />
          )}
          {onUpdate && (
            <Tooltip title="直连交易所拉取最新K线并重置缓存">
              <Button
                size="small"
                ghost
                icon={<SyncOutlined spin={updating} />}
                loading={updating}
                onClick={onUpdate}>
                更新
              </Button>
            </Tooltip>
          )}
        </div>
        {/* 图例：OHLC + 涨跌幅 + 成交量 + 指标数值（十字线联动，纯展示不挡操作） */}
        {legendData && (
          <div
            style={{
              ...GLASS,
              padding: '4px 10px',
              fontSize: 11,
              lineHeight: '18px',
              pointerEvents: 'none',
              maxWidth: '100%',
              display: 'flex',
              flexWrap: 'wrap',
              columnGap: 10,
              rowGap: 1,
            }}>
            <span style={{ color: '#e8ebf2', fontWeight: 600 }}>{label}</span>
            <span style={{ color: '#8b93a7' }}>{fmtTime(legendData.time)}</span>
            {(
              [
                ['开', legendData.o],
                ['高', legendData.h],
                ['低', legendData.l],
                ['收', legendData.c],
              ] as [string, number][]
            ).map(([l, val]) => (
              <span key={l} style={{ color: '#8b93a7' }}>
                {l}{' '}
                <span style={{ color: legendData.chg >= 0 ? scheme.up : scheme.down }}>
                  {fmtPrice(val)}
                </span>
              </span>
            ))}
            <span
              style={{
                color: '#fff',
                background: legendData.chg >= 0 ? scheme.up : scheme.down,
                borderRadius: 3,
                padding: '0 5px',
                fontWeight: 600,
              }}>
              {legendData.chg >= 0 ? '+' : ''}
              {legendData.chg.toFixed(2)}%
            </span>
            {legendVol != null && (
              <span style={{ color: '#8b93a7' }}>
                量 <span style={{ color: '#c9cfdd' }}>{fmtVol(legendVol)}</span>
              </span>
            )}
            {/* 指标数值：色点 + 短名 + 当前值 */}
            {legendData.inds.map((ind, i) => (
              <span key={`${ind.name}-${i}`} style={{ color: '#8b93a7' }}>
                <span style={{ color: ind.color }}>●</span> {ind.name}{' '}
                <span style={{ color: ind.color }}>
                  {ind.value != null && Number.isFinite(ind.value) ? fmtPrice(ind.value) : '—'}
                </span>
              </span>
            ))}
          </div>
        )}
        {/* 额外面板（复盘图信息卡），渲染在图例下方 */}
        {headerExtra}
      </div>
      <div
        ref={containerRef}
        style={{
          width: '100%',
          height: '100%',
          borderRadius: 8,
          overflow: 'hidden',
          border: '1px solid #232838',
        }}
      />
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
      {/* 文本/表格内联编辑器（pane 坐标定位，clamp 在图内；置于业务覆盖层之上） */}
      {editor && (
        <div
          style={{
            position: 'absolute',
            left: Math.max(0, Math.min(editor.x, (containerRef.current?.clientWidth ?? 600) - 260)),
            top: Math.max(0, Math.min(editor.y, (containerRef.current?.clientHeight ?? 400) - 60)),
            zIndex: 30,
          }}>
          {editor.mode === 'text' ? (
            <Input.TextArea
              autoFocus
              defaultValue={editor.value}
              placeholder="输入文本，Esc 取消"
              autoSize={{ minRows: 1, maxRows: 6 }}
              style={{
                width: 240,
                background: '#1b1f2a',
                color: '#d1d4dc',
                fontSize: 12,
                borderColor: '#2a2e39',
                boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
              }}
              onPressEnter={(e) => {
                if (e.shiftKey) return // Shift+Enter 换行不提交
                commitText((e.target as HTMLTextAreaElement).value)
              }}
              onBlur={(e) => commitText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  e.preventDefault()
                  cancelEditor()
                }
              }}
            />
          ) : (
            <div>
              <Input
                autoFocus
                defaultValue={editor.value}
                size="small"
                style={{
                  width: Math.max(120, editor.w),
                  background: '#1b1f2a',
                  color: '#d1d4dc',
                  fontSize: 12,
                  borderColor: '#2a2e39',
                }}
                onPressEnter={(e) => commitCell((e.target as HTMLInputElement).value)}
                onBlur={(e) => commitCell(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.stopPropagation()
                    e.preventDefault()
                    cancelEditor()
                  }
                }}
              />
              <div style={{ display: 'flex', gap: 2, marginTop: 2 }}>
                {(
                  [
                    ['insert-row', '+行'],
                    ['remove-row', '−行'],
                    ['insert-column', '+列'],
                    ['remove-column', '−列'],
                  ] as const
                ).map(([op, label]) => (
                  <Button
                    key={op}
                    size="small"
                    ghost
                    style={{ fontSize: 11, padding: '0 5px' }}
                    // 阻断 mousedown 默认行为防 input 失焦先提交（编辑器卸载吞掉本 click）
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => tableOp(op)}>
                    {label}
                  </Button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
