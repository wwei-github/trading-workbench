/**
 * 交易复盘K线图（交易记录列表点击币种弹窗）
 * 图表能力（指标/图例/工具栏/仓位价位线）全部在 ChartCore，与扫描结果图
 * 完全同源；本组件只负责：
 * - 数据：后端 /trades/{id}/klines 一次返回——开单前 100 根 + 持仓期间直到
 *   止损/平仓，附开单时初始止损止盈（OPEN 事件快照）与关键事件
 * - 复盘特有 SVG 覆盖层（drawOverlay）：止损区（入场→SL，红半透明）、
 *   止盈1/2区（绿半透明）从开仓K线延伸到图右缘；入场/止损/止盈水平线 +
 *   右侧价签；现止损琥珀线（跟踪移动后）
 * - 事件箭头（extraMarkers）：开仓蓝、止盈涨色、止损跌色
 * - 信息卡（headerExtra）：区间说明 + 三价摘要
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Empty, Spin, Tag, Typography } from 'antd'
import type { SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { scanApi } from '../api/scan'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import { fmtPrice, fmtTime, GLASS } from './klineShared'
import { bj, dayjs } from '../utils/dayjs'
import ChartCore, { type OverlayCtx } from './ChartCore'
import type { TradeKlineData, TradeKlineEvent } from '../types'

const { Text } = Typography

const CHART_HEIGHT = 520 // 容器无高度时的兜底值

// 后端 UTC naive → 毫秒（统一按 UTC 解析）
const utcMs = (v: string) => dayjs.utc(v).valueOf()

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

export default function TradeReplayChart({ tradeId }: { tradeId: string }) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const [data, setData] = useState<TradeKlineData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updating, setUpdating] = useState(false)
  // 复盘周期切换（2026-10-08）：reqInterval 为拉取周期（null=跟随服务端默认
  // cfg.kline_interval，可配非 1h/4h），serverInterval 为服务端实际返回档位。
  // 两者分离：首拉固化 serverInterval 不触发重拉；切周期不清旧数据、不整块
  // loading——旧图保留，新数据到达后 ChartCore 原地替换（同 KlineChart 平滑体验）
  const [reqInterval, setReqInterval] = useState<string | null>(null)
  const [serverInterval, setServerInterval] = useState<string>('1h')

  // 拉取复盘数据（tradeId / reqInterval 变化重拉；仅首拉整块 loading）
  useEffect(() => {
    let cancelled = false
    if (reqInterval == null) setLoading(true)
    scanApi.trades
      .klines(tradeId, reqInterval ?? undefined)
      .then((d) => {
        if (!cancelled) {
          setData(d)
          setError(null)
          setServerInterval(d.interval)
        }
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
  }, [tradeId, reqInterval])

  // 「更新」按钮：重拉复盘数据（在跑单的持仓区间会延长）
  const handleUpdate = useCallback(async () => {
    setUpdating(true)
    try {
      const d = await scanApi.trades.klines(tradeId, reqInterval ?? undefined)
      setData(d)
      setServerInterval(d.interval)
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || '复盘K线更新失败')
    } finally {
      setUpdating(false)
    }
  }, [tradeId, reqInterval])

  const isLong = data?.direction !== 'short'

  // 周期切换档位：服务端实际档 + 1h/4h，去重保序
  const intervalOptions = useMemo(() => {
    const vals = [serverInterval, '1h', '4h']
    return [...new Set(vals)].map((v) => ({ label: v, value: v }))
  }, [serverInterval])

  // 事件箭头标记（useMemo 保持引用稳定：ChartCore 以引用判断是否重应用标记）
  const extraMarkers = useMemo<SeriesMarker<UTCTimestamp>[]>(() => {
    if (!data) return []
    const markers: SeriesMarker<UTCTimestamp>[] = []
    for (const ev of data.events) {
      const m = eventMarker(ev, isLong, colorScheme)
      if (!m) continue
      const evMs = utcMs(ev.created_at)
      // 找事件时刻所在的K线（time <= evMs 的最后一根；早于首根则取首根）
      let time = data.klines[0] ? (Math.floor(data.klines[0].time / 1000) as UTCTimestamp) : undefined
      for (const k of data.klines) {
        if (k.time <= evMs) time = Math.floor(k.time / 1000) as UTCTimestamp
        else break
      }
      if (time == null) continue
      markers.push({ ...m, time, size: 1 })
    }
    return markers.sort((a, b) => (a.time as number) - (b.time as number))
  }, [data, isLong, colorScheme])

  // 复盘特有覆盖层：区域色块 + 风控线（ChartCore 每帧在空白 SVG 上调用）
  const drawZones = useCallback((ctx: OverlayCtx) => {
    const { chart, series, svg, container } = ctx
    if (!data) return

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
      line.setAttribute('stroke-width', '1')
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
    drawLine(data.stop_loss, colorSL, '止损', true)
    drawLine(data.tp1, colorTP, '止盈1', true)
    if (hasTP2) drawLine(data.tp2, colorTP, '止盈2', true)

    // 现止损（跟踪移动后的最新位置）：与初始止损不同才加画（琥珀色区分）；
    // 价签与已有标签同高时上下错位（保本止损=开仓价的场景）
    const curSl = data.current_stop_loss
    if (curSl != null && data.stop_loss != null && Math.abs(curSl - data.stop_loss) > 1e-12) {
      const cy = series.priceToCoordinate(curSl)
      if (cy != null) {
        const clash = tagYs.find((uy) => Math.abs(uy - cy) < TAG_GAP)
        const dy = clash == null ? 0 : clash > cy ? -TAG_SHIFT : TAG_SHIFT
        drawLine(curSl, CUR_SL_COLOR, '现止损', true, dy)
      }
    }
  }, [data, colorScheme])

  // 无数据（首拉中/首拉失败/新周期返回空）：整块反馈；
  // 有数据时（切周期/更新）旧图保留，错误只出图上方窄条（同 KlineChart）
  if (!data || data.klines.length === 0) {
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
    return <Empty description="无K线数据" style={{ marginTop: 60 }} />
  }

  const statusPnl =
    data.exit_reason != null ? ` · ${data.exit_reason}` : ''

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 420 }}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message="复盘K线更新失败"
          description={error}
          style={{ marginBottom: 8 }}
        />
      )}
      <ChartCore
        // chartKey 不含周期：图表实例跨周期复用——切周期时新数据由 ChartCore
        // 的 data effect 原地 setData（指标/可视区/标记重算，覆盖层每帧重绘）
        chartKey={tradeId}
        watermark={`${data.symbol} ${isLong ? '做多' : '做空'}`}
        label={data.symbol}
        data={data}
        from={-2}
        extraMarkers={extraMarkers}
        drawOverlay={drawZones}
        height={CHART_HEIGHT}
        onUpdate={handleUpdate}
        updating={updating}
        interval={serverInterval}
        onIntervalChange={(v) => setReqInterval(v)}
        intervalOptions={intervalOptions}
        headerExtra={
          // 复盘信息卡：区间说明 + 三价摘要（与扫描图工具栏/图例同栈不遮挡）
          <div
            style={{
              padding: '4px 10px',
              fontSize: 11,
              lineHeight: '18px',
              pointerEvents: 'none',
              color: '#8b93a7',
              ...GLASS,
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
              {data.current_stop_loss != null && data.stop_loss != null && Math.abs(data.current_stop_loss - data.stop_loss) > 1e-12 && (
                <>
                  {' '}· 现止损 <span style={{ color: CUR_SL_COLOR }}>{fmtPrice(data.current_stop_loss)}</span>
                </>
              )}
              <span style={{ marginLeft: 8, color: '#5a6377' }}>{bj(data.opened_at).format('MM-DD HH:mm')} 开仓</span>
            </div>
          </div>
        }
      />
      <Text type="secondary" style={{ position: 'absolute', bottom: 6, right: 10, fontSize: 10, zIndex: 20, color: '#5a6377' }}>
        止损/止盈1/2 为开单时快照 · 琥珀线=现止损（跟踪移动后）· 移动明细见操作历史
      </Text>
    </div>
  )
}
