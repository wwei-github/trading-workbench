import axios from 'axios'
import type { ListResponse, ScanRecord, ScanResult, ScanStatus } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

export const scanApi = {
  trigger: () => api.post<{ scan_id: string; status: string }>('/scans').then((r) => r.data),

  list: (page = 1, pageSize = 20) =>
    api
      .get<ListResponse<ScanRecord>>('/scans', { params: { page, page_size: pageSize } })
      .then((r) => r.data),

  results: (scanId: string, page = 1, pageSize = 20, sortBy = 'breakout_pct', order = 'desc') =>
    api
      .get<ListResponse<ScanResult>>(`/scans/${scanId}/results`, {
        params: { page, page_size: pageSize, sort_by: sortBy, order },
      })
      .then((r) => r.data),

  latestResults: (page = 1, pageSize = 20, sortBy = 'breakout_pct', order = 'desc') =>
    api
      .get<ListResponse<ScanResult>>('/scans/latest/results', {
        params: { page, page_size: pageSize, sort_by: sortBy, order },
      })
      .then((r) => r.data),

  status: () => api.get<ScanStatus>('/scans/status').then((r) => r.data),
}
