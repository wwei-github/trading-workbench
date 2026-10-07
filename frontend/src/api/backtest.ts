// 合约回测 API（docs/16 P-06，独立模块）
import axios from 'axios'
import type { FuturesBacktestListItem, FuturesBacktestRecord } from '../types/backtest'

const api = axios.create({ baseURL: '/api', timeout: 60000 })

export interface FuturesBacktestBody {
  source: 'history' | 'scan'
  symbols: string[]
  days: number
  variants: { name: string; overrides: Record<string, number | boolean | null> }[]
  risk_usdt: number
  max_open_trades: number
  same_direction_cap: number
}

export const backtestApi = {
  create: (body: FuturesBacktestBody) =>
    api.post<{ id: string; status: string }>('/backtests', body).then((r) => r.data),
  get: (id: string) =>
    api.get<FuturesBacktestRecord>(`/backtests/${id}`).then((r) => r.data),
  list: (limit = 20) =>
    api.get<FuturesBacktestListItem[]>('/backtests', { params: { limit } }).then((r) => r.data),
}
