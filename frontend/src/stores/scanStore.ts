import { create } from 'zustand'
import type { AIAnalysis, ScanRecord, ScanResult, ScanStatus, SystemConfig, WatchlistItem } from '../types'
import { scanApi, type ResultFilters } from '../api/scan'
import { INDICATOR_CATALOG } from '../constants/indicators'

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

// 图表技术指标（开源库 indicatorts 计算，目录见 constants/indicators.ts；开关 + 参数，localStorage 持久化）
export interface IndicatorSetting {
  enabled: boolean
  params: number[]
}

export type IndicatorSettings = Record<string, IndicatorSetting>

const INDICATORS_KEY = 'chart-indicator-settings'

function defaultIndicatorSettings(): IndicatorSettings {
  const base: IndicatorSettings = {}
  for (const def of INDICATOR_CATALOG) {
    // 默认开启 EMA（与后端趋势判断周期一致）和 VOL
    base[def.key] = {
      enabled: def.key === 'ema' || def.key === 'vol',
      params: [...def.defaults],
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
      for (const def of INDICATOR_CATALOG) {
        const s = saved[def.key]
        if (!s) continue
        if (typeof s.enabled === 'boolean') base[def.key].enabled = s.enabled
        // 参数个数与目录一致才接受，避免旧格式残留
        if (
          Array.isArray(s.params) &&
          s.params.length === def.defaults.length &&
          s.params.every((v: unknown) => typeof v === 'number' && Number.isFinite(v))
        ) {
          base[def.key].params = s.params
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
  // 图表技术指标（全局，开关 + 参数，localStorage 持久化）
  indicatorSettings: IndicatorSettings
  toggleIndicator: (key: string) => void
  setIndicatorParams: (key: string, params: number[]) => void
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
  toggleIndicator: (key) => {
    const cur = get().indicatorSettings
    if (!cur[key]) return
    const next = { ...cur, [key]: { ...cur[key], enabled: !cur[key].enabled } }
    set({ indicatorSettings: next })
    try {
      localStorage.setItem(INDICATORS_KEY, JSON.stringify(next))
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  },
  setIndicatorParams: (key, params) => {
    const cur = get().indicatorSettings
    if (!cur[key]) return
    const next = { ...cur, [key]: { ...cur[key], params } }
    set({ indicatorSettings: next })
    try {
      localStorage.setItem(INDICATORS_KEY, JSON.stringify(next))
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  },
}))
