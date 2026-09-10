import axios from 'axios'
import type { AIAnalysis, KlineData, ListResponse, ScanRecord, ScanResult, ScanStatus, SystemConfig, WatchlistItem } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

// 结果列表过滤条件（服务端过滤）
export interface ResultFilters {
  signal_type?: string
  position?: string
  pattern?: string
  ema_state?: string
}

export const scanApi = {
  trigger: () => api.post<{ scan_id: string; status: string }>('/scans').then((r) => r.data),

  list: (page = 1, pageSize = 20) =>
    api
      .get<ListResponse<ScanRecord>>('/scans', { params: { page, page_size: pageSize } })
      .then((r) => r.data),

  results: (scanId: string, page = 1, pageSize = 20, sortBy = 'volume_24h', order = 'desc', filters?: ResultFilters) =>
    api
      .get<ListResponse<ScanResult>>(`/scans/${scanId}/results`, {
        params: { page, page_size: pageSize, sort_by: sortBy, order, ...(filters || {}) },
      })
      .then((r) => r.data),

  latestResults: (page = 1, pageSize = 20, sortBy = 'volume_24h', order = 'desc', filters?: ResultFilters) =>
    api
      .get<ListResponse<ScanResult>>('/scans/latest/results', {
        params: { page, page_size: pageSize, sort_by: sortBy, order, ...(filters || {}) },
      })
      .then((r) => r.data),

  status: () => api.get<ScanStatus>('/scans/status').then((r) => r.data),

  aiAnalyses: (scanId: string) =>
    api
      .get<{ items: AIAnalysis[]; total: number }>(`/scans/${scanId}/ai-analyses`)
      .then((r) => r.data),

  triggerAi: (scanId: string, scanResultId?: string, userInput?: string) =>
    api
      .post<{ scan_id: string; status: string }>(`/scans/${scanId}/ai-analyses`, {
        scan_result_id: scanResultId ?? null,
        user_input: userInput || null,
      })
      .then((r) => r.data),

  klines: (symbol: string, limit = 100) =>
    api
      .get<KlineData>(`/scans/klines/${symbol}`, { params: { limit } })
      .then((r) => r.data),

  getConfig: () => api.get<SystemConfig>('/scans/config').then((r) => r.data),

  updateConfig: (data: Partial<SystemConfig>) =>
    api.put<SystemConfig>('/scans/config', data).then((r) => r.data),

  // 手动搜索币种 AI 分析（同步调用，AI 思考耗时较长，放宽超时）
  analyzeCoin: (symbol: string) =>
    api
      .post<AIAnalysis>('/scans/analyze', { symbol }, { timeout: 180000 })
      .then((r) => r.data),

  // 关注列表
  watchlist: {
    list: () =>
      api.get<ListResponse<WatchlistItem>>('/watchlist').then((r) => r.data),
    quotes: () =>
      api
        .get<{ items: { symbol: string; price: number; volume_24h: number }[] }>(
          '/watchlist/quotes',
        )
        .then((r) => r.data),
    add: (symbol: string) =>
      api.post<WatchlistItem>('/watchlist', { symbol }).then((r) => r.data),
    remove: (symbol: string) =>
      api.delete<{ ok: boolean; symbol: string }>(`/watchlist/${symbol}`).then((r) => r.data),
  },
}
