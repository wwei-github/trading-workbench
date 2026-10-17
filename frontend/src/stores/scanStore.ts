import { create } from 'zustand'
import type { AIAnalysis, AppLog, ScanRecord, ScanResult, ScanStatus, SystemConfig, TaskRecord, WatchlistItem } from '../types'
import { scanApi, type ResultFilters } from '../api/scan'
import { DEFAULT_ENABLED_IDS, getIndicator } from '../constants/indicators'

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

// 图表技术指标（插件 registry 全量驱动，目录见 constants/indicators.ts）：
// 每项 = 开关 + 参数记录（inputs 按插件 inputConfig 的 id 键控：数值/开关/颜色/下拉等），
// localStorage 持久化。缺省的 key 视为未启用（不为 1290 个指标预建条目）
export interface IndicatorSetting {
  enabled: boolean
  inputs: Record<string, unknown>
}

export type IndicatorSettings = Record<string, IndicatorSetting>

const INDICATORS_KEY = 'chart-indicator-settings'

// 遗留宿主指标 key 一次性迁移（2026-10-17）：旧版宿主多周期 EMA/MA 的存档 key 为
// 'ema'/'ma'（label「EMA 指数均线」），后又改 'ema-multi'/'ma-multi'；而这两个 id 现已被
// 插件 registry 里同名的社区单周期指标占用（'ema'=EMA(9)、'ma'=SMA(9)）。未迁移的旧存档
// 会把 enabled=true 解析成社区单周期线，导致图表在配置的 21/55/144 之外多画一条 EMA9/MA9。
// 升级后清一次即可（用独立标记保证只清一次，避免误删用户此后主动启用的社区指标）。
const LEGACY_MIGRATION_KEY = 'chart-indicator-settings-migrated-v1'
const LEGACY_HOST_INDICATOR_KEYS = ['ema', 'ema-multi', 'ma', 'ma-multi']

function defaultIndicatorSettings(): IndicatorSettings {
  const base: IndicatorSettings = {}
  for (const id of DEFAULT_ENABLED_IDS) {
    const def = getIndicator(id)
    if (def) base[id] = { enabled: true, inputs: { ...def.defaultInputs } }
  }
  return base
}

function loadIndicatorSettings(): IndicatorSettings {
  const base = defaultIndicatorSettings()
  try {
    const raw = localStorage.getItem(INDICATORS_KEY)
    if (raw) {
      const saved = JSON.parse(raw) as Record<string, IndicatorSetting>
      if (!localStorage.getItem(LEGACY_MIGRATION_KEY)) {
        let changed = false
        for (const k of LEGACY_HOST_INDICATOR_KEYS) {
          if (k in saved) {
            delete saved[k]
            changed = true
          }
        }
        if (changed) localStorage.setItem(INDICATORS_KEY, JSON.stringify(saved))
        localStorage.setItem(LEGACY_MIGRATION_KEY, '1')
      }
      for (const [id, s] of Object.entries(saved)) {
        const def = getIndicator(id)
        // 已下线/未知指标、结构不合法的存档直接丢弃
        if (!def || typeof s?.enabled !== 'boolean') continue
        const inputs =
          s.inputs && typeof s.inputs === 'object'
            ? { ...def.defaultInputs, ...s.inputs }
            : { ...def.defaultInputs }
        base[id] = { enabled: s.enabled, inputs }
      }
    }
  } catch {
    /* 忽略 */
  }
  return base
}

function persistIndicatorSettings(settings: IndicatorSettings) {
  try {
    localStorage.setItem(INDICATORS_KEY, JSON.stringify(settings))
  } catch {
    /* 隐私模式等场景下忽略 */
  }
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
  // 任务记录（扫描/交易/复盘等任务粒度运行历史）
  tasks: TaskRecord[]
  tasksTotal: number
  tasksPage: number
  tasksPageSize: number
  tasksType: string // '' = 全部
  // 系统日志（WARNING 及以上报错）
  logs: AppLog[]
  logsTotal: number
  logsPage: number
  logsPageSize: number
  logLevel: string // '' = 全部
  logQuery: string
  currentScanId: string | null
  aiConfig: SystemConfig | null
  aiAnalyses: AIAnalysis[]
  // 按行跟踪 AI 分析状态：scanResultId -> { loading, error }
  analyzingMap: Record<string, { loading: boolean; error: string | null }>
  fetchStatus: () => Promise<void>
  fetchResults: (scanId?: string, page?: number, pageSize?: number) => Promise<void>
  setFilters: (filters: ResultFilters) => Promise<void>
  fetchTasks: (page?: number, pageSize?: number, taskType?: string) => Promise<void>
  fetchLogs: (page?: number, pageSize?: number, level?: string, q?: string) => Promise<void>
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
  // 图表技术指标（插件 registry 全量，开关 + 参数记录，localStorage 持久化）
  indicatorSettings: IndicatorSettings
  // 开关一个指标（首次启用时按目录 defaultInputs 建条目）
  toggleIndicator: (id: string) => void
  // 设置某项参数（按插件 inputConfig 的 input id）
  setIndicatorInput: (id: string, inputId: string, value: unknown) => void
  // 重置某指标参数回插件默认
  resetIndicator: (id: string) => void
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
  // Agent 管线单次分析可达 2~5 分钟，120s 会误报超时；放宽到 10 分钟
  const maxAttempts = 200 // 200 次 × 3 秒 = 600 秒
  aiPollTimers[scanResultId] = setInterval(async () => {
    attempts++
    try {
      // 主判定走进度事件（Redis，轻量）：分析进程一结束（done/error）立即停轮询，
      // 不再死等结果列表出现或耗满超时
      const prog = await scanApi.aiProgress(scanResultId).catch(() => null)
      if (prog) {
        const errEv = prog.events.find((e) => e.t === 'error')
        if (errEv || prog.status === 'done') {
          if (errEv) {
            stopRowPolling(scanResultId)
            useScanStore.setState((s) => ({
              analyzingMap: {
                ...s.analyzingMap,
                [scanResultId]: {
                  loading: false,
                  error: errEv.note || 'AI 分析失败',
                },
              },
            }))
          } else {
            await useScanStore.getState().fetchAiAnalyses(scanId)
            stopRowPolling(scanResultId)
          }
          return
        }
      }
      // 兜底：进度事件可能丢失（Redis 重启等），每 5 轮查一次结果列表
      if (attempts % 5 === 0) {
        await useScanStore.getState().fetchAiAnalyses(scanId)
        const analyses = useScanStore.getState().aiAnalyses
        const found = analyses.find((a) => a.scan_result_id === scanResultId)
        if (found) {
          stopRowPolling(scanResultId)
          return
        }
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
  tasks: [],
  tasksTotal: 0,
  tasksPage: 1,
  tasksPageSize: 10,
  tasksType: '',
  logs: [],
  logsTotal: 0,
  logsPage: 1,
  logsPageSize: 20,
  logLevel: '',
  logQuery: '',
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
        ? await scanApi.results(id, p, ps, 'quality_score', 'desc', get().filters)
        : await scanApi.latestResults(p, ps, 'quality_score', 'desc', get().filters)
      // 优先用传入的 id，否则从结果的 scan_record_id 推断
      const newScanId = id || data.items[0]?.scan_record_id || null
      set({
        results: data.items,
        total: data.total,
        resultsPage: p,
        resultsPageSize: ps,
        currentScanId: newScanId,
      })
      // 加载已有 AI 分析结果（展示与自动分析开关无关，有数据就展示）
      if (newScanId) {
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

  fetchTasks: async (page?, pageSize?, taskType?) => {
    try {
      const p = page ?? get().tasksPage
      const ps = pageSize ?? get().tasksPageSize
      const t = taskType ? '' + taskType : get().tasksType
      const data = await scanApi.tasks(p, ps, t)
      set({
        tasks: data.items, tasksTotal: data.total,
        tasksPage: p, tasksPageSize: ps, tasksType: t,
      })
    } catch (e) {
      console.error('获取任务记录失败', e)
    }
  },

  fetchLogs: async (page?, pageSize?, level?, q?) => {
    try {
      const p = page ?? get().logsPage
      const ps = pageSize ?? get().logsPageSize
      const lv = level ? '' + level : get().logLevel
      const query = q !== undefined ? q : get().logQuery
      const data = await scanApi.logs(p, ps, lv, query)
      set({
        logs: data.items, logsTotal: data.total,
        logsPage: p, logsPageSize: ps,
        logLevel: lv, logQuery: query,
      })
    } catch (e) {
      console.error('获取系统日志失败', e)
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
  toggleIndicator: (id) => {
    const def = getIndicator(id)
    if (!def) return
    const cur = get().indicatorSettings
    const existing = cur[id]
    const next: IndicatorSettings = {
      ...cur,
      [id]: existing
        ? { ...existing, enabled: !existing.enabled }
        : { enabled: true, inputs: { ...def.defaultInputs } },
    }
    set({ indicatorSettings: next })
    persistIndicatorSettings(next)
  },
  setIndicatorInput: (id, inputId, value) => {
    const def = getIndicator(id)
    if (!def) return
    const cur = get().indicatorSettings
    const existing = cur[id] ?? { enabled: false, inputs: { ...def.defaultInputs } }
    const next: IndicatorSettings = {
      ...cur,
      [id]: { ...existing, inputs: { ...existing.inputs, [inputId]: value } },
    }
    set({ indicatorSettings: next })
    persistIndicatorSettings(next)
  },
  resetIndicator: (id) => {
    const def = getIndicator(id)
    if (!def) return
    const cur = get().indicatorSettings
    const existing = cur[id]
    if (!existing) return
    const next: IndicatorSettings = {
      ...cur,
      [id]: { ...existing, inputs: { ...def.defaultInputs } },
    }
    set({ indicatorSettings: next })
    persistIndicatorSettings(next)
  },
}))
