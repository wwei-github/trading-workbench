// 策略插件 API（docs/20 S1）：清单只读 + 影子信号查询/手动触发（不开单）
import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 15000 })

export interface StrategyBrief {
  id: string
  display_name: string
  enabled: boolean
  max_positions: number
  risk_weight: number
  config: Record<string, unknown>
  kind: 'native' | 'plugin'
}

export interface StrategyShadowSignalRow {
  id: string
  strategy_id: string
  symbol: string
  direction: 'long' | 'short'
  entry: number
  stop: number
  tp1: number
  tp2: number
  signal_type: string | null
  reason: string | null
  confidence: number
  raw: Record<string, unknown> | null
  created_at: string | null
}

export const strategiesApi = {
  list: () => api.get<StrategyBrief[]>('/strategies').then((r) => r.data),
  signals: (strategyId?: string, limit = 100) =>
    api
      .get<StrategyShadowSignalRow[]>('/strategies/signals', {
        params: { strategy_id: strategyId || undefined, limit },
      })
      .then((r) => r.data),
  refresh: (symbols?: string[]) =>
    api
      .post<{ accepted: boolean }>('/strategies/signals/refresh', { symbols })
      .then((r) => r.data),
}
