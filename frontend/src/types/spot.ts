// 现货屯币类型（docs/18 P0）

export interface SpotScoreItem {
  name: string
  value: number | string | null
  threshold: string
  score: number
}

export interface SpotScoreSection {
  label: string
  items: SpotScoreItem[]
  score: number
}

export interface SpotScore {
  total: number
  env: SpotScoreSection
  position: SpotScoreSection
  timing: SpotScoreSection
}

// 三周期环境状态快照（spot_engine.evaluate_day.env）
export interface SpotEnvState {
  monthly_bear: boolean
  deep_value: boolean
  weekly_above: boolean
  vs_ma12m: number | null
  vs_ma30w: number | null
  ath_drawdown: number | null
  weekly_rsi: number | null
  weekly_macd_golden: boolean | null
  vs_ma200: number | null
}

export interface SpotRules {
  buy_tier: number
  score_lots: number
  final_buy_lots: number
  sell_tier: number
  buy_hits: string[]
  sell_hits: string[]
  gates: string[]
  plan_text: string
}

export type SpotAction = 'BUY' | 'SELL' | 'HOLD'

// 单币单日决策（可解释）
export interface SpotDecision {
  symbol: string
  as_of: string
  price: number
  action: SpotAction
  lots: number
  quote_amount: number | null
  score: SpotScore
  env: SpotEnvState
  rules: SpotRules
}

// 现货配置（system_config.spot_settings JSON 列）
export interface SpotSettings {
  per_order_usdt: number // 单份金额 USDT
  max_total_usdt: number // 单币累计投入上限
  dca_enabled: boolean
  dca_amount: number
  usdt_reserve_pct: number
  fee_rate: number
}

// 持仓总览·单币卡
export interface SpotCoinOverview {
  symbol: string
  asset: string
  qty: number | null // 持有数量（账户查询失败 = null）
  price: number
  market_value: number | null
  score: SpotScore | null
  action: SpotAction | null
  lots: number | null
  quote_amount: number | null
  plan_text: string | null
  env: SpotEnvState | null
}

export interface SpotOverview {
  testnet: boolean
  account_keys: string[] // 现货启用账号 key（多账号聚合持仓）
  account_names: string[]
  coins: SpotCoinOverview[]
  usdt_free: number | null
  settings: SpotSettings
  account_error: string | null
}

// 现货环境与账号配置（与合约完全独立，docs/18）
export interface SpotAccountBrief {
  key: string
  name: string
  testnet: boolean
  configured: boolean
}

export interface SpotConfig {
  spot_trading_testnet: boolean
  spot_enabled_keys: string[] // 预配置/启用的正式网账号
  accounts: SpotAccountBrief[]
}

export interface SpotOrder {
  id: string
  symbol: string
  side: 'BUY' | 'SELL'
  trigger_type: string // signal / dca / manual
  trigger_detail: Record<string, unknown> | null
  qty: number | null
  quote_amount: number | null
  price: number | null
  status: string // PENDING_CONFIRM / FILLED / CANCELLED / FAILED
  fail_reason: string | null
  testnet: boolean
  created_at: string | null
  executed_at: string | null
}

// ===== 运营时间轴（docs/18 §9.2.3，react-chrono 数据源）=====

// order 事件 data
export interface SpotTimelineOrder {
  symbol: string
  side: 'BUY' | 'SELL'
  trigger_type: string
  qty: number | null
  quote_amount: number | null
  price: number | null
  status: string
  fail_reason: string | null
  trigger_detail: Record<string, unknown> | null
  testnet: boolean
  executed_at: string | null
}

// review 事件 data（AI 周度研判）
export interface SpotTimelineReview {
  week: string
  regime: string // 积累期 / 上升期 / 分配期 / 下降期
  outlook: Record<string, string> | null // {BTC: 偏多, ...}
  risks: string[] | null
  commentary: string
}

// daily_check 事件 data（每日评分日检）
export interface SpotTimelineDaily {
  status: string
  summary: string | null
  as_of: string | null
  actions: Record<string, { action: string; lots: number; score: number; plan: string }> | null
  env: Record<string, SpotEnvState> | null
  error: string | null
}

export interface SpotTimelineItem {
  id: string
  type: 'order' | 'review' | 'daily_check'
  time: string
  data: SpotTimelineOrder | SpotTimelineReview | SpotTimelineDaily
}

// ===== 回测（docs/18 §10，P1）=====

export interface BacktestParams {
  symbols: string[]
  start: string
  end: string
  budget: number
  per_order_usdt?: number | null
  dca_enabled?: boolean | null
  dca_amount?: number | null
}

export interface BacktestSummary {
  symbols: string[]
  bars: number
  start: string
  end: string
  budget: number
  total_qty: number
  coin_unit: string
  total_market_value: number
  total_cost: number
  total_realized: number
  cash_left: number
  invested: number
  orders_count: number
  action_dist: Record<string, number>
  benchmark: { invested: number; total_qty: number; qty_per_symbol: Record<string, number> }
  excess_qty: Record<string, number>
  max_drawdown: number
  equity_curve_end?: number
}

export interface BacktestSeriesPoint {
  date: string
  cash: number
  market_value: number
  equity: number
  coin_qty: Record<string, number>
  invested: number
}

export interface BacktestOrder {
  date: string
  symbol: string
  side: 'BUY' | 'SELL'
  trigger: string // signal / dca
  qty: number
  price: number
  quote: number
  fee: number
  score: number
}

export interface BacktestRecord {
  id: string
  status: 'RUNNING' | 'DONE' | 'FAILED'
  params: BacktestParams
  summary: BacktestSummary | null
  daily_series?: BacktestSeriesPoint[] | null
  orders?: BacktestOrder[] | null
  error: string | null
  created_at: string | null
  finished_at: string | null
}
