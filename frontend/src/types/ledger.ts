// 虚拟账本类型（docs/16 P-07）——与后端 app/api/ledger.py 输出逐键对齐
// 口径（P-04）：decided = status=DONE；OPEN/EXPIRED 不进任何百分比分母

export interface LedgerOverall {
  decided: number
  wins: number
  losses: number
  be_exits: number
  win_rate: number | null // decided=0 时 null
  exp_r: number | null // decided 净 R 均值（含费）
  sum_r: number
  open: number
  expired: number
}

// 分桶统计（signal_type / direction / quality 分位 / rule_version 通用）
export interface LedgerBucket {
  key: string
  n: number
  wins: number
  win_rate: number
  exp_r: number
  sum_r: number
}

export interface LedgerStats {
  overall: LedgerOverall
  by_signal_type: LedgerBucket[]
  by_direction: LedgerBucket[]
  by_quality: LedgerBucket[] // <60 / 60-69 / 70-79 / 80+ / 未知
  by_rule_version: LedgerBucket[] // 跨版本不混算
}

// 账本明细行（_serialize 全字段）
export interface LedgerEntry {
  id: string
  scan_result_id: string | null // live 行关联 ScanResult；backfill 行 null
  symbol: string
  direction: 'long' | 'short'
  source: 'live' | 'backfill'
  signal_type: string
  touch_ref: string | null
  pattern: string | null
  position: string | null
  ema_state: string | null
  quality_score: number | null
  votes_net: number | null
  entry: number
  stop: number
  tp1: number
  tp2: number | null
  r_unit: number
  entry_bar_open: string | null // 信号K开盘（naive UTC ISO）
  status: 'OPEN' | 'DONE' | 'EXPIRED'
  outcome: string | null // loss / win_tp1 / win_tp2 / be_exit；未决 null
  r: number | null // 含费净 R；未决 null
  bars: number | null
  decided_at: string | null
  ai_decision: string | null // suggest / skip（N-02 标注）
  ai_recommendation: number | null
  rule_version: string | null
}

export interface LedgerEntriesResp {
  items: LedgerEntry[]
  total: number
  page: number
  page_size: number
}
