export interface ScanRecord {
  id: string;
  scan_type: string;
  status: string;
  coin_count: number;
  hit_count: number;
  error_count: number;
  started_at: string;
  finished_at: string | null;
  created_at: string;
}

export interface ScanResult {
  id: string;
  scan_record_id: string;
  symbol: string;
  signal_type: string;
  current_price: number;
  breakout_pct: number;
  trend_slope: number;
  r_squared: number;
  pattern: string | null;
  signal_reason: string | null;
  ema_state: string | null; // 均线形态状态（bullish_align / bearish_cross 等）
  position: string | null; // 信号位置（形态/突破方向派生：支撑位/压力位）
  volume_24h: number;
  volume: number;
  volume_type: string;
  quality_score: number | null; // 指标驱动筛选：候选质量分 0-100（AI 配额排序键）
  votes_json: { // 指标票数明细（7 指标多数决）
    bull: string[];
    bear: string[];
    bull_n: number;
    bear_n: number;
    net: number;
    voted: number;
  } | null;
  is_repeat: boolean;
  created_at: string;
}

export interface ScanConfig {
  interval_hours: number;
  kline_interval: string;
  window: number;
  breakout_threshold: number;
  r_squared_threshold: number;
  repeat_window_hours: number;
}

export interface ScanStatus {
  last_scan: ScanRecord | null;
  is_scanning: boolean;
  config: ScanConfig;
  // 交易运行状态（2026-09-20 顶部一览）
  trading_enabled: boolean;
  trading_testnet: boolean;
  open_trades: number;
  max_open_trades: number;
  ai_analysis_enabled: boolean;
}

export interface AccountSummary {
  testnet: boolean;
  available: boolean; // false=交易所请求失败，降级显示
  wallet?: number; // 钱包余额（不含未实现盈亏）
  available_balance?: number;
  unrealized?: number; // 未实现盈亏
  margin_balance?: number; // 总资金 = 钱包 + 未实现盈亏
  realized?: number; // 累计已实现盈亏（当前环境全部闭单）
  profit?: number; // 总收益 = 已实现 + 未实现
  profit_rate?: number | null; // 收益率% = 总收益 ÷ 本金（本金=总资金−总收益）
  today_realized?: number; // 今日已实现盈亏（北京自然日闭单口径）
  today_pnl_pct?: number | null; // 今日盈亏%（已实现+未实现）÷ 期初钱包（当前钱包−今日已实现）
}

export interface ListResponse<T> {
  items: T[];
  total: number;
  page?: number;
  page_size?: number;
}

// 任务记录：任务粒度的运行历史（扫描/交易/复盘等），见后端 task_records
export interface TaskRecord {
  id: string;
  task_type: 'scan' | 'ai' | 'trade' | 'review';
  task_name: string;
  trigger: 'scheduled' | 'manual' | 'system';
  status: 'running' | 'completed' | 'failed' | 'skipped';
  summary: string | null;
  detail: Record<string, unknown> | null;
  error: string | null;
  scan_record_id: string | null; // scan 任务关联的扫描记录（点击加载结果）
  started_at: string;
  finished_at: string | null;
}

// 系统日志：应用 WARNING 及以上日志（定时任务/接口/交易结算等报错）
export interface AppLog {
  id: number;
  level: 'WARNING' | 'ERROR' | 'CRITICAL';
  logger_name: string;
  message: string;
  created_at: string;
}

export interface AIAnalysis {
  id: string;
  scan_result_id: string | null; // 手动搜索分析时为 null
  symbol: string;
  trade_decision: string | null; // 'suggest' | 'skip'
  skip_reason: string | null;
  direction: string | null; // 'long' | 'short'
  trade_type: string | null; // 开单类型：trend_follow / rule_123 / n_structure / rule_2b / range_edge
  analysis: string | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit_1: number | null;
  take_profit_2: number | null;
  risk_reward_ratio: number | null;
  position_pct: number | null;
  recommendation: number | null; // 0-100
  pullback_wait?: boolean; // AI 建议等回踩（入场在现价回踩侧）——仅展示"待回踩"标识，不下单
  open_block_reason?: string | null; // 未开单原因（suggest 被开仓闸门拦下时后端落库）
  created_at: string;
  review_status?: string | null; // 复盘结果：win_tp1 / win_tp2 / loss / expired（24h 后回放定论）
}

export interface WatchlistItem {
  id: string;
  symbol: string;
  note: string | null;
  created_at: string;
  // K 线最近一次手动刷新时间（刚添加时等于 created_at）
  updated_at: string | null;
  // 该币种最近一次扫描命中结果（EMA/趋势等列的数据来源）
  latest_scan: ScanResult | null;
}

export interface Kline {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SwingPoint {
  time: number; // ms 时间戳
  price: number;
  label: string; // HH / LH / H / HL / LL / L
}

export interface KlineData {
  symbol: string;
  interval: string;
  klines: Kline[];
  swings?: { highs: SwingPoint[]; lows: SwingPoint[] };
}

export interface SystemConfig {
  ai_analysis_enabled: boolean;
  ai_pipeline_enabled: boolean;
  ai_configured: boolean;
  strategy_prompt_enabled: boolean;
  strategy_prompt: string;
  kline_interval: string;
  kline_window: number;
  breakout_threshold: number;
  r_squared_threshold: number;
  repeat_window_hours: number;
  swing_order: number;
  pullback_tolerance: number;
  fib_enabled?: boolean; // 斐波那契位开关（二期）
  dual_judge_enabled?: boolean; // 双评委辩论：对 suggest 决策做多空辩论复核
  max_open_trades?: number; // 自动交易：同时在跑单子上限
  trading_min_free_pct?: number; // 开仓最低可用余额占钱包比例（0~1，原 70% 规则）
  max_stop_pct?: number | null; // 规则补丁参数：止损宽度硬上限（比例 0.04=4%；null=不限制）
  liq_gate_buffer?: number | null; // 规则补丁参数：强平闸门缓冲（比例 0.005=0.5%；null=默认）
  min_adapt_leverage?: number | null; // 动态杠杆下限（整数倍数；null=默认 8，宽止损单限制层）
  pullback_market_pct?: number | null; // 入场偏离容差（比例 0.01=1%；null=默认；待回踩距离 ≤ 容差时市价入场）
  max_slippage_pct?: number | null; // 开仓滑点实测上限（比例 0.003=0.3%；null=默认；按本单数量走订单簿计算）
  // 运行时环境与总开关（2026-09-20 由 env 迁移进 DB，页面「系统配置」弹窗可改）
  trading_testnet?: boolean; // 当前交易环境：true=测试网 false=正式网
  trading_enabled?: boolean; // 自动交易总开关（关=定时扫描 AI 分析跳过+停开新单，结算/复盘/日报照常）
  binance_key_live?: boolean; // 正式网 API key 已配置（只读状态，不返回 key）
  binance_key_testnet?: boolean; // 测试网 API key 已配置（未配置时回落正式网 key）
}

// ===== 币种黑名单（手动/自动）=====

export interface BlacklistItem {
  id: string;
  symbol: string;
  origin: 'manual' | 'auto'; // 手动（永久）/ 止损严重超预算自动（冷却期）
  reason: string;
  expires_at: string | null; // auto 冷却到期时间；manual 恒 null
  expired: boolean; // auto 已过期（自然失效）
  active: boolean; // 当前生效（过滤扫描中）
  disabled: boolean;
  created_at: string | null;
}

// ===== 规则补丁（docs/12 §10）=====

export type PatchStatus = 'draft' | 'active' | 'valid' | 'invalid' | 'rolled_back'
export type PatchVerdict = 'pending' | 'valid' | 'invalid' | 'worse'

export interface PatchMetrics {
  window_days?: number
  closed_trades?: number
  attributed_total?: number
  tag_counts?: Record<string, number>
  heat?: Record<string, number>
  ev_per_trade?: number
  win_rate?: number
  pass_rate?: number
}

export interface Patch {
  id: string
  target_tag: string
  origin: 'ai' | 'manual'
  kind: 'prompt' | 'param'
  param_key: string
  old_text: string // 系统原文段落引用（仅展示）
  new_text: string
  param_value: string
  evidence_json?: { heat_7d?: number; attributed_total_7d?: number; case_trade_ids?: string[] } | null
  metrics_before_json?: PatchMetrics | null
  metrics_after_json?: { after?: PatchMetrics; checks?: string[]; evaluated_days?: number } | null
  verdict: PatchVerdict
  status: PatchStatus
  applied_at: string | null
  created_at: string
}

export interface PatchListResponse {
  items: Patch[]
  stats: PatchMetrics // 当前实时窗口统计
  rule_tags: string[]
}

export interface PromptView {
  system_prompt: string
  agent_system: string
  user_custom: string
  patch_segments: {
    marker: string
    tag: string
    patch_id: string | null
    status: PatchStatus | 'orphan'
    verdict: PatchVerdict | null
    applied_at: string | null
    text: string
  }[]
  params: { max_stop_pct: number | null; liq_gate_buffer: number | null }
}

// ===== 自动交易（docs/06）=====

export interface TradeRecord {
  id: string
  symbol: string
  direction: 'long' | 'short'
  recommendation: number | null
  entry_price: number | null
  qty: number | null
  notional: number | null
  leverage: number
  margin_used: number | null
  risk_amount: number | null
  stop_loss: number | null
  tp1: number | null
  tp2: number | null
  status: 'OPENED' | 'TP1_HIT' | 'TP2_HIT' | 'CLOSED' | 'FAILED'
  opened_at: string | null
  closed_at: string | null
  realized_pnl: number | null // 正/负值 USDT（净额：含手续费/资金费；运行中=已止盈部分，结算后=全程净额）
  unrealized_pnl: number | null // 浮动盈亏快照（运行中每小时 :10 巡检刷新，docs/16；终态 0）
  pnl_pct: number | null // 相对止损金额 %（运行中=（已实现+浮动）/risk_amount）
  exit_reason: string | null // sl / tp1_then_sl / tp1_trail / trail_sl / breakeven_sl / liquidation / manual / error
  fail_reason?: string | null // 开仓失败原因（仅 FAILED 记录，后端读自 raw.fail_reason）
  testnet: boolean // 测试网/正式网
  ai_snapshot: AIAnalysis | null // 开单时 AI 分析结论快照
  ai_analysis_id: string | null
  ai_advice: { // AI 持仓诊断（docs/16，浮亏在跑单）
    thesis_valid: boolean // 开仓逻辑是否仍成立
    trend_broken: boolean // 走势是否走坏
    action: 'hold' | 'tighten' | 'reduce' | 'exit'
    reason: string
    detail?: string
  } | null
  ai_advice_at: string | null // 最近诊断时间（NULL=未诊断）
}

// AI 持仓建议动作元数据（列表 Tag 颜色，docs/16）
export const ADVICE_ACTION_META: Record<string, { label: string; color: string }> = {
  hold: { label: '继续持有', color: 'blue' },
  tighten: { label: '收紧止损', color: 'orange' },
  reduce: { label: '建议减仓', color: 'gold' },
  exit: { label: '建议离场', color: 'red' },
}

export interface TradeEvent {
  id: string
  event_type: string // OPEN / TP1_FILL / TP2_FILL / SL_MOVE / SL_FILL / CANCEL / ERROR / SETTLE / SKIP
  detail: Record<string, unknown> | null
  created_at: string
}

// 一键清空测试网数据（预检/执行共用返回；executed=true 表示已删除）
export interface PurgeStats {
  records: number
  events: number
  transfers: number
  running: number
  executed: boolean
}

// 合约账户资金划转（每小时巡检同步自币安 income TRANSFER）
export interface AccountTransfer {
  id: string
  tran_id: string
  asset: string
  amount: number // 正=转入（现货→合约），负=转出（合约→现货）
  transfer_time: string
}

// ===== 交易复盘K线（交易记录弹窗）=====

// 复盘关键事件（图上打箭头）：开仓 / 止盈成交 / 止损成交
export interface TradeKlineEvent {
  event_type: 'OPEN' | 'TP1_FILL' | 'TP2_FILL' | 'SL_FILL'
  created_at: string
  detail: Record<string, unknown> | null
}

export interface TradeKlineData {
  trade_id: string
  symbol: string
  direction: 'long' | 'short'
  interval: string
  status: TradeRecord['status']
  exit_reason: string | null
  entry_price: number | null // 开单时初始值（OPEN 事件快照）
  stop_loss: number | null
  current_stop_loss: number | null // 跟踪止损现值（SL_MOVE 后更新；与初始止损不同时弹窗加画现止损线）
  tp1: number | null
  tp2: number | null
  opened_at: string | null
  closed_at: string | null
  klines: Kline[]
  events: TradeKlineEvent[]
}

// ===== AI 建议复盘系统（P2）=====

// 单个维度分组的复盘统计（groups 数组每个元素只有一个维度字段非空）
export interface ReviewGroupStat {
  signal_type: string | null;
  position: string | null;
  ema_state: string | null;
  total: number;
  win_tp1: number;
  win_tp2: number;
  loss: number;
  expired: number;
  win_rate: number;
  ev_r: number | null; // 期望值（R 倍数，含费用假设）
}

// 复盘统计汇总（expired 不计入胜率分母）
export interface ReviewStats {
  days: number;
  total: number;
  win_tp1: number;
  win_tp2: number;
  loss: number;
  expired: number;
  win_rate: number;
  ev_r: number | null;
  groups: ReviewGroupStat[];
}

// ===== 实盘归因（复盘重设计 2026-09-18）=====

// 实盘总览统计
export interface ActualOverview {
  days: number;
  count: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl: number;
  avg_win: number;
  avg_loss: number;
  payoff: number | null;
  ev_per_trade: number;
  avg_hold_hours: number;
  total_fee: number;
  total_funding: number;
  planned_risk_total: number;
  plan_gap: number;
  attributed: number;
}

// 实盘分组统计（dim = 开单类型/方向/出场原因/开仓时段/错误标签）
export interface ActualGroupStat {
  dim: string;
  key: string;
  count: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl: number;
  avg_win: number;
  avg_loss: number;
  payoff: number | null;
  ev_per_trade: number;
}

// 实盘逐笔明细（计划 vs 实际 + AI 归因）
export interface ActualTradeItem {
  id: string;
  symbol: string;
  direction: string | null;
  trade_type: string;
  exit_reason: string | null;
  entry_plan: number | null;
  sl_plan: number | null;
  tp1_plan: number | null;
  entry_actual: number | null;
  realized_pnl: number | null;
  pnl_pct: number | null;
  risk_amount: number | null;
  fee_total: number | null;
  funding_total: number | null;
  recommendation: number | null;
  opened_at: string | null;
  closed_at: string | null;
  hold_hours: number;
  error_tags: string[];
  error_lesson: string | null;
  attributed: boolean;
}

export interface ActualReview {
  overview: ActualOverview;
  groups: ActualGroupStat[];
  items: ActualTradeItem[];
}

export interface AttributionResult {
  processed: number;
  results: { symbol: string; ok: boolean; tags?: string[]; lesson?: string; error?: string }[];
}

// 技能库条目（列表不含 body，详情接口返回全文）
export interface SkillInfo {
  name: string;
  description: string;
  use_when: string;
  version: string;
  body?: string;
}

// Agent 工具循环轨迹（stage_trace 为 null 表示单次调用管线生成）
export interface StageTrace {
  rounds: number
  tool_calls: number
  elapsed_ms: number
  steps: { round: number; llm_ms: number; tools: string[]; calls?: { tool: string; args: Record<string, string>; result_len: number; ms: number }[] }[]
}

// AI 分析进度事件（后端 Redis 事件流，前端 2s 轮询）
export interface AiProgressEvent {
  t: 'start' | 'gate' | 'round' | 'tool' | 'done' | 'error'
  ts: number
  symbol?: string
  pipeline?: string
  round?: number
  tools?: string[]
  tool?: string
  args?: string
  note?: string
  decision?: string
}

export interface AiProgress {
  scan_result_id: string
  status: 'done' | 'running' | 'idle'
  events: AiProgressEvent[]
}

// ===== 资金分析（docs/14，对齐后端 FundAnalyticsOut）=====

export interface FundSummary {
  closed: number; wins: number; win_rate: number | null
  net_pnl: number; fee_total: number; funding_total: number
  avg_win_r: number | null
  transfer_in: number; transfer_out: number; transfer_net: number
}
export interface DailyPoint { date: string; pnl: number }
export interface SymbolPnl { symbol: string; pnl: number; count: number; wins: number }
export interface ExitReasonStat { reason: string; count: number; pnl: number }
export interface PnlBucket { bucket: string; count: number }
export interface TransferPoint { date: string; transfer_in: number; transfer_out: number }
export interface FundAnalytics {
  summary: FundSummary
  equity_curve: DailyPoint[]
  daily_pnl: DailyPoint[]
  by_symbol: SymbolPnl[]
  exit_reasons: ExitReasonStat[]
  pnl_distribution: PnlBucket[]
  transfers: TransferPoint[]
}
