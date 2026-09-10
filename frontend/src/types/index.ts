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
  analysis: string | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit_1: number | null;
  take_profit_2: number | null;
  risk_reward_ratio: number | null;
  position_pct: number | null;
  recommendation: number | null; // 0-100
  created_at: string;
}

export interface WatchlistItem {
  id: string;
  symbol: string;
  note: string | null;
  created_at: string;
}

export interface Kline {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface KlineData {
  symbol: string;
  interval: string;
  klines: Kline[];
}

export interface SystemConfig {
  ai_analysis_enabled: boolean;
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
}
