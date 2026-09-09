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
      set({ results: data.items, total: data.total, currentScanId: id || null })
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
    } catch (e: any) {
      const detail = e?.response?.data?.detail || '触发 AI 分析失败'
      throw new Error(detail)
    } finally {
      set({ aiLoading: false })
    }
  },
}))
