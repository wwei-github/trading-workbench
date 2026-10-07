// 合约回测类型（docs/16 P-06）——与后端 backtest_engine.aggregate 输出逐键对齐

// 出场腿（replay_exit.legs）
export interface ReplayLeg {
  name: string // tp0 / tp1 / tp2 / sl / be_exit / trail
  price: number
  qty_pct: number
  r: number
}

// 单笔回放结果（replay_exit 返回值）
export interface ReplayResult {
  outcome: 'loss' | 'win_tp1' | 'win_tp2' | 'be_exit' | 'open' | 'invalid'
  r: number | null // open 为 null（不进 decided 分母）
  legs: ReplayLeg[]
  sl_price: number | null
  bars: number
  armed_be: boolean
  trail_used: boolean
}

// 基线逐笔订单明细（orders_detail.items）
export interface BacktestOrder {
  symbol: string
  direction: 'long' | 'short'
  entry: number
  stop: number
  tp1: number
  tp2: number
  entry_time_ms: number
  src: string | null // suggest / veto / actual / scan
  outcome: string
  r: number | null
  legs: ReplayLeg[]
  bars: number
}

// 变体统计（_variant_stats，P-04 口径：open/invalid 单列 excluded_open）
export interface VariantStats {
  name: string
  overrides?: Record<string, number | boolean | null>
  n: number
  decided: number
  excluded_open: number
  exp_r: number | null
  sum_r: number
  win_rate: number | null
  full_loss_rate: number | null
  avg_bars: number | null
  outcome_dist: Record<string, number>
}

// 配对 delta（双双终局样本）
export interface DeltaStats {
  name: string
  n_pairs: number
  delta_exp_r: number | null
  delta_sum_r: number
  delta_win_rate: number | null
  migration_top: { from: string; to: string; n: number }[]
}

// 组合模拟日点
export interface PortfolioPoint {
  date: string
  equity_r: number
  equity_usdt: number
  closed: number
}

// 组合模拟结果（simulate_portfolio 输出 + name）
export interface PortfolioResult {
  name: string
  final_equity_r: number
  final_equity_usdt: number
  max_dd_r: number
  trades: number
  rejected: { slot_full: number; same_dir_cap: number }
  daily_series: PortfolioPoint[]
}

export interface FuturesBacktestSummary {
  engine_version: string
  data_range: { days: number; cutoff: string; symbols: string[] }
  signals: { n: number }
  variants: VariantStats[]
  deltas: DeltaStats[]
  portfolio: PortfolioResult[]
}

export interface VariantOverride {
  name: string
  overrides: Record<string, number | boolean | null>
}

export interface FuturesBacktestParams {
  source: 'history' | 'scan'
  symbols: string[]
  days: number
  variants: VariantOverride[]
  portfolio: {
    risk_usdt: number
    max_open_trades: number
    same_direction_cap: number
  }
}

export interface FuturesBacktestRecord {
  id: string
  status: 'RUNNING' | 'DONE' | 'FAILED'
  params: FuturesBacktestParams
  summary: FuturesBacktestSummary | null
  daily_series?: { items: PortfolioPoint[] } | null
  orders?: BacktestOrder[] | null
  truncated?: boolean
  error: string | null
  created_at: string | null
  finished_at: string | null
}

// 历史列表项（不含大 JSON）
export interface FuturesBacktestListItem {
  id: string
  params: FuturesBacktestParams
  status: string
  signals: number | null
  variants_n: number
  error: string | null
  created_at: string | null
  finished_at: string | null
}
