// 图表指标目录（2026-10-09 换插件）：lightweight-charts-indicators@0.10.0
// （oakscriptjs Pine v6 口径，95 标准指标 + 1150 社区指标），画线走其
// IndicatorRenderer——本文件只做目录声明：包装插件的 calculate/metadata，
// 新增指标 = 从插件 import 后加一项配置（社区指标走 executeScript，见 docs/18 §七）
import type { Bar, IndicatorResult, PlotConfig } from 'oakscriptjs'
import {
  ATR,
  AwesomeOscillator,
  BollingerBands,
  CCI,
  DonchianChannels,
  EMA,
  KDJ,
  KeltnerChannels,
  MACD,
  MFI,
  OBV,
  ParabolicSAR,
  ROC,
  RSI,
  SMA,
  WilliamsPercentRange,
} from 'lightweight-charts-indicators'
import type { RenderableIndicator } from 'lightweight-charts-indicators/render'

// 插件 Bar 形状（time 为秒级 int，与宿主 candlePoints 一致）
export type IndicatorBar = Bar

// 插件指标模块 → RenderableIndicator 适配：overlay 在 metadata 里、
// hline/fill/arrow 配置按需携带
type IndicatorModule = {
  metadata: { overlay: boolean }
  plotConfig: PlotConfig[]
  hlineConfig?: RenderableIndicator['hlineConfig']
  fillConfig?: RenderableIndicator['fillConfig']
}
const mod = (m: IndicatorModule): RenderableIndicator => ({
  overlay: m.metadata.overlay,
  plotConfig: m.plotConfig,
  ...(m.hlineConfig ? { hlineConfig: m.hlineConfig } : {}),
  ...(m.fillConfig ? { fillConfig: m.fillConfig } : {}),
})

export interface IndicatorDef {
  key: string
  label: string
  // 主图叠加 / 独立副图（插件 metadata.overlay 决定 renderer 行为，
  // 这里保留目录语义用于 pane 分配）
  pane: 'main' | 'sub'
  defaults: number[]
  paramLabels: string[]
  // 计算：params 为用户可调参数（目录 defaults 覆盖插件默认）
  calculate: (bars: IndicatorBar[], params: number[]) => IndicatorResult
  // 渲染描述：插件模块自带（overlay + plotConfig），多周期均线族为合成配置
  renderable: (params: number[]) => RenderableIndicator
  // 图例短名（按 renderable.plotConfig 顺序；缺省用插件 title）
  legendNames?: string[]
  // 不进图例的 plot 下标（如 MACD 柱）
  legendSkip?: number[]
}

// 多周期均线族：插件 EMA/SMA 单实例只出一条 EMA 线（plot0），多周期 =
// 多次 calculate 合并（plot0_0/1/2…）+ 合成 plotConfig
const MA_COLORS = ['#f0b90b', '#00bcd4', '#ff9800', '#b39ddb', '#ef5350']
const multiPeriodMA = (mod: typeof EMA | typeof SMA, labelPrefix: string) => ({
  calculate: (bars: IndicatorBar[], periods: number[]): IndicatorResult => {
    const plots: IndicatorResult['plots'] = {}
    periods.forEach((p, i) => {
      const r = mod.calculate(bars, { length: p })
      plots[`plot0_${i}`] = r.plots.plot0 ?? []
    })
    return { metadata: mod.metadata, plots }
  },
  renderable: (periods: number[]): RenderableIndicator => ({
    overlay: true,
    plotConfig: periods.map((p, i): PlotConfig => ({
      id: `plot0_${i}`,
      title: `${labelPrefix}${p}`,
      color: MA_COLORS[i % MA_COLORS.length],
      style: 'line',
    })),
  }),
  legendNames: (periods: number[]) => periods.map((p) => `${labelPrefix}${p}`),
})

const EMA_MULTI = multiPeriodMA(EMA, 'EMA')
const SMA_MULTI = multiPeriodMA(SMA, 'MA')

export const INDICATOR_CATALOG: IndicatorDef[] = [
  // ===== 主图叠加 =====
  {
    key: 'ema',
    label: 'EMA 指数均线',
    pane: 'main',
    defaults: [21, 55, 144],
    paramLabels: ['周期1', '周期2', '周期3'],
    calculate: EMA_MULTI.calculate,
    renderable: EMA_MULTI.renderable,
    legendNames: EMA_MULTI.legendNames([21, 55, 144]),
  },
  {
    key: 'ma',
    label: 'MA 移动平均',
    pane: 'main',
    defaults: [5, 10, 30, 60],
    paramLabels: ['周期1', '周期2', '周期3', '周期4'],
    calculate: SMA_MULTI.calculate,
    renderable: SMA_MULTI.renderable,
    legendNames: SMA_MULTI.legendNames([5, 10, 30, 60]),
  },
  {
    key: 'boll',
    label: 'BOLL 布林带',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => BollingerBands.calculate(bars, { length }),
    renderable: () => mod(BollingerBands),
    legendNames: ['中轨', '上轨', '下轨'],
  },
  {
    key: 'kc',
    label: 'KC 肯特纳通道',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => KeltnerChannels.calculate(bars, { length }),
    renderable: () => mod(KeltnerChannels),
  },
  {
    key: 'donchian',
    label: '唐奇安通道',
    pane: 'main',
    defaults: [20],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => DonchianChannels.calculate(bars, { length }),
    renderable: () => mod(DonchianChannels),
  },
  {
    key: 'psar',
    label: 'SAR 抛物线',
    pane: 'main',
    defaults: [],
    paramLabels: [],
    calculate: (bars) => ParabolicSAR.calculate(bars),
    renderable: () => mod(ParabolicSAR),
  },
  // ===== 副图 =====
  {
    // VOL 成交量：宿主直绘（up/down 着色 + volume 刻度，插件无此定制），
    // calculate/renderable 不走（ChartCore 按 key 特判）
    key: 'vol',
    label: 'VOL 成交量',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    calculate: () => ({ metadata: { title: 'Volume', overlay: false }, plots: {} }),
    renderable: () => ({ overlay: false, plotConfig: [] }),
  },
  {
    key: 'macd',
    label: 'MACD',
    pane: 'sub',
    defaults: [12, 26, 9],
    paramLabels: ['快线', '慢线', '信号'],
    calculate: (bars, [fastLength, slowLength, signalLength]) =>
      MACD.calculate(bars, { fastLength, slowLength, signalLength }),
    renderable: () => mod(MACD),
    legendNames: ['MACD', 'DIF', 'DEA'],
    legendSkip: [0], // 柱线不进图例（两线已表达趋势）
  },
  {
    key: 'kdj',
    label: 'KDJ 随机指标',
    pane: 'sub',
    defaults: [9, 3],
    paramLabels: ['N', 'M'],
    calculate: (bars, [period, signal]) => KDJ.calculate(bars, { period, signal }),
    renderable: () => mod(KDJ),
    legendNames: ['K', 'D', 'J'],
  },
  {
    key: 'rsi',
    label: 'RSI 相对强弱',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => RSI.calculate(bars, { length }),
    renderable: () => mod(RSI), // 70/50/30 参考线由插件 hlineConfig 自带
    legendNames: ['RSI'],
  },
  {
    key: 'wr',
    label: 'WR 威廉指标',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => WilliamsPercentRange.calculate(bars, { length }),
    renderable: () => mod(WilliamsPercentRange),
    legendNames: ['WR'],
  },
  {
    key: 'cci',
    label: 'CCI 顺势指标',
    pane: 'sub',
    defaults: [20],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => CCI.calculate(bars, { length }),
    renderable: () => mod(CCI),
    legendNames: ['CCI'],
  },
  {
    key: 'atr',
    label: 'ATR 真实波幅',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => ATR.calculate(bars, { length }),
    renderable: () => mod(ATR),
    legendNames: ['ATR'],
  },
  {
    key: 'obv',
    label: 'OBV 能量潮',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    calculate: (bars) => OBV.calculate(bars),
    renderable: () => mod(OBV),
  },
  {
    key: 'mfi',
    label: 'MFI 资金流',
    pane: 'sub',
    defaults: [14],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => MFI.calculate(bars, { length }),
    renderable: () => mod(MFI),
    legendNames: ['MFI'],
  },
  {
    key: 'roc',
    label: 'ROC 变动率',
    pane: 'sub',
    defaults: [12],
    paramLabels: ['周期'],
    calculate: (bars, [length]) => ROC.calculate(bars, { length }),
    renderable: () => mod(ROC),
    legendNames: ['ROC'],
  },
  {
    key: 'ao',
    label: 'AO 动量震荡',
    pane: 'sub',
    defaults: [],
    paramLabels: [],
    calculate: (bars) => AwesomeOscillator.calculate(bars),
    renderable: () => mod(AwesomeOscillator),
  },
]
