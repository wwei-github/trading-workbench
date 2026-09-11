// 图表指标目录：计算全部委托开源库 indicatorts（https://github.com/cinar/indicatorts，TS 原生，242 个指标）
// 本文件只做两件事：① 声明指标目录（名称/位置/参数）；② 把库函数输出适配成统一渲染结构
// 新增指标 = 在 INDICATOR_CATALOG 加一项配置，不需要写指标数学
import {
  ao,
  atr,
  bb,
  cci,
  donchianChannel,
  ema,
  kdj,
  kc,
  mfi,
  macd,
  obv,
  psar,
  roc,
  rsi,
  sma,
  williamsR,
  type Trend,
} from 'indicatorts'

// 渲染层输入上下文：一份 K 线 + 全局涨跌配色
export interface IndicatorContext {
  opens: number[]
  highs: number[]
  lows: number[]
  closes: number[]
  volumes: number[]
  cc: { up: string; down: string }
}

export interface IndicatorLine {
  color: string
  values: (number | null)[]
  lineStyle?: 'solid' | 'dotted' | 'dashed'
}

export interface IndicatorBar {
  value: number
  color: string
}

export interface IndicatorResult {
  lines: IndicatorLine[]
  bars?: IndicatorBar[]
}

export interface IndicatorDef {
  key: string
  label: string
  pane: 'main' | 'sub' // 主图叠加 / 独立副图
  defaults: number[]
  paramLabels: string[]
  // 柱状副图是否按成交量格式化刻度（如 VOL）
  volumeFormat?: boolean
  compute: (ctx: IndicatorContext, p: number[]) => IndicatorResult
}

const LINE_COLORS = ['#f0b90b', '#00bcd4', '#ff9800', '#b39ddb', '#ef5350']

// 多周期均线通用适配（EMA/MA）：每个周期一条线
const periodLines = (
  closes: number[],
  periods: number[],
  calc: (values: number[], period: number) => number[],
  colors: string[] = LINE_COLORS,
): IndicatorLine[] =>
  periods.map((period, i) => ({
    color: colors[i % colors.length],
    values: calc(closes, period),
  }))

// SAR 点位着色：上涨点/下跌点区分（trends: 1 上涨 / -1 下跌）
const sarLines = (r: { trends: Trend[]; psarResult: number[] }): IndicatorLine[] => {
  const up: (number | null)[] = []
  const down: (number | null)[] = []
  r.psarResult.forEach((v, i) => {
    up.push(r.trends[i] === 1 ? v : null)
    down.push(r.trends[i] === -1 ? v : null)
  })
  return [
    { color: '#26a69a', values: up, lineStyle: 'dotted' },
    { color: '#ef5350', values: down, lineStyle: 'dotted' },
  ]
}

export const INDICATOR_CATALOG: IndicatorDef[] = [
  // ===== 主图叠加 =====
  {
    key: 'ema',
    label: 'EMA 指数均线',
    pane: 'main',
    defaults: [21, 55, 144],
    paramLabels: ['周期1', '周期2', '周期3'],
    compute: (ctx, periods) => ({
      lines: periodLines(
        ctx.closes,
        periods,
        (v, period) => ema(v, { period }),
        ['#f0b90b', '#00bcd4', '#ff9800'],
      ),
    }),
  },
  {
    key: 'ma',
    label: 'MA 移动平均',
    pane: 'main',
    defaults: [5, 10, 30, 60],
    paramLabels: ['周期1', '周期2', '周期3', '周期4'],
    compute: (ctx, periods) => ({
      lines: periodLines(ctx.closes, periods, (v, period) => sma(v, { period })),
    }),
  },
  {
    key: 'boll',
    label: 'BOLL 布林带',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => {
      const r = bb(ctx.closes, { period })
      return {
        lines: [
          { color: '#4d8ef7', values: r.upper },
          { color: '#d1d4dc', values: r.middle },
          { color: '#4d8ef7', values: r.lower },
        ],
      }
    },
  },
  {
    key: 'kc',
    label: 'KC 肯特纳通道',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => {
      const r = kc(ctx.highs, ctx.lows, ctx.closes, { period })
      return {
        lines: [
          { color: '#00bcd4', values: r.upper },
          { color: '#d1d4dc', values: r.middle },
          { color: '#00bcd4', values: r.lower },
        ],
      }
    },
  },
  {
    key: 'donchian',
    label: '唐奇安通道',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => {
      // 库实现：上轨 = 收盘价滚动最高，下轨 = 滚动最低，中轨 = 两者均值
      const r = donchianChannel(ctx.closes, { period })
      return {
        lines: [
          { color: '#ef5350', values: r.upper },
          { color: '#f0b90b', values: r.middle },
          { color: '#26a69a', values: r.lower },
        ],
      }
    },
  },
  {
    key: 'psar',
    label: 'SAR 抛物线',
    pane: 'main',
    defaults: [],
    paramLabels: [],
    compute: (ctx) => ({ lines: sarLines(psar(ctx.highs, ctx.lows, ctx.closes)) }),
  },
  // ===== 副图 =====
  {
    key: 'vol',
    label: 'VOL 成交量',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    volumeFormat: true,
    compute: (ctx) => ({
      lines: [],
      bars: ctx.volumes.map((v, i) => ({
        value: v,
        color: ctx.closes[i] >= ctx.opens[i] ? ctx.cc.up : ctx.cc.down,
      })),
    }),
  },
  {
    key: 'macd',
    label: 'MACD',
    pane: 'sub',
    defaults: [12, 26, 9],
    paramLabels: ['快线', '慢线', '信号'],
    compute: (ctx, [fast, slow, signal]) => {
      const r = macd(ctx.closes, { fast, slow, signal })
      return {
        lines: [
          { color: '#f0b90b', values: r.macdLine },
          { color: '#00bcd4', values: r.signalLine },
        ],
        // 库只给两线，柱 = DIF - DEA（标准 MACD 定义，一行差值）
        bars: r.macdLine.map((v, i) => {
          const hist = v - r.signalLine[i]
          return { value: hist, color: hist >= 0 ? ctx.cc.up : ctx.cc.down }
        }),
      }
    },
  },
  {
    key: 'kdj',
    label: 'KDJ 随机指标',
    pane: 'sub',
    defaults: [9, 3],
    paramLabels: ['N', 'M'],
    compute: (ctx, [period, signalPeriod]) => {
      // 库配置字段：rPeriod（RSV）/ kPeriod / dPeriod，M 同时作用于 K、D 平滑
      const r = kdj(ctx.highs, ctx.lows, ctx.closes, {
        rPeriod: period,
        kPeriod: signalPeriod,
        dPeriod: signalPeriod,
      })
      return {
        lines: [
          { color: '#f0b90b', values: r.k },
          { color: '#00bcd4', values: r.d },
          { color: '#ef5350', values: r.j },
        ],
      }
    },
  },
  {
    key: 'rsi',
    label: 'RSI 相对强弱',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [{ color: '#b39ddb', values: rsi(ctx.closes, { period }) }],
    }),
  },
  {
    key: 'wr',
    label: 'WR 威廉指标',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [{ color: '#00bcd4', values: williamsR(ctx.highs, ctx.lows, ctx.closes, { period }) }],
    }),
  },
  {
    key: 'cci',
    label: 'CCI 顺势指标',
    pane: 'sub',
    defaults: [20],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [{ color: '#f0b90b', values: cci(ctx.highs, ctx.lows, ctx.closes, { period }) }],
    }),
  },
  {
    key: 'atr',
    label: 'ATR 真实波幅',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [{ color: '#ff9800', values: atr(ctx.highs, ctx.lows, ctx.closes, { period }).atrLine }],
    }),
  },
  {
    key: 'obv',
    label: 'OBV 能量潮',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    compute: (ctx) => ({
      lines: [{ color: '#b39ddb', values: obv(ctx.closes, ctx.volumes) }],
    }),
  },
  {
    key: 'mfi',
    label: 'MFI 资金流',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [
        { color: '#00bcd4', values: mfi(ctx.highs, ctx.lows, ctx.closes, ctx.volumes, { period }) },
      ],
    }),
  },
  {
    key: 'roc',
    label: 'ROC 变动率',
    pane: 'sub',
    defaults: [12],
    paramLabels: ['周期'],
    compute: (ctx, [period]) => ({
      lines: [{ color: '#f0b90b', values: roc(ctx.closes, { period }) }],
    }),
  },
  {
    key: 'ao',
    label: 'AO 动量震荡',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    compute: (ctx) => ({
      lines: [],
      bars: ao(ctx.highs, ctx.lows).map((v) => ({
        value: v,
        color: v >= 0 ? ctx.cc.up : ctx.cc.down,
      })),
    }),
  },
]
