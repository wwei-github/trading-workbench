// 图表指标注册表（2026-10-16 全量插件驱动）
// 数据源 = lightweight-charts-indicators@0.10.0 的 indicatorRegistry（1290 个：
// 95 标准 + 1150 社区 + 45 K线形态），每条含 inputConfig（完整参数定义）/
// plotConfig / hline·fill·arrow 配置 / defaultInputs / calculate。
// 本文件不再硬编码指标，只做三件事：
//   1) 把 registry 条目适配成统一的 ChartIndicatorDef（供 ChartCore 通用渲染）
//   2) 保留两个宿主特例：VOL（up/down 着色直绘，插件不支持）与 EMA/MA 多周期族
//      （插件单实例只出一条线，多周期 = 多次 calculate 合并 plots）
//   3) 提供「常用」「社区明星」白名单与搜索/分类过滤
// 注：import indicatorRegistry 会打包整个插件（index.mjs ≈ 6.4MB，本工具为本地自托管，
// 换取「1290 指标全量可选」的能力，可接受；若日后需瘦身可改动态 import 按需加载）。
import { indicatorRegistry, type IndicatorRegistryEntry } from 'lightweight-charts-indicators'
import type { IndicatorResult, InputConfig } from 'oakscriptjs'
import type { Bar } from 'oakscriptjs'
import type { RenderableIndicator } from 'lightweight-charts-indicators/render'

// 插件 Bar 形状（time 为秒级 int，与宿主 candlePoints 一致）
export type IndicatorBar = Bar
export type { InputConfig }
export { indicatorRegistry }

// 统一指标描述：registry 条目与宿主特例共用同一套字段，ChartCore 据此分配 pane、
// 计算、渲染与出图例；IndicatorPanel 据此生成参数控件
export interface ChartIndicatorDef {
  id: string
  name: string
  // 分类（registry 原生分类；宿主特例用「常用」）
  category: string
  // 主图叠加（pane 0）/ 独立副图（顺序分配 pane 1,2,3…）
  overlay: boolean
  // 参数定义（驱动参数面板；缺省为空 = 无可调参数）
  inputConfig: InputConfig[]
  // 默认参数（store 初始化/重置用）
  defaultInputs: Record<string, unknown>
  calculate: (bars: IndicatorBar[], inputs: Record<string, unknown>) => IndicatorResult
  // 渲染描述（overlay + plotConfig + hline/fill/arrow，插件 IndicatorRenderer 直接消费）
  renderable: (inputs: Record<string, unknown>) => RenderableIndicator
  // 图例短名（按 plotConfig 顺序；缺省用 plotConfig.title）；随 inputs 变化
  legendNames?: (inputs: Record<string, unknown>) => string[]
  // 不进图例的 plot 下标（如 MACD 柱）
  legendSkip?: number[]
  // 宿主直绘（如 VOL）：不走插件 renderer，由 ChartCore 特判
  hostDrawn?: boolean
  // 稳定排序（决定 pane 分配顺序）：特例为负，registry 条目为原始下标
  order: number
}

// registry 条目 → ChartIndicatorDef：条目本身即满足 RenderableIndicator 结构
// （overlay/plotConfig/hlineConfig/fillConfig/arrowConfig），直接透传以免遗漏未来字段
const fromRegistry = (e: IndicatorRegistryEntry, order: number): ChartIndicatorDef => ({
  id: e.id,
  name: e.name,
  category: e.category,
  overlay: e.overlay,
  inputConfig: e.inputConfig ?? [],
  defaultInputs: { ...(e.defaultInputs ?? {}) },
  calculate: (bars, inputs) => e.calculate(bars, inputs) as IndicatorResult,
  renderable: () => e as unknown as RenderableIndicator,
  order,
})

// ── 宿主特例 1：VOL 成交量 ──────────────────────────────────────────
// 插件无 up/down 着色 + volume 价格刻度，保留宿主直绘（ChartCore 按 hostDrawn 特判）
const VOL_DEF: ChartIndicatorDef = {
  id: 'vol',
  name: 'VOL 成交量',
  category: '常用',
  overlay: false,
  inputConfig: [],
  defaultInputs: {},
  calculate: () => ({ metadata: { title: 'Volume', shortTitle: 'Vol', overlay: false }, plots: {} }),
  renderable: () => ({ overlay: false, plotConfig: [] }),
  hostDrawn: true,
  order: -3,
}

// ── 宿主特例 2：EMA/MA 多周期族 ─────────────────────────────────────
// 插件 EMA/SMA 单实例只出 plot0 一条线；多周期 = 多次 calculate 合并
// （plot0_0/1/2…）+ 合成 plotConfig，线宽收细（主图多线叠加不抢眼）
const MA_COLORS = ['#f0b90b', '#00bcd4', '#ff9800', '#b39ddb', '#ef5350']

function compositeMA(
  registryId: string,
  def: { id: string; name: string; prefix: string; defaults: number[]; order: number },
): ChartIndicatorDef {
  const entry = indicatorRegistry.find((e) => e.id === registryId)!
  const inputConfig: InputConfig[] = def.defaults.map((p, i) => ({
    id: `length${i + 1}`,
    type: 'int',
    title: `周期${i + 1}`,
    defval: p,
    min: 1,
    max: 500,
  }))
  const defaultInputs: Record<string, unknown> = Object.fromEntries(
    def.defaults.map((p, i) => [`length${i + 1}`, p]),
  )
  const lengthAt = (inputs: Record<string, unknown>, i: number) => {
    const v = Number(inputs[`length${i + 1}`])
    return Number.isFinite(v) && v > 0 ? Math.round(v) : def.defaults[i]
  }
  return {
    id: def.id,
    name: def.name,
    category: '常用',
    overlay: true,
    inputConfig,
    defaultInputs,
    calculate: (bars, inputs) => {
      const plots: IndicatorResult['plots'] = {}
      def.defaults.forEach((_p, i) => {
        const r = entry.calculate(bars, { length: lengthAt(inputs, i) })
        plots[`plot0_${i}`] = r.plots.plot0 ?? []
      })
      return { metadata: entry.metadata, plots }
    },
    renderable: (inputs) => ({
      overlay: true,
      plotConfig: def.defaults.map((_p, i) => ({
        id: `plot0_${i}`,
        title: `${def.prefix}${lengthAt(inputs, i)}`,
        color: MA_COLORS[i % MA_COLORS.length],
        style: 'line' as const,
        lineWidth: 1,
      })),
    }),
    legendNames: (inputs) => def.defaults.map((_p, i) => `${def.prefix}${lengthAt(inputs, i)}`),
    order: def.order,
  }
}

const EMA_MULTI = compositeMA('ema', {
  id: 'ema-multi',
  name: 'EMA 指数均线',
  prefix: 'EMA',
  defaults: [21, 55, 144],
  order: -2,
})
const MA_MULTI = compositeMA('sma', {
  id: 'ma-multi',
  name: 'MA 移动平均',
  prefix: 'MA',
  defaults: [5, 10, 30, 60],
  order: -1,
})

// 全量指标：宿主特例在前，registry 条目按原始下标
export const ALL_INDICATORS: ChartIndicatorDef[] = [
  VOL_DEF,
  EMA_MULTI,
  MA_MULTI,
  ...indicatorRegistry.map((e, i) => fromRegistry(e, i)),
]

const INDICATOR_MAP = new Map(ALL_INDICATORS.map((d) => [d.id, d]))

export const getIndicator = (id: string): ChartIndicatorDef | undefined => INDICATOR_MAP.get(id)

// 默认开启：EMA 多周期族（与后端趋势判断周期一致）+ VOL
export const DEFAULT_ENABLED_IDS = ['ema-multi', 'vol']

// ── 常用指标（原 16 项保留 + VWAP/SuperTrend 高频项）──────────────────
export const COMMON_IDS = [
  'ema-multi',
  'ma-multi',
  'bb',
  'keltner',
  'donchian',
  'parabolic-sar',
  'supertrend',
  'vwap',
  'vol',
  'macd',
  'kdj',
  'rsi',
  'williams-r',
  'cci',
  'atr',
  'obv',
  'mfi',
  'roc',
  'awesome-oscillator',
].filter((id) => INDICATOR_MAP.has(id))

// ── 社区明星指标（TradingView 社区高知名度，registry community 组）────
export const STAR_IDS = [
  'alpha-trend',
  'half-trend',
  'squeeze-momentum',
  'ttm-squeeze-pro',
  'ut-bot',
  'hull-suite',
  'trend-magic',
  'ssl-channel',
  'chandelier-exit',
  'qqe-mod',
  'coral-trend',
  'wavetrend',
  'pivot-point-supertrend',
  'trendlines-with-breaks',
  'market-structure-trailing-stop',
  'order-blocks-signals',
  'liquidity-levels',
  'zigzag-fibonacci',
].filter((id) => INDICATOR_MAP.has(id))

export const commonIndicators = (): ChartIndicatorDef[] =>
  COMMON_IDS.map((id) => INDICATOR_MAP.get(id)!).filter(Boolean)

export const starIndicators = (): ChartIndicatorDef[] =>
  STAR_IDS.map((id) => INDICATOR_MAP.get(id)!).filter(Boolean)

// 分类过滤选项（固定顺序；「常用」「社区明星」为白名单伪分类）
export const CATEGORY_FILTERS = [
  '全部',
  '常用',
  '社区明星',
  'Moving Averages',
  'Momentum',
  'Oscillators',
  'Trend',
  'Volatility',
  'Volume',
  'Channels & Bands',
  'Candlestick Patterns',
]

// 搜索 + 分类过滤：query 匹配 name/id（大小写不敏感）
export function filterIndicators(query: string, category: string): ChartIndicatorDef[] {
  const q = query.trim().toLowerCase()
  let pool: ChartIndicatorDef[]
  if (category === '常用') pool = commonIndicators()
  else if (category === '社区明星') pool = starIndicators()
  else if (category && category !== '全部') pool = ALL_INDICATORS.filter((d) => d.category === category)
  else pool = ALL_INDICATORS
  if (!q) return pool
  return pool.filter(
    (d) => d.name.toLowerCase().includes(q) || d.id.toLowerCase().includes(q),
  )
}

// 参数输入 source 类型的候选值（Pine 常用价格源）
export const SOURCE_OPTIONS = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4', 'hlcc4']
