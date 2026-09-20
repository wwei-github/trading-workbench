import axios from 'axios'
import type { AccountSummary, AccountTransfer, AIAnalysis, AiProgress, AttributionResult, BlacklistItem, FundAnalytics, KlineData, ListResponse, ActualReview, Patch, PatchListResponse, PromptView, PurgeStats, ReviewStats, ScanRecord, ScanResult, ScanStatus, SkillInfo, StageTrace, SystemConfig, TaskRecord, AppLog, TradeEvent, TradeKlineData, TradeRecord, WatchlistItem } from '../types'

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

  results: (scanId: string, page = 1, pageSize = 20, sortBy = 'quality_score', order = 'desc', filters?: ResultFilters) =>
    api
      .get<ListResponse<ScanResult>>(`/scans/${scanId}/results`, {
        params: { page, page_size: pageSize, sort_by: sortBy, order, ...(filters || {}) },
      })
      .then((r) => r.data),

  latestResults: (page = 1, pageSize = 20, sortBy = 'quality_score', order = 'desc', filters?: ResultFilters) =>
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

  // ===== 实盘归因（复盘重设计 2026-09-18）=====

  // 实盘归因统计（trade_records 主轴：总览 + 分组 + 逐笔计划vs实际 + AI 错误标签）
  actualReview: (days = 30) =>
    api
      .get<ActualReview>('/scans/review/actual', { params: { days } })
      .then((r) => r.data),

  // 手动触发 AI 错误归因（tradeId 缺省 = 处理全部待归因 ≤5 笔）
  attributeTrades: (tradeId?: string) =>
    api
      .post<AttributionResult>('/scans/review/attribute',
        tradeId ? { trade_id: tradeId } : {})
      .then((r) => r.data),

  // ===== 规则补丁（docs/12 §10）=====

  // 补丁列表（告警置顶）+ 当前实时窗口统计
  patches: () =>
    api.get<PatchListResponse>('/scans/review/patches').then((r) => r.data),

  // 合入草稿（提示词追加覆盖段 / 参数写列，即时生效）
  applyPatch: (id: string) =>
    api.post<Patch>(`/scans/review/patches/${id}/apply`).then((r) => r.data),

  // 回滚（提示词删标记段 / 参数恢复默认，即时生效）
  rollbackPatch: (id: string) =>
    api.post<Patch>(`/scans/review/patches/${id}/rollback`).then((r) => r.data),

  // 删除草稿（仅 draft 可删）
  deletePatch: (id: string) =>
    api.delete(`/scans/review/patches/${id}`).then((r) => r.data),

  // 手动触发一次草稿生成（立即调 LLM）；未生成时 skips 给出各关判定明细
  triggerPatches: () =>
    api
      .post<{
        created: number;
        skips: string[];
        attributed_total_7d: number;
        drafts: { id: string; tag: string; kind: string }[];
      }>(
        '/scans/review/patches/trigger',
      )
      .then((r) => r.data),

  // 当前生效提示词分层视图（系统自带/用户自定义/补丁覆盖段 + 参数）
  promptView: () =>
    api.get<PromptView>('/scans/review/prompt-view').then((r) => r.data),

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

  // ===== 币种黑名单（手动/自动）=====

  blacklist: {
    list: () => api.get<BlacklistItem[]>('/scans/blacklist').then((r) => r.data),
    add: (symbol: string, reason?: string) =>
      api
        .post<{ id: string; symbol: string }>('/scans/blacklist', { symbol, reason: reason || '' })
        .then((r) => r.data),
    remove: (symbol: string) =>
      api.delete<{ ok: boolean }>(`/scans/blacklist/${encodeURIComponent(symbol)}`).then((r) => r.data),
  },

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
    // 交易复盘K线（开单前100根 + 持仓期间，含初始止损止盈与关键事件）
    klines: (tradeId: string) =>
      api.get<TradeKlineData>(`/trades/${tradeId}/klines`, { timeout: 60000 }).then((r) => r.data),
    // 合约账户资金划转（正=转入/负=转出，每小时巡检同步）
    transfers: (limit = 200) =>
      api.get<AccountTransfer[]>('/trades/transfers', { params: { limit } }).then((r) => r.data),
    // 资金分析聚合（docs/14，start/end 为北京时间 YYYY-MM-DD 闭区间）
    analytics: (params: { scope: 'live' | 'testnet'; start?: string; end?: string }) =>
      api.get<FundAnalytics>('/trades/analytics', { params }).then((r) => r.data),
    // 账户资金概览（当前环境：总资金/总收益/收益率，顶部状态栏展示）
    accountSummary: () =>
      api.get<AccountSummary>('/trades/account-summary').then((r) => r.data),
    // 手动 AI 持仓诊断（docs/16，无视冷却）：异步派发立即返回，前端轮询结果
    advice: (tradeId: string) =>
      api.post<{ queued: boolean; symbol: string }>(`/trades/${tradeId}/advice`).then((r) => r.data),
    // 一键清空测试网数据（两阶段：confirm=false 预检，true 执行删除）
    purgeTestnet: (confirm: boolean) =>
      api
        .post<PurgeStats>('/trades/purge-testnet', null, { params: { confirm } })
        .then((r) => r.data),
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

  // ===== 运维观测（任务记录 / 系统日志）=====

  // 任务记录（新→旧；task_type/status 可选过滤）
  tasks: (page = 1, pageSize = 10, taskType?: string, status?: string) =>
    api
      .get<ListResponse<TaskRecord>>('/tasks', {
        params: {
          page,
          page_size: pageSize,
          task_type: taskType || undefined,
          status: status || undefined,
        },
      })
      .then((r) => r.data),

  // 系统日志（新→旧；level/q 可选过滤）
  logs: (page = 1, pageSize = 20, level?: string, q?: string) =>
    api
      .get<ListResponse<AppLog>>('/logs', {
        params: {
          page,
          page_size: pageSize,
          level: level || undefined,
          q: q || undefined,
        },
      })
      .then((r) => r.data),
}
