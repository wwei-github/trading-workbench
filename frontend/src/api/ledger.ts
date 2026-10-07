// 虚拟账本 API（docs/16 P-07，独立模块）
import axios from 'axios'
import type { LedgerEntriesResp, LedgerStats } from '../types/ledger'

const api = axios.create({ baseURL: '/api', timeout: 60000 })

export interface LedgerEntryQuery {
  symbol?: string
  direction?: string
  status?: string
  outcome?: string
  source?: string
  signal_type?: string
  days?: number
  page?: number
  page_size?: number
}

export const ledgerApi = {
  stats: (days: number, source?: string) =>
    api
      .get<LedgerStats>('/ledger/stats', { params: { days, source: source || undefined } })
      .then((r) => r.data),
  entries: (q: LedgerEntryQuery) =>
    api.get<LedgerEntriesResp>('/ledger/entries', { params: q }).then((r) => r.data),
  backfill: (days: number, symbols?: string[]) =>
    api
      .post<{ ok: boolean; days: number; symbols: string | string[] | null }>('/ledger/backfill', {
        days,
        symbols,
      })
      .then((r) => r.data),
}
