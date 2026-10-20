import axios from 'axios'
import type { AccountSummary, AccountTransfer, AIAnalysis, AiProgress, AttributionResult, BlacklistItem, FundAnalytics, FunnelData, HitHistoryItem, KlineData, ListResponse, ActualReview, Patch, PatchListResponse, PendingEntry, PromptView, PullbackPendingItem, PurgeStats, ReviewFunnel, ReviewStats, ScanRecord, ScanResult, ScanStatus, SkillInfo, StageTrace, SystemConfig, TaskRecord, AppLog, TradeEvent, TradeKlineData, TradeRecord, TradeReconciliation, TransferStats, WatchlistItem } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

// 结果列表过滤条件（服务端过滤）
export interface ResultFilters {
  signal_type?: string
  pattern?: string
  ema_state?: string
}

export const scanApi = {
  trigger: () => api.post<{ scan_id: string; status: string }>('/scans').then((r) => r.data),
  // 筛选漏斗明细（2026-10-11）：该轮各关卡被拒计数 + symbol→关卡映射；funnel=null（旧行/异常轮次）
  funnel: (scanId: string) =>
    api.get<{ scan_id: string; funnel: FunnelData | null }>(`/scans/${scanId}/funnel`).then((r) => r.data),
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

  klines: (symbol: string, limit = 100, forceRefresh = false, interval?: '1h' | '4h' | '1d') =>
    api
      .get<KlineData>(`/scans/klines/${symbol}`, {
        params: {
          limit,
          ...(forceRefresh ? { force_refresh: true } : {}),
          ...(interval ? { interval } : {}),
        },
      })
      .then((r) => r.data),

  getConfig: () => api.get<SystemConfig>('/scans/config').then((r) => r.data),

  updateConfig: (data: Partial<SystemConfig>) =>
    api.put<SystemConfig>('/scans/config', data).then((r) => r.data),

  // 月度回撤熔断人工复盘确认（docs/13 #2）：写确认时刻恢复开仓
  ackMonthlyBreaker: () =>
    api.post<{ ok: boolean; ack_at: string }>('/scans/config/monthly-breaker-ack').then((r) => r.data),

  // 周度回撤减速人工复盘确认（2026-10-07，对称月熔断）：写确认时刻恢复常规上限
  ackWeeklyBreaker: () =>
    api.post<{ ok: boolean; ack_at: string }>('/scans/config/weekly-breaker-ack').then((r) => r.data),

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

  // 全漏斗转化率（docs/24 A5c）：信号命中 → AI 分析 → suggest → 过评委 → 开仓 → 盈利
  reviewFunnel: (days = 30, scope = 'all') =>
    api
      .get<ReviewFunnel>('/scans/review/funnel', { params: { days, scope } })
      .then((r) => r.data),

  // ===== 实盘归因（复盘重设计 2026-09-18）=====

  // 实盘归因统计（trade_records 主轴：总览 + 分组 + 逐笔计划vs实际 + AI 错误标签）
  // scope：live/testnet/all 或账号 key（docs/17 口径，缺省 all 兼容旧调用）
  actualReview: (days = 30, scope?: string) =>
    api
      .get<ActualReview>('/scans/review/actual', { params: { days, scope } })
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

  // ===== 回踩两段式待确认池（docs/19）=====

  pullbackPendings: {
    list: (status: 'all' | 'pending' | 'confirmed' | 'expired' | 'superseded' = 'all', limit = 100) =>
      api
        .get<PullbackPendingItem[]>(`/scans/pullback-pendings`, { params: { status, limit } })
        .then((r) => r.data),
  },

  // ===== 命中历史（2026-10-15，docs/22）=====

  // 跨轮次扫描命中历史（默认最近 7 天，start/end 为 UTC ISO8601 闭区间；
  // all_time=true（清空日期选择器）= 不限下界，查全量）
  hitHistory: (params: {
    start?: string
    end?: string
    symbol?: string
    include_repeat?: boolean
    all_time?: boolean
    page?: number
    page_size?: number
    sort_by?: string
    order?: string
  }) => api.get<ListResponse<HitHistoryItem>>('/scans/hit-history', { params }).then((r) => r.data),

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

  // 交易记录列表（新→旧；status 可选过滤；scope 账号范围过滤，docs/17）
  trades: {
    list: (status?: string, limit = 200, scope = 'all') =>
      api
        .get<TradeRecord[]>('/trades', { params: { status, limit, scope } })
        .then((r) => r.data),
    // 挂单入场列表（docs/27，2026-10-17）：现价盈亏比不足时在 AI 入场价挂的限价单
    // status 默认 pending（进行中）；传 '' 不过滤 → 返回全部状态（交易记录页挂单历史用）；
    // scope 同 trades.list
    pendingEntries: (status = 'pending', limit = 100, scope = 'all') =>
      api
        .get<PendingEntry[]>('/trades/pending-entries', { params: { status, limit, scope } })
        .then((r) => r.data),
    // 手动撤销挂单（撤交易所 GTC 限价单 + 标记 canceled）
    cancelPendingEntry: (id: string) =>
      api
        .post<{ ok: boolean; id: string; filled?: boolean; trade_id?: string | null }>(
          `/trades/pending-entries/${id}/cancel`,
        )
        .then((r) => r.data),
    // 一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）
    events: (tradeId: string) =>
      api.get<TradeEvent[]>(`/trades/${tradeId}/events`).then((r) => r.data),
    // 交易复盘K线（开单前100根 + 持仓期间，含初始止损止盈与关键事件）
    // interval 可选（1h/4h），缺省用后端 cfg.kline_interval——弹窗内切换周期
    klines: (tradeId: string, interval?: string) =>
      api
        .get<TradeKlineData>(`/trades/${tradeId}/klines`, {
          params: interval ? { interval } : undefined,
          timeout: 60000,
        })
        .then((r) => r.data),
    // 逐笔 R 泄漏对账（docs/24 A1）：计划 vs 实际 vs 泄漏分解（可能惰性查交易所回补，放宽超时）
    reconciliation: (tradeId: string) =>
      api
        .get<TradeReconciliation>(`/trades/${tradeId}/reconciliation`, { timeout: 60000 })
        .then((r) => r.data),
    // 合约账户资金划转（正=转入/负=转出，每小时巡检逐账号同步）
    // scope：live/testnet 环境聚合、all、或账号 key（docs/17）
    transfers: (scope: string = 'live', limit = 200) =>
      api
        .get<AccountTransfer[]>('/trades/transfers', { params: { scope, limit } })
        .then((r) => r.data),
    // 划转统计（服务端全量聚合 + 初始资金 + 实时钱包余额）
    transferStats: (scope: string = 'live') =>
      api
        .get<TransferStats>('/trades/transfer-stats', { params: { scope } })
        .then((r) => r.data),
    // 资金分析聚合（docs/14，start/end 为北京时间 YYYY-MM-DD 闭区间）
    analytics: (params: { scope: string; start?: string; end?: string }) =>
      api.get<FundAnalytics>('/trades/analytics', { params }).then((r) => r.data),
    // 账户资金概览（账号维度：总资金/总收益/收益率，顶部状态栏展示；
    // 2026-10-03 删环境切换：accountKey 由页面 scope 显式解析传入，
    // 缺省时后端回落 testnet-1）
    accountSummary: (accountKey?: string) =>
      api
        .get<AccountSummary>('/trades/account-summary', {
          params: accountKey ? { account_key: accountKey } : undefined,
        })
        .then((r) => r.data),
    // 手动 AI 持仓诊断（docs/16，无视冷却）：异步派发立即返回，前端轮询结果
    advice: (tradeId: string) =>
      api.post<{ queued: boolean; symbol: string }>(`/trades/${tradeId}/advice`).then((r) => r.data),
    // 一键清空测试网数据（两阶段：confirm=false 预检，true 执行删除）
    purgeTestnet: (confirm: boolean) =>
      api
        .post<PurgeStats>('/trades/purge-testnet', null, { params: { confirm } })
        .then((r) => r.data),
    // 注：手动持仓导入已自动化——每小时结算巡检自动检测并接管 App 手动单
    // 「导入持仓」按钮：立即手动触发一次导入（逻辑与巡检一致）
    adoptNow: () =>
      api
        .post<{ ok: boolean; adopted: string[]; count: number; account_key: string }>(
          '/trades/adopt-now', null, { timeout: 60000 },
        )
        .then((r) => r.data),
    // 「立即巡检」按钮（2026-09-26）：celery 派发即返回，结算/导入/诊断在后台执行
    inspectNow: () =>
      api
        .post<{ ok: boolean; task_id: string; message: string }>(
          '/trades/inspect-now', null, { timeout: 15000 },
        )
        .then((r) => r.data),
    // 扫描结果表「开单」按钮（2026-09-27）：对 AI 分析立即走完整开仓闸门；
    // 2026-10-03 删环境切换：accountKey 必传（前端弹窗选账号），后端不猜网。
    // opened=false 为闸门拦截（业务结果），block_reason 给前端提示
    openFromAnalysis: (analysisId: string, accountKey: string) =>
      api
        .post<{
          ok: boolean;
          opened: boolean;
          pending?: boolean;
          block_reason: string | null;
          trade_id: string | null;
          pending_entry_id?: string | null;
          account_key: string;
        }>(
          '/trades/open-from-analysis',
          { analysis_id: analysisId, account_key: accountKey },
          { timeout: 60000 },
        )
        .then((r) => r.data),
    // 异常单标记（2026-10-18）：人工认定测试网伪盈亏等异常单，标记后资金分析/
    // 复盘统计、熔断闸门、飞书日报全链路剔除该单；行本身保留可审计、可取消
    setExcluded: (tradeId: string, excluded: boolean) =>
      api
        .post<{ id: string; excluded: boolean }>(`/trades/${tradeId}/excluded`, null, {
          params: { excluded },
        })
        .then((r) => r.data),
    // 切换移动止损方式（2026-10-20）：hl=HL/LH 结构跟踪 / three_line=三线止损
    setTrailingMode: (tradeId: string, mode: 'hl' | 'three_line') =>
      api
        .post<{ id: string; trailing_mode: string }>(
          `/trades/${tradeId}/trailing-mode`, null, { params: { mode } },
        )
        .then((r) => r.data),
    // 市价平仓（与飞书卡片按钮同源）：同步执行约 2~5s，立即结算落终态
    closeMarket: (tradeId: string) =>
      api
        .post<{ ok: boolean; symbol: string; pnl: number; pnl_pct: number | null }>(
          `/trades/${tradeId}/close-market`, null, { timeout: 60000 },
        )
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

  // 系统日志（新→旧；level/q/logger 前缀可选过滤）
  logs: (page = 1, pageSize = 20, level?: string, q?: string, logger_name?: string) =>
    api
      .get<ListResponse<AppLog>>('/logs', {
        params: {
          page,
          page_size: pageSize,
          level: level || undefined,
          q: q || undefined,
          logger_name: logger_name || undefined,
        },
      })
      .then((r) => r.data),
}
