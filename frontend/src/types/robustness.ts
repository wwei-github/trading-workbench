// 稳健性扫描类型（docs/16 P-12）
export interface RobustnessVariant {
  name: string
  overrides: Record<string, number | boolean | null>
  decided: number
  win_rate: number | null
  exp_r: number | null
  sum_r: number
  sr: number
  skew: number
  kurt: number
  dsr: number
}

export interface RobustnessPbo {
  n_blocks: number
  n_combinations: number
  pbo: number
  is_best_oos_rank_median: number
  oos_sr_median: number[]
  overfit_combos: number
  excluded_rows: number
}

export interface RobustnessConclusion {
  verdict: 'plateau' | 'peak' | 'mixed' | 'insufficient'
  text: string
  best_is: string
  best_oos: string | null
  best_is_is_best_oos: boolean
}

export interface RobustnessSummary {
  n_trials: number
  n_signals: number
  matrix_rows: number
  excluded_n: number
  pbo: RobustnessPbo | null
  variants: RobustnessVariant[]
  best_is: string
  best_oos: string | null
  conclusion: RobustnessConclusion
  engine_version: string
  data_range: { days: number; symbols: string[]; start?: string; end?: string }
}

export interface RobustnessParams {
  source: 'history' | 'scan'
  symbols: string[]
  days: number
  start?: string | null
  end?: string | null
  n_blocks: number
  grid: Record<string, (number | null)[]> | null
}

export interface RobustnessRecord {
  id: string
  status: 'RUNNING' | 'DONE' | 'FAILED'
  params: RobustnessParams
  summary: RobustnessSummary | null
  error: string | null
  created_at: string | null
  finished_at: string | null
}

export interface RobustnessListItem {
  id: string
  params: RobustnessParams
  status: string
  verdict: string | null
  pbo: number | null
  n_signals: number | null
  error: string | null
  created_at: string | null
  finished_at: string | null
}
