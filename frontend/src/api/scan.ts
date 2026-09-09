import axios from 'axios'
import type { AIAnalysis, KlineData, ListResponse, ScanRecord, ScanResult, ScanStatus, SystemConfig } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

export const scanApi = {
  trigger: () => api.post<{ scan_id: string; status: string }>('/scans').then((r) => r.data),

  list: (page = 1, pageSize = 20) =>
    api
      .get<ListResponse<ScanRecord>>('/scans', { params: { page, page_size: pageSize } })
      .then((r) => r.data),

  results: (scanId: string, page = 1, pageSize = 20, sortBy = 'volume_24h', order = 'desc') =>
    api
      .get<ListResponse<ScanResult>>(`/scans/${scanId}/results`, {
        params: { page, page_size: pageSize, sort_by: sortBy, order },
      })
      .then((r) => r.data),

  latestResults: (page = 1, pageSize = 20, sortBy = 'volume_24h', order = 'desc') =>
    api
      .get<ListResponse<ScanResult>>('/scans/latest/results', {
        params: { page, page_size: pageSize, sort_by: sortBy, order },
      })
      .then((r) => r.data),

  status: () => api.get<ScanStatus>('/scans/status').then((r) => r.data),

  aiAnalyses: (scanId: string) =>
    api
      .get<{ items: AIAnalysis[]; total: number }>(`/scans/${scanId}/ai-analyses`)
      .then((r) => r.data),

  triggerAi: (scanId: string, scanResultId?: string) =>
    api
      .post<{ scan_id: string; status: string }>(`/scans/${scanId}/ai-analyses`, {
        scan_result_id: scanResultId ?? null,
      })
      .then((r) => r.data),

  klines: (symbol: string, limit = 100) =>
    api
      .get<KlineData>(`/scans/klines/${symbol}`, { params: { limit } })
      .then((r) => r.data),

  getConfig: () => api.get<SystemConfig>('/scans/config').then((r) => r.data),

  updateConfig: (data: Partial<SystemConfig>) =>
    api.put<SystemConfig>('/scans/config', data).then((r) => r.data),
}
