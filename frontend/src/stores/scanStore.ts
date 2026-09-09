import { create } from 'zustand'
import type { AIAnalysis, ScanRecord, ScanResult, ScanStatus } from '../types'
import { scanApi } from '../api/scan'

interface ScanState {
  status: ScanStatus | null
  results: ScanResult[]
  total: number
  loading: boolean
  history: ScanRecord[]
  currentScanId: string | null
  aiEnabled: boolean
  aiAnalyses: AIAnalysis[]
  aiLoading: boolean
  fetchStatus: () => Promise<void>
  fetchResults: (scanId?: string) => Promise<void>
  fetchHistory: () => Promise<void>
  triggerScan: () => Promise<void>
  setAiEnabled: (v: boolean) => void
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
  aiEnabled: localStorage.getItem('aiEnabled') === '1',
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
      const newScanId = id || null
      set({ results: data.items, total: data.total, currentScanId: newScanId })
      // 如果 AI 开启且有扫描 ID，自动加载 AI 分析
      if (get().aiEnabled && newScanId) {
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

  setAiEnabled: (v: boolean) => {
    localStorage.setItem('aiEnabled', v ? '1' : '0')
    set({ aiEnabled: v })
    // 开启 AI 时，如果有当前扫描结果，自动加载已有的 AI 分析
    if (v) {
      const scanId = get().currentScanId
      if (scanId) {
        get().fetchAiAnalyses(scanId)
      }
    } else {
      set({ aiAnalyses: [] })
      stopAiPolling()
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
