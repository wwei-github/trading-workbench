import { create } from 'zustand'
import type { AIAnalysis, ScanRecord, ScanResult, ScanStatus, SystemConfig } from '../types'
import { scanApi } from '../api/scan'

interface ScanState {
  status: ScanStatus | null
  results: ScanResult[]
  total: number
  loading: boolean
  history: ScanRecord[]
  currentScanId: string | null
  aiConfig: SystemConfig | null
  aiAnalyses: AIAnalysis[]
  aiLoading: boolean
  fetchStatus: () => Promise<void>
  fetchResults: (scanId?: string) => Promise<void>
  fetchHistory: () => Promise<void>
  triggerScan: () => Promise<void>
  toggleAi: (enabled: boolean) => Promise<void>
  updateConfig: (data: Partial<SystemConfig>) => Promise<void>
  fetchAiAnalyses: (scanId: string) => Promise<void>
  triggerAiAnalysis: (scanId: string, scanResultId?: string) => Promise<void>
}

// AI 分析轮询计时器
let aiPollTimer: ReturnType<typeof setInterval> | null = null

function startAiPolling(scanId: string, store: () => ScanState) {
  stopAiPolling()
  let attempts = 0
  const maxAttempts = 30 // 最多轮询 30 次 × 3 秒 = 90 秒
  aiPollTimer = setInterval(async () => {
    attempts++
    try {
      await store().fetchAiAnalyses(scanId)
      const analyses = store().aiAnalyses
      // 如果已有分析结果且数量匹配结果数，停止轮询
      if (analyses.length > 0 && store().results.length > 0) {
        const analyzedIds = new Set(analyses.map((a) => a.scan_result_id))
        const allDone = store().results.every((r) => analyzedIds.has(r.id))
        if (allDone || attempts >= maxAttempts) {
          stopAiPolling()
        }
      } else if (attempts >= maxAttempts) {
        stopAiPolling()
      }
    } catch {
      // 忽略轮询错误
    }
  }, 3000)
}

function stopAiPolling() {
  if (aiPollTimer) {
    clearInterval(aiPollTimer)
    aiPollTimer = null
  }
}

export const useScanStore = create<ScanState>((set, get) => ({
  status: null,
  results: [],
  total: 0,
  loading: false,
  history: [],
  currentScanId: null,
  aiConfig: null,
  aiAnalyses: [],
  aiLoading: false,

  fetchStatus: async () => {
    try {
      const data = await scanApi.status()
      set({ status: data })
    } catch (e) {
      console.error('获取扫描状态失败', e)
    }
  },

  fetchResults: async (scanId?: string) => {
    set({ loading: true })
    try {
      const id = scanId || get().currentScanId
      const data = id
        ? await scanApi.results(id)
        : await scanApi.latestResults()
      // 优先用传入的 id，否则从结果的 scan_record_id 推断
      const newScanId = id || data.items[0]?.scan_record_id || null
      set({ results: data.items, total: data.total, currentScanId: newScanId })
      // 如果 AI 开启且有扫描 ID，自动加载 AI 分析
      const cfg = get().aiConfig
      if (cfg?.ai_analysis_enabled && newScanId) {
        get().fetchAiAnalyses(newScanId)
      }
    } catch (e) {
      console.error('获取扫描结果失败', e)
    } finally {
      set({ loading: false })
    }
  },

  fetchHistory: async () => {
    try {
      const data = await scanApi.list(1, 10)
      set({ history: data.items })
    } catch (e) {
      console.error('获取历史记录失败', e)
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
          get().fetchAiAnalyses(scanId)
        }
      } else {
        set({ aiAnalyses: [] })
        stopAiPolling()
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

  triggerAiAnalysis: async (scanId: string, scanResultId?: string) => {
    set({ aiLoading: true })
    try {
      await scanApi.triggerAi(scanId, scanResultId)
      // 启动轮询，持续刷新 AI 分析结果
      const storeFn = () => get()
      startAiPolling(scanId, storeFn)
    } catch (e: any) {
      const detail = e?.response?.data?.detail || '触发 AI 分析失败'
      throw new Error(detail)
    } finally {
      set({ aiLoading: false })
    }
  },
}))
