import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Dropdown, InputNumber, Segmented, Tag, Typography } from 'antd'
import { SettingOutlined } from '@ant-design/icons'
import {
  init,
  dispose,
  registerOverlay,
  utils,
  type Chart,
  type OverlayFigure,
  type SmoothLineStyle,
} from 'klinecharts'
import { scanApi } from '../api/scan'
import {
  useScanStore,
  INDICATOR_CATALOG,
  type ColorScheme,
  type IndicatorSetting,
} from '../stores/scanStore'
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

// 关键位 kind → 中文标签
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
// 仅作用于 K 线实体与成交量/MACD 柱；关键位/AI 仓位线保持语义色不变
const CANDLE_COLORS: Record<ColorScheme, { up: string; down: string }> = {
  'red-up': { up: '#ef5350', down: '#26a69a' },
  'green-up': { up: '#26a69a', down: '#ef5350' },
}

// 指标多线通用配色（快→慢循环）
const LINE_COLORS = ['#f0b90b', '#00bcd4', '#ff9800', '#b39ddb', '#ef5350']

const fmtPrice = (p: number) =>
  p < 1 ? p.toFixed(6) : p < 100 ? p.toFixed(4) : p.toFixed(2)

// ===== 自定义 overlay：关键位水平线（全宽虚线 + 左上标签 + 右侧价格轴标签）=====
interface KeyLevelExt {
  label: string
  color: string
  priceText: string
}

registerOverlay<KeyLevelExt>({
  name: 'keyLevelLine',
  totalStep: 2,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: false,
  needDefaultYAxisFigure: false,
  createPointFigures: ({ overlay, coordinates, bounding }) => {
    const y = coordinates[0]?.y
    if (!Number.isFinite(y)) return []
    const { label, color, priceText } = overlay.extendData
    return [
      {
        type: 'line',
        attrs: { coordinates: [{ x: bounding.left, y }, { x: bounding.right, y }] },
        styles: { color, style: 'dashed', size: 1, dashedValue: [4, 3] },
      },
      {
        type: 'text',
        attrs: { x: bounding.left + 6, y: y - 3, text: `${label} ${priceText}`, baseline: 'bottom' },
        styles: {
          color: '#fff',
          size: 10,
          family: 'monospace',
          backgroundColor: color,
          borderRadius: 2,
          paddingLeft: 4,
          paddingRight: 4,
          paddingTop: 1,
          paddingBottom: 1,
        },
      },
    ]
  },
  // 右侧价格轴上的价位标签
  createYAxisFigures: ({ overlay, coordinates, bounding }) => {
    const y = coordinates[0]?.y
    if (!Number.isFinite(y)) return []
    const { color, priceText } = overlay.extendData
    return [
      {
        type: 'text',
        attrs: { x: bounding.left + 2, y, text: priceText, align: 'left', baseline: 'middle' },
        styles: { color: '#fff', size: 10, backgroundColor: color, borderRadius: 2, paddingLeft: 2, paddingRight: 2 },
      },
    ]
  },
})

// ===== 自定义 overlay：AI 仓位标注（入场/止损/止盈线 + 区间色块 + 标签盒）=====
interface AiPosExt {
  lines: { label: string; price: number; priceText: string; color: string; dashed: boolean }[]
  zones: { from: number; to: number; color: string }[]
}

const AI_MARK_W = 140 // 线条/色块宽度（贴右侧价格轴）

registerOverlay<AiPosExt>({
  name: 'aiPosition',
  totalStep: 2,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: false,
  needDefaultYAxisFigure: false,
  createPointFigures: ({ overlay, coordinates, bounding }) => {
    const figs: OverlayFigure[] = []
    const ys = coordinates.map((c) => c?.y)
    for (const z of overlay.extendData.zones) {
      const y0 = ys[z.from]
      const y1 = ys[z.to]
      if (!Number.isFinite(y0) || !Number.isFinite(y1) || y0 === y1) continue
      figs.push({
        type: 'rect',
        attrs: { x: bounding.right - AI_MARK_W, y: Math.min(y0, y1), width: AI_MARK_W, height: Math.abs(y1 - y0) },
        styles: { style: 'fill', color: z.color },
      })
    }
    overlay.extendData.lines.forEach((ln, i) => {
      const y = ys[i]
      if (!Number.isFinite(y)) return
      figs.push({
        type: 'line',
        attrs: { coordinates: [{ x: bounding.right - AI_MARK_W, y }, { x: bounding.right, y }] },
        styles: { color: ln.color, style: ln.dashed ? 'dashed' : 'solid', size: 1, dashedValue: [5, 3] },
      })
      const text = `${ln.label} ${ln.priceText}`
      const w = utils.calcTextWidth(text, 10, 'bold', 'monospace') + 10
      figs.push({
        type: 'rect',
        attrs: { x: bounding.right - w, y: y - 8, width: w, height: 16 },
        styles: { style: 'fill', color: ln.color, borderRadius: 2 },
      })
      figs.push({
        type: 'text',
        attrs: { x: bounding.right - w + 5, y, text, align: 'left', baseline: 'middle' },
        styles: { color: '#fff', size: 10, weight: 'bold', family: 'monospace' },
      })
    })
    return figs
  },
})

// 指标线样式（klinecharts 要求完整字段）
const lineStyle = (color: string, size = 1): SmoothLineStyle => ({
  size,
  color,
  style: 'solid',
  dashedValue: [2, 2],
  smooth: false,
})

// 指标柱样式（涨跌跟随全局配色）
const barStyle = (cc: { up: string; down: string }) => ({
  style: 'fill' as const,
  upColor: cc.up,
  downColor: cc.down,
  noChangeColor: '#888888',
})

// 各指标的个性化配色（叠加在库内置样式之上）
function indicatorStyles(name: string, cc: { up: string; down: string }, params: number[]) {
  switch (name) {
    case 'EMA':
    case 'MA':
    case 'WR':
      return { lines: params.map((_, i) => lineStyle(LINE_COLORS[i % LINE_COLORS.length])) }
    case 'BOLL':
      // 中轨灰、上下轨蓝（顺序以库实现为准，中轨居中）
      return { lines: [lineStyle('#4d8ef7'), lineStyle('#d1d4dc'), lineStyle('#4d8ef7')] }
    case 'BBI':
      return { lines: [lineStyle('#ef5350'), lineStyle('#f0b90b'), lineStyle('#00bcd4'), lineStyle('#b39ddb')] }
    case 'VOL':
      return { bars: [barStyle(cc)] }
    case 'MACD':
      return { bars: [barStyle(cc)], lines: [lineStyle('#f0b90b'), lineStyle('#00bcd4')] }
    case 'KDJ':
      return { lines: [lineStyle('#f0b90b'), lineStyle('#00bcd4'), lineStyle('#ef5350')] }
    case 'RSI':
      return { lines: [lineStyle('#b39ddb'), lineStyle('#00bcd4'), lineStyle('#ff9800')] }
    case 'SAR':
      return { circles: [barStyle(cc)] }
    default:
      return { lines: params.map((_, i) => lineStyle(LINE_COLORS[i % LINE_COLORS.length])) }
  }
}

export default function KlineChart({ symbol, limit = 500, ai, keyLevels, refreshKey = 0 }: Props) {
  // 全局涨跌配色 / 技术指标（store 共享，切换后所有图表同步生效）
  const colorScheme = useScanStore((s) => s.colorScheme)
  const setColorScheme = useScanStore((s) => s.setColorScheme)
  const indicatorSettings = useScanStore((s) => s.indicatorSettings)
  const toggleIndicator = useScanStore((s) => s.toggleIndicator)
  const setIndicatorParams = useScanStore((s) => s.setIndicatorParams)

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const keyOverlayIdsRef = useRef<string[]>([])
  const aiOverlayIdRef = useRef<string | null>(null)

  const [error, setError] = useState<string | null>(null)
  // K 线最新收盘价（作为"当前价"，用于挑选最近的关键位）
  const [lastClose, setLastClose] = useState<number | null>(null)
  // 数据是否已加载（指标/overlay 依赖坐标转换，须等数据就绪）
  const [loaded, setLoaded] = useState(false)
  // 隐藏的关键位类型（勾选开关）
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set())

  // 初始化图表 + 拉取数据（klinecharts v10 通过 DataLoader 注入数据）
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    let disposed = false
    const cc = CANDLE_COLORS[useScanStore.getState().colorScheme]

    const chart = init(container, {
      styles: {
        grid: {
          show: true,
          horizontal: { show: true, color: '#1e222d', size: 1, style: 'solid', dashedValue: [4, 4] },
          vertical: { show: true, color: '#1e222d', size: 1, style: 'solid', dashedValue: [4, 4] },
        },
        candle: {
          bar: {
            upColor: cc.up,
            downColor: cc.down,
            noChangeColor: '#888888',
            upBorderColor: cc.up,
            downBorderColor: cc.down,
            noChangeBorderColor: '#888888',
            upWickColor: cc.up,
            downWickColor: cc.down,
            noChangeWickColor: '#888888',
          },
          priceMark: {
            high: { show: false },
            low: { show: false },
            last: {
              // 线颜色跟随涨跌配色（类型上不支持自定义 color）
              line: { show: true, style: 'dashed', size: 1, dashedValue: [4, 3] },
              text: { show: true, color: '#fff', size: 10 },
            },
          },
        },
        xAxis: {
          axisLine: { show: true, color: '#2b2b43' },
          tickLine: { show: true, color: '#2b2b43', length: 3 },
          tickText: { show: true, color: '#9aa3b2', size: 10 },
        },
        yAxis: {
          axisLine: { show: true, color: '#2b2b43' },
          tickLine: { show: true, color: '#2b2b43', length: 3 },
          tickText: { show: true, color: '#9aa3b2', size: 10 },
        },
        separator: { size: 1, color: '#2b2b43', fill: true, activeBackgroundColor: '#1e222d' },
        crosshair: {
          horizontal: {
            line: { show: true, color: '#758696', style: 'dashed', size: 1, dashedValue: [4, 3] },
            text: { show: true, color: '#fff', backgroundColor: '#758696', size: 10 },
          },
          vertical: {
            line: { show: true, color: '#758696', style: 'dashed', size: 1, dashedValue: [4, 3] },
            text: { show: true, color: '#fff', backgroundColor: '#758696', size: 10 },
          },
        },
      },
    })
    if (!chart) return
    chartRef.current = chart
    chart.setPeriod({ type: 'hour', span: 1 })

    scanApi
      .klines(symbol, limit)
      .then((data) => {
        if (disposed || !chartRef.current) return
        const list = data.klines.map((k: Kline) => ({
          timestamp: k.time,
          open: Number(k.open),
          high: Number(k.high),
          low: Number(k.low),
          close: Number(k.close),
          volume: Number(k.volume),
        }))
        const last = list[list.length - 1]?.close ?? 0
        // 按价格量级设置精度（setSymbol 必须在 setDataLoader 之前，避免二次加载）
        const pricePrecision = last > 0 ? (last < 1 ? 8 : last < 100 ? 4 : 2) : 2
        chart.setSymbol({ ticker: symbol, pricePrecision, volumePrecision: 2 })
        chart.setDataLoader({
          getBars: ({ callback }) => {
            callback(list, false)
          },
        })
        setLastClose(last)
        setLoaded(true)
        setError(null)
      })
      .catch((e) => {
        // 展示后端 503/4xx 的 detail（如交易所封禁时长）
        setError(e?.response?.data?.detail || 'K线数据加载失败')
      })

    const ro = new ResizeObserver(() => chartRef.current?.resize())
    ro.observe(container)

    return () => {
      disposed = true
      ro.disconnect()
      dispose(container)
      chartRef.current = null
      keyOverlayIdsRef.current = []
      aiOverlayIdRef.current = null
      setLoaded(false)
      setLastClose(null)
    }
  }, [symbol, limit, refreshKey])

  // 切换涨跌配色：直接改 K 线样式，无需重建图表
  useEffect(() => {
    const cc = CANDLE_COLORS[colorScheme]
    chartRef.current?.setStyles({
      candle: {
        bar: {
          upColor: cc.up,
          downColor: cc.down,
          upBorderColor: cc.up,
          downBorderColor: cc.down,
          upWickColor: cc.up,
          downWickColor: cc.down,
        },
      },
    })
  }, [colorScheme])

  // 同步技术指标：创建/更新参数/移除（全部为 klinecharts 内置指标，无手写计算）
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !loaded) return
    const cc = CANDLE_COLORS[colorScheme]
    for (const item of INDICATOR_CATALOG) {
      const setting: IndicatorSetting | undefined = indicatorSettings[item.name]
      const exists = chart.getIndicators({ name: item.name }).length > 0
      if (setting?.enabled) {
        const createOpts = {
          name: item.name,
          calcParams: setting.params,
          styles: indicatorStyles(item.name, cc, setting.params),
        }
        if (exists) {
          chart.overrideIndicator(createOpts)
        } else {
          chart.createIndicator(createOpts, item.isStack)
        }
      } else if (exists) {
        chart.removeIndicator({ name: item.name })
      }
    }
  }, [indicatorSettings, colorScheme, loaded, symbol])

  // 绘制关键位水平线：只画距当前价最近的 N 条支撑 + N 条压力，可按类型勾选隐藏
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !loaded || lastClose == null) return

    // 清除旧关键位 overlay
    for (const id of keyOverlayIdsRef.current) {
      try {
        chart.removeOverlay({ id })
      } catch {
        /* 已销毁 */
      }
    }
    keyOverlayIdsRef.current = []

    if (!keyLevels || keyLevels.length === 0) return

    // 按角色取距当前价最近的 N 条
    const pickNearest = (role: 'support' | 'resistance') =>
      keyLevels
        .filter((lv) => lv.role === role)
        .sort((a, b) => Math.abs(a.price - lastClose) - Math.abs(b.price - lastClose))
        .slice(0, MAX_LINES_PER_ROLE)

    const nearest = [...pickNearest('support'), ...pickNearest('resistance')].filter(
      (lv) => !hiddenKinds.has(lv.kind),
    )

    for (const lv of nearest) {
      const id = chart.createOverlay({
        name: 'keyLevelLine',
        points: [{ value: lv.price }],
        extendData: {
          label: KIND_LABEL[lv.kind] || lv.kind,
          color: lv.role === 'support' ? '#26a69a' : '#ef5350',
          priceText: fmtPrice(lv.price),
        },
      }) as string | null
      if (id) keyOverlayIdsRef.current.push(id)
    }
  }, [keyLevels, hiddenKinds, lastClose, loaded, symbol])

  // 绘制 AI 仓位标注（入场/止损/止盈线 + 区间色块）
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    if (aiOverlayIdRef.current) {
      try {
        chart.removeOverlay({ id: aiOverlayIdRef.current })
      } catch {
        /* 已销毁 */
      }
      aiOverlayIdRef.current = null
    }

    if (!ai || ai.entry_price == null || !loaded) return

    const isLong = ai.direction !== 'short'
    // lines 顺序 = overlay points 顺序，zones 用下标引用
    const lines: AiPosExt['lines'] = []
    const zones: AiPosExt['zones'] = []
    const pushLine = (label: string, price: number, color: string, dashed: boolean) => {
      lines.push({ label, price, priceText: fmtPrice(price), color, dashed })
      return lines.length - 1
    }

    const idxEntry = pushLine(isLong ? '做多' : '做空', ai.entry_price, isLong ? '#26a69a' : '#ef5350', false)
    if (ai.stop_loss != null) {
      const idxSL = pushLine('止损', ai.stop_loss, '#ef5350', true)
      zones.push({ from: idxEntry, to: idxSL, color: 'rgba(239, 83, 80, 0.15)' })
    }
    if (ai.take_profit_1 != null) {
      const idxTP1 = pushLine('止盈1', ai.take_profit_1, '#26a69a', true)
      zones.push({ from: idxEntry, to: idxTP1, color: 'rgba(38, 166, 154, 0.15)' })
      if (ai.take_profit_2 != null) {
        const idxTP2 = pushLine('止盈2', ai.take_profit_2, '#00bcd4', true)
        zones.push({ from: idxTP1, to: idxTP2, color: 'rgba(38, 166, 154, 0.08)' })
      }
    }

    const id = chart.createOverlay({
      name: 'aiPosition',
      points: lines.map((ln) => ({ value: ln.price })),
      extendData: { lines, zones },
    }) as string | null
    if (id) aiOverlayIdRef.current = id
  }, [ai, loaded, symbol])

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
        {/* 技术指标面板：全部内置指标，开关 + 参数（全局生效，localStorage 持久化） */}
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
              {[true, false].map((stack) => (
                <div key={String(stack)} style={stack ? { marginBottom: 10 } : undefined}>
                  <Text style={{ color: '#9aa3b2', fontSize: 11 }}>
                    {stack ? '主图指标' : '副图指标'}
                  </Text>
                  {INDICATOR_CATALOG.filter((it) => it.isStack === stack).map((it) => {
                    const setting = indicatorSettings[it.name]
                    return (
                      <div key={it.name} style={{ marginTop: 4 }}>
                        <Checkbox
                          checked={!!setting?.enabled}
                          onChange={() => toggleIndicator(it.name)}>
                          <span style={{ color: '#d1d4dc', fontSize: 12 }}>{it.label}</span>
                        </Checkbox>
                        {setting?.enabled && it.paramLabels.length > 0 && (
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginLeft: 22, marginTop: 2 }}>
                            {it.paramLabels.map((pl, i) => (
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
                                  setIndicatorParams(it.name, params)
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
          <Button size="small" ghost icon={<SettingOutlined />}>
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
    </div>
  )
}
