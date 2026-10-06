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
  touch_ref: string | null; // 回踩触点（docs/22：retest/swing/ema21/ema55/ema144）
  position_ref: string | null; // 命中位置文案（回踩=触点位/突破=前高前低轨价；旧行 NULL）
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
    htf?: { // 4h 背景级别快照（旧扫描行无此键）
      state: string | null; // ranging / mixed / bullish / bearish
      label: string | null; // 完整标签（含价格位置与 ADX/间距）
      dir: number; // 1 多 / -1 空 / 0 中性
    };
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
  // 交易运行状态（2026-09-20 顶部一览；2026-10-03 双网独立开关，统计双网合计）
  trading_enabled_testnet: boolean;
  trading_enabled_live: boolean;
  open_trades: number;
  /** 其中已进入止盈止损阶段的（TP1_HIT/TP2_HIT，止损已移保本，不占上限名额） */
  open_tp_hit: number;
  /** 在跑 OPENED 按网计数（2026-10-04 拆分显示；满仓跳过判定=任一网达上限） */
  open_trades_testnet: number;
  open_trades_live: number;
  max_open_trades: number;
  ai_analysis_enabled: boolean;
  /** 各网当日熔断状态（2026-10-04 顶部卡片）：active true=已熔断/false=未熔断/null=未知（钱包失败或未启用） */
  loss_breaker?: Record<'testnet' | 'live', {
    enabled: boolean;
    active: boolean | null;
    day_pnl: number;
    cap: number | null;
    /** 周/月回撤分层（docs/13 #2，2026-10-04）：null=该层未启用 */
    week?: { active: boolean; pnl: number; cap: number } | null;
    /** month.active=触发且未确认；acked=本月已人工复盘确认（放行至月底） */
    month?: { active: boolean; hit: boolean; acked: boolean; pnl: number; cap: number } | null;
  }>;
}

export interface AccountSummary {
  testnet: boolean;
  account_key?: string; // 当前账号（docs/17）
  account_name?: string; // 当前账号显示名
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

// 任务记录：任务粒度的运行历史（扫描/交易/复盘/现货等），见后端 task_records
export interface TaskRecord {
  id: string;
  task_type: 'scan' | 'ai' | 'trade' | 'review' | 'spot';
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
  // 战法命中标注（2026-10-02，仅借鉴）：AI 判定信号符合的战法名 + 判定依据
  tactic_name?: string | null;
  tactic_reason?: string | null;
  created_at: string;
  review_status?: string | null; // 复盘结果：win_tp1 / win_tp2 / loss / expired（24h 后回放定论）
}

// 战法（用户自建战术剧本，仅借鉴标注不做硬闸）
export interface Tactic {
  id: string;
  name: string;
  description: string;
  direction: 'all' | 'long' | 'short';
  scope: 'testnet' | 'live' | 'both';
  enabled: boolean;
  trade_count: number; // 该战法累计被 AI 命中的分析数
  created_at: string | null;
  updated_at: string | null;
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

// 关键位（docs/21）已随功能整体移除（2026-09-28）：KeyLevel 接口不再定义，
// K 线/复盘接口不再携带 key_levels；历史字段按后端惯例不 DROP、前端忽略

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
  // 止损金额模式（2026-09-27）：pct=风险基数分档×3%（默认）/ fixed=固定 USDT 金额
  risk_budget_mode?: 'pct' | 'fixed';
  risk_fixed_amount?: number; // fixed 模式的单笔止损金额（USDT，默认 3）
  max_stop_pct?: number | null; // 规则补丁参数：止损宽度硬上限（比例 0.04=4%；null=不限制）
  liq_gate_buffer?: number | null; // 规则补丁参数：强平闸门缓冲（比例 0.005=0.5%；null=默认）
  min_adapt_leverage?: number | null; // 动态杠杆下限（整数倍数；null=默认 8，宽止损单限制层）
  pullback_market_pct?: number | null; // 入场偏离容差（比例 0.01=1%；null=默认；待回踩距离 ≤ 容差时市价入场）
  max_slippage_pct?: number | null; // 开仓滑点实测上限（比例 0.003=0.3%；null=默认；按本单数量走订单簿计算）
  // 风控参数（2026-10-02 上页面）：null=回落 env 默认，页面留空即恢复默认
  min_recommendation?: number | null; // 开单评分门槛（0~100，env 默认 60）
  daily_loss_stop_pct?: number | null; // 当日亏损熔断阈值（% 值，9=风险基数 9%；null=默认）
  daily_loss_stop_enabled?: boolean | null; // 当日亏损熔断开关（null=未定制按 env 默认开；页面两态化后保存即写布尔）
  ai_rr_min?: number | null; // 盈亏比铁律下限（0.5~10，env 默认 1.5）
  risk_pct?: number | null; // 单笔风险比例（% 值，0.1~20；pct 模式风险基数分档 × 此比例）
  leverage_max?: number | null; // 杠杆上限（1~125，env 默认 20）
  taker_fee_rate?: number | null; // taker 手续费率（比例 0.0005=0.05%；保本止损复算）
  near_anchor_factor?: number | null; // 近锚降级风险折价系数（0~1，env 默认 0.7）
  portfolio_heat_cap?: number | null; // 组合热度上限（比例 0.2=钱包 20%；0=关闭）
  same_direction_cap?: number | null; // 同向持仓上限（单数，0=不限）
  btc_crash_pause_pct?: number | null; // BTC 急跌暂停阈值（% 值，1h 3根累计跌幅；0=关闭）
  weekly_dd_stop_pct?: number | null; // 周回撤减速阈值（% 值，UTC 周起净亏 ≥ 基数×此值 → 单笔预算减半；0=关闭）
  monthly_dd_stop_pct?: number | null; // 月回撤熔断阈值（% 值，UTC 月起净亏 ≥ 基数×此值 → 停开新仓；0=关闭）
  monthly_breaker_ack_at?: string | null; // 月熔断人工确认时刻（只读，熔断卡片「确认复盘」写）
  atr_trail_k?: number | null; // ATR 跟进倍数（Chandelier，默认 3；0=关闭 ATR 腿）
  // 自动交易开关（2026-09-20 由 env 迁移进 DB，页面「系统配置」弹窗可改；
  // 2026-10-03 总开关拆分为双网独立开关可同时开启；同日删环境切换）
  trading_enabled_testnet?: boolean; // 测试网自动交易（默认开）
  trading_enabled_live?: boolean; // 正式网自动交易（默认关，实盘安全基线）
  // 多账号（docs/17 P0/P1）：.env 注册表只读展示 + 主账号 + 启用自动交易的 live 账号集合
  current_account_key?: string | null;
  live_trading_keys?: string[]; // P1 扇出：正式网自动交易开关开启时扇出的账号（空=回落 live-1）
  accounts?: AccountBrief[];
  binance_key_live?: boolean; // 正式网 API key 已配置（只读状态，不返回 key）
  binance_key_testnet?: boolean; // 测试网 API key 已配置（未配置时回落正式网 key）
  // 筛选参数组（2026-09-28）：scan_filters=库内定制值（null=回落 env/代码默认）；
  // scan_filters_effective=实际生效值（后端解析结果），表单初值用它。
  // 2026-09-28 增量：allowed_triggers（数组）
  scan_filters?: Record<string, number | string[] | boolean> | null;
  scan_filters_effective?: Record<string, number | string[] | boolean>;
}

// 账号注册表条目（docs/17）：key 稳定标识（testnet-1/live-N），只读不回传凭据
export interface AccountBrief {
  key: string;
  name: string;
  testnet: boolean;
  configured: boolean;
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

// ===== 回踩两段式待确认池（docs/19）=====

export interface PullbackPendingItem {
  id: string;
  symbol: string;
  direction: 1 | -1; // 1 多 / -1 空
  touch_ref: string | null; // Stage-1 命中触点（docs/22：retest/swing/ema21/55/144；旧行 NULL）
  stage1_candle_open: string | null; // Stage-1 触碰K线开盘时间（锚点）
  ema21: number | null; // 触碰时 EMA21
  quality_score: number | null;
  votes_json: Record<string, any> | null;
  status: 'pending' | 'confirmed' | 'expired' | 'superseded';
  resolve_reason: string | null; // window_expired / direction_flip / breakout / same_candle_confirm
  confirm_pattern: string | null; // 确认形态（confirmed 行）
  confirmed_scan_result_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
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
  qty_tp1?: number | null // TP1 分批数量（后端读自 raw；旧记录无此键走 75%/50% 比例回退）
  qty_tp2?: number | null // TP2 分批数量（无 TP2 单为 0/None；旧记录走 25% 比例回退）
  notional: number | null
  leverage: number
  margin_used: number | null
  risk_amount: number | null
  risk_budget_mode?: string | null // 开仓时止损金额模式快照：pct/fixed（null=旧行按 pct 理解）
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
  signal_type?: string | null // 触发信号类型（trigger_breakout 等；null=导入持仓/旧行）
  pattern?: string | null // 触发K线形态（hammer 等；突破类为 null）
  testnet: boolean // 测试网/正式网
  account_key?: string | null // 开单账号（docs/17：testnet-1/live-N；null=旧行按 testnet 回落）
  ai_snapshot: AIAnalysis | null // 开单时 AI 分析结论快照
  ai_analysis_id: string | null
  htf?: { // 4h 背景快照（经 AI分析→扫描结果 votes_json 取，旧数据无此字段）
    state: string | null
    label: string | null
    dir: number
  } | null
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

// 划转统计（服务端全量聚合 + 初始资金 + 实时钱包余额；2026-09-26）
export interface TransferStats {
  tin: number
  tout: number
  net: number
  initial_capital: number
  wallet_balance: number
  available: boolean // false=钱包余额查询全部失败，前端降级「--」
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
  touch_ref: string | null; // 回踩触点（docs/22：与 signal_type 联合分组，null=无触点行）
  position: string | null;
  ema_state: string | null;
  rule_version: string | null; // 规则版本分组维度（docs/24 A0，null=非版本组）
  is_veto: boolean | null; // 双评委否决单回放标记（docs/24 A5a，null=非 veto 组）
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
  // 胜率口径说明（docs/24 A2：建议质量口径 vs 实盘净胜率）
  win_rate_note: string;
  // 规则版本分组（docs/24 A0）：每版本一行，legacy=标记上线前的旧行
  rule_version_groups: ReviewGroupStat[];
  // 双评委否决对比（docs/24 A5a）：被否决建议的假想回放统计 + 对比说明文字
  veto_stats: ReviewGroupStat | null;
  veto_note: string;
}

// 全漏斗阶段（docs/24 A5c）：conversion = 相对上一级转化率，首级为 null
export interface FunnelStage {
  stage: string;
  count: number;
  conversion: number | null;
  note: string;
}

// 全漏斗转化率（信号命中 → AI 分析 → suggest → 过评委 → 开仓 → 盈利）
export interface ReviewFunnel {
  days: number;
  scope: string;
  stages: FunnelStage[];
  // 闸门拦截原因 top3（定位开仓级流失主因）
  block_reasons_top: { reason: string; count: number }[];
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
  /** 双网镜像单笔数（同一 AI 决策双网都开了；all 口径非 0，2026-10-04） */
  mirror_trades: number;
}

// 实盘分组统计（dim = 开单类型/方向/出场原因/开仓时段/网络/错误标签）
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
  stop_anchor: string | null; // 止损锚类型（docs/23）：structure/near
  volume_ratio: number | null; // 量比（docs/24 A5b）：信号K线量 ÷ 近20根基准量
  rule_version: string | null; // 逐笔规则集快照（docs/24 A0），legacy=标记前旧行
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

// ===== 逐笔 R 泄漏对账（docs/24 A1）=====

// 泄漏项双口径：不利 = 正 / 有利 = 负；数据缺失为 null
export interface ReconLeakItem {
  usdt: number | null;
  r: number | null;
}

export interface TradeReconciliation {
  trade_id: string;
  symbol: string;
  direction: string | null;
  status: string;
  exit_reason: string | null;
  account_key: string | null;
  testnet: boolean;
  plan: {
    source: string; // ai_snapshot = AI 快照 / record = 记录现值
    entry: number | null;
    sl: number | null;
    tp1: number | null;
    tp2: number | null;
    qty_total: number | null;
    qty_tp1: number | null;
    qty_tp2: number | null;
    risk_amount: number | null;
    rr1: number | null;
  };
  actual: {
    entry_price: number | null;
    qty: number | null;
    slippage_pct: number | null;
    qty_tp1: number | null;
    qty_tp2: number | null;
    tp1_fill: { price: number | null; qty: number | null; pnl: number | null; time: string | null } | null;
    sl_fill: { trigger_price: number | null; price: number | null; qty: number | null; time: string | null } | null;
    fee_total: number | null;
    funding_total: number | null;
    realized_pnl: number | null;
  };
  leakage: {
    // a)~e) 泄漏分解（成本口径：不利 = 正 / 有利 = 负）
    entry_slip: ReconLeakItem | null;
    fee: ReconLeakItem | null;
    funding: ReconLeakItem | null;
    sl_breach: ReconLeakItem | null;
    tp1_slip: ReconLeakItem | null;
    // f) 汇总：理论 R（TP1 档位 × RR1）vs 实际 R，泄漏 = 理论 − 实际
    summary: {
      theoretical_usdt: number | null;
      theoretical_r: number | null;
      actual_usdt: number | null;
      actual_r: number | null;
      leakage_usdt: number | null;
      leakage_r: number | null;
      covered_usdt: number | null;
      covered_r: number | null;
      unexplained_usdt: number | null;
    };
  };
  gaps: string[];
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
// llm = 流式增量活进度（≥2s 一条，面板只保留最新一条，2026-09-30 方案 B）
export interface AiProgressEvent {
  t: 'start' | 'gate' | 'round' | 'tool' | 'llm' | 'done' | 'error'
  ts: number
  symbol?: string
  pipeline?: string
  round?: number
  tools?: string[]
  tool?: string
  args?: string
  note?: string
  decision?: string
  attempt?: number
  reasoning_chars?: number
  content_chars?: number
  elapsed_s?: number
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

// ── 大盘环境/市场情绪（Header 大盘条，GET /api/market/overview）──
export interface FearGreed {
  value: number // 0-100，越小越恐惧
  label: string // alternative.me 原文（Extreme Fear / Fear / Neutral / Greed / Extreme Greed）
}
export interface BreadthItem {
  ema_state: string | null // bullish_align / bearish_cross / mixed 等
  ema_label: string | null // 中文标签（多头排列/纠缠…）
  change_24h_pct: number // 24h 涨跌幅（%）
}
export interface MarketOverview {
  fear_greed: FearGreed | null
  breadth: Record<string, BreadthItem> | null // key: BTCUSDT / ETHUSDT
}

// ===== 影子模式（docs/14，2026-10-04：只计算+打标+记录，不参与决策）=====

// 影子单桶统计（n=标记数；counted=有效归因数 win/loss；win_rate 基于 counted）
// CARD-05/06（反事实口径）没有 counted/win_rate，改用 decided/sum_delta_r 组
export interface ShadowBucketStat {
  bucket: string
  n: number
  counted?: number
  win_rate?: number | null
  insufficient?: boolean // n < 30：样本不足，仅供参考
}

// 早期保护反事实统计（docs/15 §五：CARD-05/06，trade_record 宿主）
export interface ShadowProtectStat {
  decided: number // |ΔR|>0.05R 的笔数（saved+injured，预注册判定基数）
  sum_delta_r: number // 净改善 ΣΔR（正=规则救回多于误伤）
  avg_real_r: number | null // 平均真实收益（R）
  avg_cf_r: number | null // 平均反事实收益（R）
}

// 单特征影子评估报告（baseline = 全部桶合计）
export interface ShadowFeatureReport {
  feature: string
  name: string
  card: string // 预注册标准卡（docs/14 §七：CARD-01 等）
  min_sample: number // 达标所需最小有效归因样本
  window_days: number
  baseline: { n: number; counted?: number; win_rate?: number | null } & Partial<ShadowProtectStat>
  buckets: (ShadowBucketStat & Partial<ShadowProtectStat>)[]
  // CARD-05/06 专用：测试网对照组（分层统计，不进判定）
  testnet_baseline?: ShadowProtectStat & { n: number; buckets: Record<string, number> }
  decided?: number // CARD-05/06：正式网 decided（= baseline.decided）
  sufficient: boolean // 有效归因样本 ≥ min_sample（达标 ≠ 升格，升格走人工流程）
}
