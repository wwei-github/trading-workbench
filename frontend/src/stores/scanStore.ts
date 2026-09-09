import { create } from 'zustand'
import type { AIAnalysis, ScanRecord, ScanResult, ScanStatus, SystemConfig } from '../types'
import { scanApi } from '../api/scan'

interface ScanState {
  status: ScanStatus | null
  results: ScanResult[]
  total: number
  loading: boolean
  resultsPage: number
  resultsPageSize: number
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
  fetchHistory: (page?: number, pageSize?: number) => Promise<void>
  fetchConfig: () => Promise<void>
  triggerScan: () => Promise<void>
  toggleAi: (enabled: boolean) => Promise<void>
  updateConfig: (data: Partial<SystemConfig>) => Promise<void>
  fetchAiAnalyses: (scanId: string) => Promise<void>
  triggerAiAnalysis: (scanId: string, scanResultId: string) => Promise<void>
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
  const maxAttempts = 20 // 最多轮询 20 次 × 3 秒 = 60 秒
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
      // 超时
      if (attempts >= maxAttempts) {
        useScanStore.setState((s) => ({
          analyzingMap: {
            ...s.analyzingMap,
            [scanResultId]: { loading: false, error: 'AI 分析超时，请重试' },
          },
        }))
        stopRowPolling(scanResultId)
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
  history: [],
  historyTotal: 0,
  historyPage: 1,
  historyPageSize: 10,
  currentScanId: null,
  aiConfig: null,
  aiAnalyses: [],
  analyzingMap: {},

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
        ? await scanApi.results(id, p, ps)
        : await scanApi.latestResults(p, ps)
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

  triggerAiAnalysis: async (scanId: string, scanResultId: string) => {
    try {
      await scanApi.triggerAi(scanId, scanResultId)
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
}))
