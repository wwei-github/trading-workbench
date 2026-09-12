import axios from 'axios'
import type { AIAnalysis, AiProgress, KlineData, ListResponse, ReviewStats, ScanRecord, ScanResult, ScanStatus, SkillInfo, StageTrace, SystemConfig, TradeEvent, TradeRecord, WatchlistItem } from '../types'

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

  klines: (symbol: string, limit = 100, forceRefresh = false) =>
    api
      .get<KlineData>(`/scans/klines/${symbol}`, {
        params: forceRefresh ? { limit, force_refresh: true } : { limit },
      })
      .then((r) => r.data),

  getConfig: () => api.get<SystemConfig>('/scans/config').then((r) => r.data),

  updateConfig: (data: Partial<SystemConfig>) =>
    api.put<SystemConfig>('/scans/config', data).then((r) => r.data),

  // 手动搜索币种 AI 分析（同步调用，AI 思考耗时较长，放宽超时）
  analyzeCoin: (symbol: string) =>
    api
      .post<AIAnalysis>('/scans/analyze', { symbol }, { timeout: 600000 })
      .then((r) => r.data),

  // AI 分析进度事件流（展开区域流式展示分析过程，2s 轮询）
  aiProgress: (scanResultId: string) =>
    api
      .get<AiProgress>(`/scans/ai-progress/${scanResultId}`)
      .then((r) => r.data),

  // ===== AI 建议复盘系统（P2）=====

  // 复盘统计（days 为统计窗口，expired 不计入胜率分母）
  reviewStats: (days = 30) =>
    api
      .get<ReviewStats>('/scans/review/stats', { params: { days } })
      .then((r) => r.data),

  // Agent 工具循环轨迹（404 = 分析记录不存在；stage_trace 为 null = 单次调用管线生成）
  aiTrace: (analysisId: string) =>
    api
      .get<{ analysis_id: string; stage_trace: StageTrace | null }>(
        `/scans/ai-trace/${analysisId}`,
      )
      .then((r) => r.data),

  // 技能库列表（只读，不含全文 body）
  skills: () => api.get<SkillInfo[]>('/scans/skills').then((r) => r.data),

  // 技能详情（含全文 body markdown，404 = 技能不存在）
  skillDetail: (name: string) =>
    api.get<SkillInfo>(`/scans/skills/${encodeURIComponent(name)}`).then((r) => r.data),

  // ===== 自动交易（docs/06）=====

  // 交易记录列表（新→旧；status 可选过滤）
  trades: {
    list: (status?: string, limit = 200) =>
      api
        .get<TradeRecord[]>('/trades', { params: { status, limit } })
        .then((r) => r.data),
    // 一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）
    events: (tradeId: string) =>
      api.get<TradeEvent[]>(`/trades/${tradeId}/events`).then((r) => r.data),
  },

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
    // 手动刷新：强制拉最新 K 线并重跑信号判定；命中返回最新信号（未命中保留原显示）
    refresh: (symbol: string) =>
      api
        .post<{
          symbol: string;
          updated_at: string;
          kline_count: number;
          last_close: number | null;
          hit: boolean;
          signal_count: number;
          latest_scan: ScanResult | null;
        }>(`/watchlist/${symbol}/refresh`)
        .then((r) => r.data),
  },
}
