// 策略插件 API（docs/20 S1）：清单 + 影子信号/观测/分析 + 手动触发（不开单）
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
  // 推演回填（NULL=未推演）
  outcome: string | null
  r: number | null
  eval_bars: number | null
}

export interface StrategyObservationRow {
  id: string
  symbol: string
  metrics: {
    gate_4h?: Record<string, unknown> | null
    structure_1h?: Record<string, unknown> | null
    range_score?: { score: number; items: { no: number; name: string; hit: boolean | null; value: unknown }[] } | null
    stochastic?: Record<string, unknown> | null
    macd_zero?: string | null
  }
  metrics_version: string
  created_at: string | null
}

export interface StrategyAnalysisStat {
  strategy_id: string
  signals: number
  evaluated: number
  decided: number
  open: number
  wins: number
  win_rate: number | null
  avg_r: number | null
  total_r: number | null
}

export interface StrategyAnalysis {
  stats: StrategyAnalysisStat[]
  overlaps: { pair: [string, string]; overlap_rate: number; hits: number }[]
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
  evaluate: () =>
    api.post<{ accepted: boolean }>('/strategies/signals/evaluate').then((r) => r.data),
  observations: (symbol?: string, limit = 100) =>
    api
      .get<StrategyObservationRow[]>('/strategies/observations', {
        params: { symbol: symbol || undefined, limit },
      })
      .then((r) => r.data),
  refreshObservations: () =>
    api.post<{ accepted: boolean }>('/strategies/observations/refresh').then((r) => r.data),
  analysis: () => api.get<StrategyAnalysis>('/strategies/analysis').then((r) => r.data),
}
