/**
 * K 线图表共享内核（2026-09-27 统一组件）
 * - 全站唯一 K 线图组件 KlineChart（symbol 扫描 / tradeId 复盘双数据源）共用：
 *   图表创建配置 / 涨跌配色 / 数值格式化 / 价签走廊
 * - 新增 K 线图一律走 createTradeChart，禁止再各自 createChart 复制配置
 */
import type { CSSProperties } from 'react'
import {
  createChart,
  createTextWatermark,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { dayjs, BJ } from '../utils/dayjs'

// 涨跌配色方案：红涨绿跌（国内习惯，默认）/ 绿涨红跌（国际习惯）
// 全局生效（切换入口在页面顶部 ColorSchemeToggle）：K线实体与 AI 仓位线、
// 各面板的涨跌/盈亏文本、胜负/多空标签均随方案翻转（utils/scheme.ts）
export const CANDLE_COLORS: Record<ColorScheme, { up: string; down: string }> = {
  'red-up': { up: '#ef5350', down: '#26a69a' },
  'green-up': { up: '#26a69a', down: '#ef5350' },
}

// ===== 数值格式化（图例/AI 标注/价签共用）=====
export const fmtPrice = (p: number) =>
  p < 1 ? p.toFixed(6) : p < 100 ? p.toFixed(4) : p.toFixed(2)
export const fmtVol = (v: number) =>
  v >= 1e9
    ? `${(v / 1e9).toFixed(2)}B`
    : v >= 1e6
      ? `${(v / 1e6).toFixed(2)}M`
      : v >= 1e3
        ? `${(v / 1e3).toFixed(2)}K`
        : v.toFixed(2)
export const fmtTime = (sec: number) => {
  // K 线时间戳为 UTC epoch，统一转北京时区展示（与其他面板一致）
  return dayjs.unix(sec).tz(BJ).format('MM-DD HH:mm')
}
// 相对入场价的百分比对价（SL -3.1% / TP +4.5%，带符号一位小数）
export const pctDelta = (entry: number, p: number) => {
  const d = ((p - entry) / entry) * 100
  return `${d > 0 ? '+' : ''}${d.toFixed(1)}%`
}
// 盈亏比 RR = |TP1-入场| / |入场-SL|（风险为 0 或缺侧返回 null 不显示）
export const rrRatio = (entry: number, sl: number | null, tp: number | null): number | null => {
  if (sl == null || tp == null) return null
  const risk = Math.abs(entry - sl)
  return risk > 1e-12 ? Math.abs(tp - entry) / risk : null
}

// 悬浮玻璃拟态面板（工具栏/图例共用）
export const GLASS: CSSProperties = {
  background: 'rgba(19, 23, 34, 0.72)',
  backdropFilter: 'blur(10px)',
  WebkitBackdropFilter: 'blur(10px)',
  border: '1px solid rgba(66, 74, 96, 0.45)',
  borderRadius: 8,
  boxShadow: '0 4px 16px rgba(0, 0, 0, 0.25)',
}

// 统一图表创建：背景/网格/价格轴/时间轴/十字线/本地化/水印/涨跌配色
// 一处配置，所有 K 线图外观与交互完全一致
export function createTradeChart(
  container: HTMLDivElement,
  opts: { watermark: string; height: number },
): { chart: IChartApi; series: ISeriesApi<'Candlestick'> } {
  const chart = createChart(container, {
    layout: {
      // 上下渐变背景，比纯色更有层次
      background: { type: ColorType.VerticalGradient, topColor: '#1c2231', bottomColor: '#12151f' },
      textColor: '#c9cfdd',
      fontFamily:
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif",
      attributionLogo: false,
      // 主图与指标副图之间的分隔线调暗：平时与网格同色系，拖动悬停时轻微提亮
      panes: {
        enableResize: true,
        separatorColor: '#1d2332',
        separatorHoverColor: 'rgba(90, 100, 130, 0.25)',
      },
    },
    grid: {
      vertLines: { color: '#1d2332' },
      horzLines: { color: '#1d2332' },
    },
    rightPriceScale: {
      borderColor: '#252b3b',
      entireTextOnly: true,
      // 顶部留白 18%：避开左上角悬浮的工具栏/图例，最高 K 线不被遮挡
      scaleMargins: { top: 0.18, bottom: 0.08 },
    },
    timeScale: {
      borderColor: '#252b3b',
      // 显示时间（2026-10-16）：X 轴刻度统一显示「年月日 小时」（YYYY-MM-DD HH:mm），
      // 不再随缩放退化为纯日期。刻度时间戳为 UTC epoch，按北京时间展示（与全站一致）
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time: UTCTimestamp) =>
        dayjs.unix(time).tz(BJ).format('YYYY-MM-DD HH:mm'),
      rightOffset: 12,
      lockVisibleTimeRangeOnResize: true,
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#363c4e' },
      horzLine: { color: '#758696', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#363c4e' },
    },
    localization: {
      // 加密货币价格跨度大（0.00001 ~ 100000），按量级适配小数位
      priceFormatter: (p: number) => fmtPrice(p),
    },
    width: container.clientWidth || 520,
    height: container.clientHeight || opts.height,
  })

  // 主图中央水印（币种名，复盘图带方向）
  createTextWatermark(chart.panes()[0], {
    lines: [
      {
        text: opts.watermark,
        color: 'rgba(148, 158, 184, 0.09)',
        fontSize: 48,
        fontStyle: 'bold',
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      },
    ],
  })

  const cc = CANDLE_COLORS[useScanStore.getState().colorScheme]
  const series = chart.addSeries(CandlestickSeries, {
    upColor: cc.up,
    downColor: cc.down,
    borderVisible: false,
    wickUpColor: cc.up,
    wickDownColor: cc.down,
  })
  return { chart, series }
}

// 涨跌配色切换：直接改 series 选项，图表实例同步生效，无需重建
export function applyCandleScheme(series: ISeriesApi<'Candlestick'>, scheme: ColorScheme) {
  const c = CANDLE_COLORS[scheme]
  series.applyOptions({
    upColor: c.up,
    downColor: c.down,
    wickUpColor: c.up,
    wickDownColor: c.down,
  })
}

// 可视范围 + 右侧「价签走廊」（2026-09-23 用户反馈；2026-10-12 用户拍板加宽）：
// 右缘价签宽约 110px，按 barSpacing 换算成空K线根数避免标签压住最新K线，
// 目标走廊 180px（下限 12 根）给最新K线与右缘留出更宽的呼吸空间。
// from：可视起点逻辑下标（扫描图=最新130根起点；复盘图=-2 左侧留白）
export function applyCorridorView(
  chart: IChartApi,
  containerWidth: number | null | undefined,
  count: number,
  from: number,
) {
  const plotW = Math.max(200, (containerWidth || 520) - 64)
  let corridor = 12
  for (let i = 0; i < 2; i++) {
    corridor = Math.max(12, Math.ceil(180 / (plotW / (count - from + 2 + corridor))))
  }
  chart.timeScale().applyOptions({ rightOffset: corridor })
  chart.timeScale().setVisibleLogicalRange({ from, to: count + corridor })
}
