// 稳健性扫描 API（docs/16 P-12，独立模块，挂 /api/backtests/robustness）
import axios from 'axios'
import type { RobustnessListItem, RobustnessRecord } from '../types/robustness'

const api = axios.create({ baseURL: '/api', timeout: 60000 })

export interface RobustnessBody {
  source: 'history' | 'scan'
  symbols: string[]
  days?: number            // start/end 缺省时必传（回看天数）
  start?: string           // YYYY-MM-DD；与 end 成对，优先于 days
  end?: string
  n_blocks?: number
}

export const robustnessApi = {
  create: (body: RobustnessBody) =>
    api.post<{ id: string; status: string }>('/backtests/robustness', body).then((r) => r.data),
  get: (id: string) =>
    api.get<RobustnessRecord>(`/backtests/robustness/${id}`).then((r) => r.data),
  list: (limit = 10) =>
    api.get<RobustnessListItem[]>('/backtests/robustness', { params: { limit } }).then((r) => r.data),
}
