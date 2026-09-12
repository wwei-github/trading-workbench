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

export interface KeyLevel {
  kind: string; // prev_high / prev_low / support / resistance / range_top / range_bottom
  price: number;
  zone_low: number;
  zone_high: number;
  touches: number;
  role: string; // support / resistance
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
  position: string | null; // 12金K出现的位置（关键位类型）
  key_levels: KeyLevel[] | null; // 命中的关键位明细
  volume_24h: number;
  volume: number;
  volume_type: string;
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
}

export interface ListResponse<T> {
  items: T[];
  total: number;
  page?: number;
  page_size?: number;
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
  key_level_tolerance?: number; // 关键位区域半宽（±x）
  level_merge_threshold?: number; // 支撑/压力聚类合并阈值
  fib_enabled?: boolean; // 斐波那契位开关（二期）
  dual_judge_enabled?: boolean; // 双评委辩论：对 suggest 决策做多空辩论复核
  max_open_trades?: number; // 自动交易：同时在跑单子上限
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
  realized_pnl: number | null // 正/负值 USDT
  pnl_pct: number | null // 相对止损金额 %
  exit_reason: string | null // sl / tp1_then_sl / trail_sl / breakeven_sl / manual / error
  ai_analysis_id: string | null
}

export interface TradeEvent {
  id: string
  event_type: string // OPEN / TP1_FILL / TP2_FILL / SL_MOVE / SL_FILL / CANCEL / ERROR / SETTLE / SKIP
  detail: Record<string, unknown> | null
  created_at: string
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
  groups: ReviewGroupStat[];
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
