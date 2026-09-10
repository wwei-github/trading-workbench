import { create } from 'zustand'
import type { AIAnalysis, ScanRecord, ScanResult, ScanStatus, SystemConfig, WatchlistItem } from '../types'
import { scanApi, type ResultFilters } from '../api/scan'

// 图表涨跌配色（全局）：红涨绿跌（国内习惯，默认）/ 绿涨红跌（国际习惯）
export type ColorScheme = 'red-up' | 'green-up'

const COLOR_SCHEME_KEY = 'chart-color-scheme'

function loadColorScheme(): ColorScheme {
  try {
    return localStorage.getItem(COLOR_SCHEME_KEY) === 'green-up' ? 'green-up' : 'red-up'
  } catch {
    return 'red-up'
  }
}

// 图表技术指标（klinecharts 内置指标全量目录，开关 + 参数，localStorage 持久化）
export interface IndicatorSetting {
  enabled: boolean
  params: number[] // calcParams，多值即多线
}

export interface IndicatorCatalogItem {
  name: string // klinecharts 内置指标名
  label: string
  isStack: boolean // true = 叠加主图，false = 独立副图
  defaults: number[] // 默认参数（与库内置一致；EMA 定制为 21/55/144）
  paramLabels: string[]
}

export const INDICATOR_CATALOG: IndicatorCatalogItem[] = [
  // 主图叠加
  { name: 'EMA', label: 'EMA 指数均线', isStack: true, defaults: [21, 55, 144], paramLabels: ['周期1', '周期2', '周期3'] },
  { name: 'MA', label: 'MA 移动平均', isStack: true, defaults: [5, 10, 30, 60], paramLabels: ['周期1', '周期2', '周期3', '周期4'] },
  { name: 'BOLL', label: 'BOLL 布林带', isStack: true, defaults: [20, 2], paramLabels: ['周期', '倍数'] },
  { name: 'BBI', label: 'BBI 多空指标', isStack: true, defaults: [3, 6, 12, 24], paramLabels: ['周期1', '周期2', '周期3', '周期4'] },
  { name: 'SAR', label: 'SAR 抛物线', isStack: true, defaults: [2, 2, 20], paramLabels: ['步长', '增量', '上限'] },
  // 副图
  { name: 'VOL', label: 'VOL 成交量', isStack: false, defaults: [5, 10, 20], paramLabels: ['MA1', 'MA2', 'MA3'] },
  { name: 'MACD', label: 'MACD', isStack: false, defaults: [12, 26, 9], paramLabels: ['快线', '慢线', '信号'] },
  { name: 'KDJ', label: 'KDJ 随机指标', isStack: false, defaults: [9, 3, 3], paramLabels: ['N', 'M1', 'M2'] },
  { name: 'RSI', label: 'RSI 相对强弱', isStack: false, defaults: [6, 12, 24], paramLabels: ['周期1', '周期2', '周期3'] },
  { name: 'WR', label: 'WR 威廉指标', isStack: false, defaults: [6, 10, 14], paramLabels: ['周期1', '周期2', '周期3'] },
  { name: 'CCI', label: 'CCI 顺势指标', isStack: false, defaults: [20], paramLabels: ['周期'] },
  { name: 'DMI', label: 'DMI 趋向指标', isStack: false, defaults: [14, 6], paramLabels: ['周期', '平滑'] },
  { name: 'OBV', label: 'OBV 能量潮', isStack: false, defaults: [30], paramLabels: ['MA周期'] },
  { name: 'BIAS', label: 'BIAS 乖离率', isStack: false, defaults: [6, 12, 24], paramLabels: ['周期1', '周期2', '周期3'] },
  { name: 'BRAR', label: 'BRAR 情绪指标', isStack: false, defaults: [26], paramLabels: ['周期'] },
  { name: 'CR', label: 'CR 能量指标', isStack: false, defaults: [26, 10, 20, 40, 60], paramLabels: ['周期', 'MA1', 'MA2', 'MA3', 'MA4'] },
  { name: 'PSY', label: 'PSY 心理线', isStack: false, defaults: [12, 6], paramLabels: ['周期', 'MA周期'] },
  { name: 'MTM', label: 'MTM 动量指标', isStack: false, defaults: [12, 6], paramLabels: ['周期', 'MA周期'] },
  { name: 'ROC', label: 'ROC 变动率', isStack: false, defaults: [12, 6], paramLabels: ['周期', 'MA周期'] },
  { name: 'TRIX', label: 'TRIX 三重指数', isStack: false, defaults: [12, 9], paramLabels: ['周期', 'MA周期'] },
  { name: 'DMA', label: 'DMA 平均线差', isStack: false, defaults: [10, 50, 10], paramLabels: ['短期', '长期', 'M周期'] },
  { name: 'EMV', label: 'EMV 简易波动', isStack: false, defaults: [14, 9], paramLabels: ['周期', 'MA周期'] },
  { name: 'VR', label: 'VR 成交量比率', isStack: false, defaults: [26, 6], paramLabels: ['周期', 'MA周期'] },
  { name: 'AO', label: 'AO 动量震荡', isStack: false, defaults: [5, 34], paramLabels: ['短期', '长期'] },
  { name: 'SMA', label: 'SMA 加权移动平均', isStack: false, defaults: [12, 2], paramLabels: ['周期', '权重'] },
  { name: 'AVP', label: 'AVP 均价', isStack: false, defaults: [], paramLabels: [] },
  { name: 'PVT', label: 'PVT 价量趋势', isStack: false, defaults: [], paramLabels: [] },
]

export type IndicatorSettings = Record<string, IndicatorSetting>

const INDICATORS_KEY = 'chart-indicator-settings-v2'

function defaultIndicatorSettings(): IndicatorSettings {
  const base: IndicatorSettings = {}
  for (const it of INDICATOR_CATALOG) {
    // 默认开启 EMA（与后端趋势判断周期一致）和 VOL
    base[it.name] = {
      enabled: it.name === 'EMA' || it.name === 'VOL',
      params: [...it.defaults],
    }
  }
  return base
}

function loadIndicatorSettings(): IndicatorSettings {
  const base = defaultIndicatorSettings()
  try {
    const raw = localStorage.getItem(INDICATORS_KEY)
    if (raw) {
      const saved = JSON.parse(raw)
      for (const it of INDICATOR_CATALOG) {
        const s = saved[it.name]
        if (!s) continue
        if (typeof s.enabled === 'boolean') base[it.name].enabled = s.enabled
        // 参数个数与目录一致才接受，避免旧格式残留
        if (
          Array.isArray(s.params) &&
          s.params.length === it.defaults.length &&
          s.params.every((v: unknown) => typeof v === 'number' && Number.isFinite(v))
        ) {
          base[it.name].params = s.params
        }
      }
    }
  } catch {
    /* 忽略 */
  }
  return base
}

interface ScanState {
  status: ScanStatus | null
  results: ScanResult[]
  total: number
  loading: boolean
  resultsPage: number
  resultsPageSize: number
  // 结果列表过滤条件（服务端过滤）
  filters: ResultFilters
  history: ScanRecord[]
  historyTotal: number
  historyPage: number
  historyPageSize: number
  currentScanId: string | null
  aiConfig: SystemConfig | null
  aiAnalyses: AIAnalysis[]
  // 按行跟踪 AI 分析状态：scanResultId -> { loading, error }
  analyzingMap: Record<string, { loading: boolean; error: string | null }>
  fetchStatus: () => Promise<void>
  fetchResults: (scanId?: string, page?: number, pageSize?: number) => Promise<void>
  setFilters: (filters: ResultFilters) => Promise<void>
  fetchHistory: (page?: number, pageSize?: number) => Promise<void>
  fetchConfig: () => Promise<void>
  triggerScan: () => Promise<void>
  toggleAi: (enabled: boolean) => Promise<void>
  updateConfig: (data: Partial<SystemConfig>) => Promise<void>
  fetchAiAnalyses: (scanId: string) => Promise<void>
  triggerAiAnalysis: (scanId: string, scanResultId: string, userInput?: string) => Promise<void>
  // 关注列表
  watchlist: WatchlistItem[]
  fetchWatchlist: () => Promise<void>
  addToWatchlist: (symbol: string) => Promise<void>
  removeFromWatchlist: (symbol: string) => Promise<void>
  // 图表涨跌配色（全局切换，localStorage 持久化）
  colorScheme: ColorScheme
  setColorScheme: (scheme: ColorScheme) => void
  // 图表技术指标（klinecharts 内置指标，开关 + 参数，localStorage 持久化）
  indicatorSettings: IndicatorSettings
  toggleIndicator: (name: string) => void
  setIndicatorParams: (name: string, params: number[]) => void
}

// 每行的轮询计时器：scanResultId -> timer
const aiPollTimers: Record<string, ReturnType<typeof setInterval>> = {}

function stopRowPolling(scanResultId: string) {
  if (aiPollTimers[scanResultId]) {
    clearInterval(aiPollTimers[scanResultId])
    delete aiPollTimers[scanResultId]
  }
  useScanStore.setState((s) => ({
    analyzingMap: {
      ...s.analyzingMap,
      [scanResultId]: { loading: false, error: null },
    },
  }))
}

function startRowPolling(scanId: string, scanResultId: string) {
  // 先停掉该行已有的轮询
  stopRowPolling(scanResultId)
  useScanStore.setState((s) => ({
    analyzingMap: {
      ...s.analyzingMap,
      [scanResultId]: { loading: true, error: null },
    },
  }))
  let attempts = 0
  const maxAttempts = 40 // 最多轮询 40 次 × 3 秒 = 120 秒（思考型模型分析较慢，且可能内部重试）
  aiPollTimers[scanResultId] = setInterval(async () => {
    attempts++
    try {
      await useScanStore.getState().fetchAiAnalyses(scanId)
      const analyses = useScanStore.getState().aiAnalyses
      const found = analyses.find((a) => a.scan_result_id === scanResultId)
      if (found) {
        // 该行分析完成
        stopRowPolling(scanResultId)
        return
      }
      // 超时（先停轮询再写错误，避免被 stopRowPolling 清掉）
      if (attempts >= maxAttempts) {
        stopRowPolling(scanResultId)
        useScanStore.setState((s) => ({
          analyzingMap: {
            ...s.analyzingMap,
            [scanResultId]: { loading: false, error: 'AI 分析超时，请重试' },
          },
        }))
      }
    } catch {
      // 忽略轮询错误
    }
  }, 3000)
}

export const useScanStore = create<ScanState>((set, get) => ({
  status: null,
  results: [],
  total: 0,
  loading: false,
  resultsPage: 1,
  resultsPageSize: 20,
  filters: {},
  history: [],
  historyTotal: 0,
  historyPage: 1,
  historyPageSize: 10,
  currentScanId: null,
  aiConfig: null,
  aiAnalyses: [],
  analyzingMap: {},
  watchlist: [],

  fetchStatus: async () => {
    try {
      const data = await scanApi.status()
      set({ status: data })
    } catch (e) {
      console.error('获取扫描状态失败', e)
    }
  },

  fetchResults: async (scanId?: string, page?: number, pageSize?: number) => {
    set({ loading: true })
    try {
      const id = scanId || get().currentScanId
      // scanId 变化时重置到第 1 页
      const scanChanged = scanId && scanId !== get().currentScanId
      const p = scanChanged ? 1 : (page ?? get().resultsPage)
      const ps = pageSize ?? get().resultsPageSize
      const data = id
        ? await scanApi.results(id, p, ps, 'volume_24h', 'desc', get().filters)
        : await scanApi.latestResults(p, ps, 'volume_24h', 'desc', get().filters)
      // 优先用传入的 id，否则从结果的 scan_record_id 推断
      const newScanId = id || data.items[0]?.scan_record_id || null
      set({
        results: data.items,
        total: data.total,
        resultsPage: p,
        resultsPageSize: ps,
        currentScanId: newScanId,
      })
      // 如果 AI 开启且有扫描 ID，只加载已有 AI 分析结果（不自动触发）
      const cfg = get().aiConfig
      if (cfg?.ai_analysis_enabled && newScanId) {
        await get().fetchAiAnalyses(newScanId)
      }
    } catch (e) {
      console.error('获取扫描结果失败', e)
    } finally {
      set({ loading: false })
    }
  },

  setFilters: async (filters: ResultFilters) => {
    set({ filters, resultsPage: 1 })
    await get().fetchResults(undefined, 1)
  },

  fetchHistory: async (page?: number, pageSize?: number) => {
    try {
      const p = page ?? get().historyPage
      const ps = pageSize ?? get().historyPageSize
      const data = await scanApi.list(p, ps)
      set({
        history: data.items,
        historyTotal: data.total,
        historyPage: p,
        historyPageSize: ps,
      })
    } catch (e) {
      console.error('获取历史记录失败', e)
    }
  },

  fetchConfig: async () => {
    try {
      const data = await scanApi.getConfig()
      set({ aiConfig: data })
    } catch (e) {
      console.error('获取系统配置失败', e)
    }
  },

  triggerScan: async () => {
    try {
      await scanApi.trigger()
      await get().fetchStatus()
    } catch (e) {
      console.error('触发扫描失败', e)
      throw e
    }
  },

  toggleAi: async (enabled: boolean) => {
    try {
      const data = await scanApi.updateConfig({ ai_analysis_enabled: enabled })
      set({ aiConfig: data })
      if (enabled) {
        const scanId = get().currentScanId
        if (scanId) {
          // 只加载已有分析结果，不自动触发
          await get().fetchAiAnalyses(scanId)
        }
      } else {
        set({ aiAnalyses: [], analyzingMap: {} })
        // 清理所有行的轮询计时器
        for (const id of Object.keys(aiPollTimers)) {
          clearInterval(aiPollTimers[id])
          delete aiPollTimers[id]
        }
      }
    } catch (e: any) {
      console.error('更新 AI 配置失败', e)
      throw e
    }
  },

  updateConfig: async (data: Partial<SystemConfig>) => {
    try {
      const resp = await scanApi.updateConfig(data)
      set({ aiConfig: resp })
    } catch (e: any) {
      console.error('更新配置失败', e)
      throw e
    }
  },

  fetchAiAnalyses: async (scanId: string) => {
    try {
      const data = await scanApi.aiAnalyses(scanId)
      set({ aiAnalyses: data.items })
    } catch (e) {
      console.error('获取 AI 分析失败', e)
    }
  },

  triggerAiAnalysis: async (scanId: string, scanResultId: string, userInput?: string) => {
    try {
      await scanApi.triggerAi(scanId, scanResultId, userInput)
      // 启动该行的轮询
      startRowPolling(scanId, scanResultId)
    } catch (e: any) {
      const detail = e?.response?.data?.detail || '触发 AI 分析失败'
      useScanStore.setState((s) => ({
        analyzingMap: {
          ...s.analyzingMap,
          [scanResultId]: { loading: false, error: detail },
        },
      }))
      throw new Error(detail)
    }
  },

  // ===== 关注列表 =====
  fetchWatchlist: async () => {
    try {
      const data = await scanApi.watchlist.list()
      set({ watchlist: data.items })
    } catch (e) {
      console.error('获取关注列表失败', e)
    }
  },

  addToWatchlist: async (symbol: string) => {
    const item = await scanApi.watchlist.add(symbol)
    set((s) => {
      // 幂等：避免重复
      if (s.watchlist.find((w) => w.symbol === item.symbol)) return s
      return { watchlist: [item, ...s.watchlist] }
    })
  },

  removeFromWatchlist: async (symbol: string) => {
    await scanApi.watchlist.remove(symbol)
    set((s) => ({
      watchlist: s.watchlist.filter((w) => w.symbol !== symbol),
    }))
  },

  // ===== 图表涨跌配色 =====
  colorScheme: loadColorScheme(),
  setColorScheme: (scheme) => {
    set({ colorScheme: scheme })
    try {
      localStorage.setItem(COLOR_SCHEME_KEY, scheme)
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  },

  // ===== 图表技术指标 =====
  indicatorSettings: loadIndicatorSettings(),
  toggleIndicator: (name) => {
    const cur = get().indicatorSettings
    if (!cur[name]) return
    const next = { ...cur, [name]: { ...cur[name], enabled: !cur[name].enabled } }
    set({ indicatorSettings: next })
    try {
      localStorage.setItem(INDICATORS_KEY, JSON.stringify(next))
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  },
  setIndicatorParams: (name, params) => {
    const cur = get().indicatorSettings
    if (!cur[name]) return
    const next = { ...cur, [name]: { ...cur[name], params } }
    set({ indicatorSettings: next })
    try {
      localStorage.setItem(INDICATORS_KEY, JSON.stringify(next))
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  },
}))
