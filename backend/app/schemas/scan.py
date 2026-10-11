from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from uuid import UUID


class ScanRecordOut(BaseModel):
    id: UUID
    scan_type: str
    status: str
    coin_count: int
    hit_count: int
    error_count: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScanResultOut(BaseModel):
    id: UUID
    scan_record_id: UUID
    symbol: str
    signal_type: str
    current_price: float
    breakout_pct: float
    trend_slope: float
    r_squared: float
    pattern: Optional[str] = None
    signal_reason: Optional[str] = None
    touch_ref: Optional[str] = None  # 回踩触点（docs/22：retest/swing/ema21/55/144）
    position_ref: Optional[str] = None  # 命中位置文案（回踩触点位/突破前高前低轨；旧行 NULL）
    ema_state: Optional[str] = None  # 均线形态状态（bullish_align 等）
    position: Optional[str] = None  # 信号位置（形态/突破方向派生）
    volume_24h: float
    volume: float
    volume_type: str
    quality_score: Optional[int] = None  # 指标驱动筛选：候选质量分 0-100（AI 配额排序键）
    votes_json: Optional[dict] = None  # 指标票数明细 {bull, bear, bull_n, bear_n, net, voted}
    is_repeat: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ScanListResponse(BaseModel):
    items: list[ScanRecordOut]
    total: int
    page: int
    page_size: int


class ScanResultListResponse(BaseModel):
    items: list[ScanResultOut]
    total: int


class ScanTriggerResponse(BaseModel):
    scan_id: UUID
    status: str


class ScanConfig(BaseModel):
    interval_hours: int
    kline_interval: str
    window: int
    breakout_threshold: float
    r_squared_threshold: float
    repeat_window_hours: int


class ScanStatusResponse(BaseModel):
    last_scan: Optional[ScanRecordOut] = None
    is_scanning: bool
    config: ScanConfig
    # 交易运行状态（2026-09-20 顶部一览；2026-10-03 双网独立开关；
    # 同日删环境切换，在跑单统计为双网合计）
    trading_enabled_testnet: bool = False  # 测试网自动交易开关
    trading_enabled_live: bool = False     # 正式网自动交易开关
    open_trades: int = 0          # 在跑单数（双网合计，OPENED/TP1_HIT/TP2_HIT 三状态）
    open_tp_hit: int = 0          # 其中已进入止盈止损阶段的（TP1_HIT/TP2_HIT，止损已移保本，不占上限名额）
    open_trades_testnet: int = 0  # 在跑 OPENED 按网计数（2026-10-04 拆分显示；满仓跳过判定=任一网达上限）
    open_trades_live: int = 0
    max_open_trades: int = 5
    ai_analysis_enabled: bool = False
    # 各网当日熔断状态（2026-10-04 顶部卡片「熔断状态」列）：
    # {"testnet": {"enabled", "active", "day_pnl", "cap"}, "live": {...}}
    # active: True=已熔断 / False=未熔断 / None=无法判定（钱包失败或未启用）
    loss_breaker: dict = {}


# ===== AI 分析 =====

class AIAnalysisOut(BaseModel):
    id: UUID
    scan_result_id: UUID
    symbol: str
    trade_decision: Optional[str] = None  # suggest / skip
    skip_reason: Optional[str] = None
    direction: Optional[str] = None  # long / short
    trade_type: Optional[str] = None  # 开单类型：trend_follow / rule_123 / n_structure / rule_2b / range_edge
    analysis: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    # 程序止损参考价（2026-10-11）：0.5×ATR 外扩（calc_stop_reference），
    # 仅前端与 AI 报价对比展示；skip/旧行 NULL
    calc_stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    position_pct: Optional[float] = None
    recommendation: Optional[float] = None  # 推荐程度 0-100
    pullback_wait: bool = False  # AI 建议等回踩（仅展示"待回踩"标识，不下单）
    open_block_reason: Optional[str] = None  # 未开单原因（开仓闸门拦截时记录）
    # 战法命中标注（2026-10-02，仅借鉴）：白名单闸门后的战法名与判定依据
    tactic_name: Optional[str] = None
    tactic_reason: Optional[str] = None
    # 双入场策略胜出方（2026-10-10-5，docs/20）：s1=完整规则 / s2=EMA21 金K跟踪，
    # NULL=旧行按 s1；两策略分条结论在 analysis 文本内
    entry_strategy: Optional[str] = None
    # 策略二结论审计字段（2026-10-11 建列）：前端分区展示结论徽标直接取结构化字段
    strategy2_decision: Optional[str] = None
    strategy2_reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AIAnalysisListResponse(BaseModel):
    items: list[AIAnalysisOut]
    total: int


class AIAnalysisTriggerRequest(BaseModel):
    scan_result_id: Optional[UUID] = None  # None = 全量分析, 有值 = 单币分析
    user_input: Optional[str] = None  # 用户补充说明，随分析一起传给 AI


# ===== 手动搜索 AI 分析 =====

class ManualAnalyzeRequest(BaseModel):
    symbol: str


class ManualAnalysisOut(BaseModel):
    """手动搜索币种的 AI 分析结果（无 scan_result_id）"""
    id: UUID
    symbol: str
    trade_decision: Optional[str] = None  # suggest / skip
    skip_reason: Optional[str] = None
    direction: Optional[str] = None  # long / short
    trade_type: Optional[str] = None  # 开单类型：trend_follow / rule_123 / n_structure / rule_2b / range_edge
    analysis: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    calc_stop_loss: Optional[float] = None  # 程序止损参考价（0.5×ATR 外扩，仅对比展示）
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    position_pct: Optional[float] = None
    recommendation: Optional[float] = None  # 推荐程度 0-100
    pullback_wait: bool = False  # AI 建议等回踩（仅展示"待回踩"标识，不下单）
    # 战法命中标注（2026-10-02，仅借鉴）
    tactic_name: Optional[str] = None
    tactic_reason: Optional[str] = None
    created_at: datetime


# ===== 关注列表 =====

class WatchlistItemOut(BaseModel):
    id: UUID
    symbol: str
    note: Optional[str] = None
    created_at: datetime
    # K 线最近一次手动刷新时间（刚添加时等于 created_at）
    updated_at: Optional[datetime] = None
    # 该币种最近一次扫描命中结果（无命中或从未扫描时为 None）
    latest_scan: Optional[ScanResultOut] = None

    class Config:
        from_attributes = True


class WatchlistListResponse(BaseModel):
    items: list[WatchlistItemOut]
    total: int


class WatchlistAddRequest(BaseModel):
    symbol: str
    note: Optional[str] = None


# ===== 系统配置（AI 开关 + 扫描策略） =====

class SystemConfigOut(BaseModel):
    # AI 分析
    ai_analysis_enabled: bool
    ai_configured: bool  # 后端是否已配置 AI_API_KEY（不返回 key 本身）
    ai_pipeline_enabled: bool = False  # Agent 管线开关
    dual_judge_enabled: bool = True  # 双评委辩论（P2，2026-09-15 起默认开）
    tp2_enabled: bool = False  # TP2 分批止盈开关（2026-10-08 默认关：仅 TP1 80%+剩余跟进）

    # 自定义策略提示词
    strategy_prompt_enabled: bool
    strategy_prompt: str

    # 扫描策略
    kline_interval: str
    kline_window: int
    breakout_threshold: float
    r_squared_threshold: float
    repeat_window_hours: int
    swing_order: int
    pullback_tolerance: float

    # 筛选参数组（2026-09-28）：scan_filters=库内原始值（null=未定制，回落 env）；
    # scan_filters_effective=实际生效值（DB>env>代码默认解析结果），前端表单初值用
    scan_filters: Optional[dict] = None
    scan_filters_effective: dict = {}

    fib_enabled: bool

    # 自动交易（docs/06）
    max_open_trades: int = 5
    trading_min_free_pct: float = 0.5  # 开仓最低可用余额占钱包比例（0~1；2026-09-15 由固定 70% 改为可配置）
    # 止损金额模式（2026-09-27）：pct=风险基数分档×3%（默认）/ fixed=固定 USDT 金额
    risk_budget_mode: str = "pct"
    risk_fixed_amount: float = 3.0

    # 自动交易开关（2026-09-20 由 env 迁移进 DB，页面可改；2026-10-03 总开关
    # 拆分为双网独立开关可同时开启——同一 AI 分析双网各开一单；同日删环境切换）
    trading_enabled_testnet: bool = True   # 测试网自动交易（默认开）
    trading_enabled_live: bool = False     # 正式网自动交易（默认关，实盘安全基线）
    # 多账号（docs/17 P0/P1）：.env 注册表只读展示 + 主账号 + 启用自动交易的 live 账号集合
    current_account_key: Optional[str] = None
    live_trading_keys: list[str] = []   # P1 扇出：正式网自动交易开关开启时扇出的账号（空=回落 live-1）
    accounts: list["AccountBrief"] = []
    # 双环境凭据配置状态（只读，不返回 key 本身）
    binance_key_live: bool = False      # 正式网 key 已配置
    binance_key_testnet: bool = False   # 测试网 key 已配置（未配时回落正式网 key = 与 live 同值）

    # 规则补丁参数（docs/12 §10，比例口径 0.04=4%；NULL=默认/不限制）
    max_stop_pct: Optional[float] = None
    liq_gate_buffer: Optional[float] = None
    min_adapt_leverage: Optional[int] = None  # 动态杠杆下限（整数倍数；NULL=默认 8）
    pullback_market_pct: Optional[float] = None  # 入场偏离容差（比例 0.01=1%；NULL=默认）
    max_slippage_pct: Optional[float] = None  # 开仓滑点实测上限（比例 0.003=0.3%；NULL=默认）

    # 风控参数（2026-10-02 上页面；NULL=回落 env 默认，页面留空即恢复默认）
    min_recommendation: Optional[int] = None          # 开单评分门槛（默认 60）
    daily_loss_stop_pct: Optional[float] = None       # 当日亏损熔断阈值 %（默认 9）
    daily_loss_stop_enabled: Optional[bool] = None    # 熔断开关（默认开）
    ai_rr_min: Optional[float] = None                 # 盈亏比铁律下限（默认 1.5）
    ai_rr_tol: Optional[float] = None                 # 现价复算容差（默认 0.02，0=严格）
    risk_pct: Optional[float] = None                  # 单笔风险比例 %（默认 3，pct 模式）
    leverage_max: Optional[int] = None                # 杠杆上限（默认 20）
    taker_fee_rate: Optional[float] = None            # Taker 费率（默认 0.0005）
    near_anchor_factor: Optional[float] = None        # 近锚折价系数（默认 0.7；1=关闭）
    portfolio_heat_cap: Optional[float] = None        # 组合热度上限（默认 0=关闭）
    same_direction_cap: Optional[int] = None          # 同向上限（默认 0=不限）
    btc_crash_pause_pct: Optional[float] = None       # BTC 急跌暂停 %（默认 1；0=关）
    market_wide_pause_pct: Optional[float] = None     # 大盘单边暂停 %（N-15，默认 3；0=关；双跌拦开多/双涨拦开空）
    weekly_dd_stop_pct: Optional[float] = None        # 周回撤减速阈值 %（默认 15；0=关，docs/13 #2；触发时在跑/同向上限减半）
    monthly_dd_stop_pct: Optional[float] = None       # 月回撤熔断阈值 %（默认 25；0=关）
    monthly_breaker_ack_at: Optional[datetime] = None  # 月熔断人工确认时刻（只读，ack 端点写）
    weekly_breaker_ack_at: Optional[datetime] = None  # 周减速人工确认时刻（只读，ack 端点写，2026-10-07）
    atr_trail_k: Optional[float] = None               # ATR 跟进倍数（默认 3；0=关 ATR 腿，docs/13 #5）
    # TP 分批比例（2026-10-09，% 值；NULL=env 默认 60/20/80，剩余自动跟进）
    tp1_qty_pct_with_tp2: Optional[float] = None      # 有 TP2 时 TP1 分批比例 %
    tp2_qty_pct: Optional[float] = None               # TP2 分批比例 %
    tp1_qty_pct_no_tp2: Optional[float] = None        # 无 TP2 时 TP1 一次止盈比例 %

    # 初始资金（2026-09-26）：正式网主账号首次入金，资金划转面板展示口径
    live_initial_capital: float = 0

    class Config:
        from_attributes = True


class AccountBrief(BaseModel):
    """账号注册表条目（只读展示，docs/17）：key 稳定标识，key/secret 永不返回"""
    key: str
    name: str
    testnet: bool
    configured: bool = True   # 注册表构建即代表 key+secret 齐全，恒 True（保留字段语义）

    class Config:
        from_attributes = True


class SystemConfigUpdate(BaseModel):
    # AI 分析开关
    ai_analysis_enabled: Optional[bool] = None
    ai_pipeline_enabled: Optional[bool] = None
    dual_judge_enabled: Optional[bool] = None
    tp2_enabled: Optional[bool] = None

    # 自定义策略提示词
    strategy_prompt_enabled: Optional[bool] = None
    strategy_prompt: Optional[str] = None

    # 扫描策略（均为可选，只更新传入的字段）
    kline_interval: Optional[str] = None
    kline_window: Optional[int] = None
    breakout_threshold: Optional[float] = None
    r_squared_threshold: Optional[float] = None
    repeat_window_hours: Optional[int] = None
    swing_order: Optional[int] = None
    pullback_tolerance: Optional[float] = None

    # 筛选参数组（2026-09-28）：整组覆盖；传 null=清除定制（恢复 env/代码默认）。
    # 键集与范围校验见 api/scan.py _validate_scan_filters
    scan_filters: Optional[dict] = None

    fib_enabled: Optional[bool] = None

    # 自动交易（docs/06）
    max_open_trades: Optional[int] = None
    trading_min_free_pct: Optional[float] = None  # 0~1，开仓最低可用余额占比
    # 止损金额模式（2026-09-27）：pct/fixed；fixed 金额 0.1~1000 USDT
    risk_budget_mode: Optional[str] = None
    risk_fixed_amount: Optional[float] = None

    # 自动交易开关（2026-10-03 拆分双网独立开关，语义见 SystemConfigOut）
    trading_enabled_testnet: Optional[bool] = None
    trading_enabled_live: Optional[bool] = None
    # 当前正式网主账号（docs/17）：需为注册表内 live 账号（仅前端展示/默认 scope）
    current_account_key: Optional[str] = None
    # P1 扇出（docs/17 §3）：启用自动交易的正式网账号集合
    live_trading_keys: Optional[list[str]] = None

    # 规则补丁参数（比例口径；传 null 恢复默认）
    max_stop_pct: Optional[float] = None
    liq_gate_buffer: Optional[float] = None
    min_adapt_leverage: Optional[int] = None  # 动态杠杆下限（1~20；传 null 恢复默认）
    pullback_market_pct: Optional[float] = None  # 入场偏离容差（比例 0.01=1%；NULL=默认）
    max_slippage_pct: Optional[float] = None  # 开仓滑点实测上限（比例 0.003=0.3%；NULL=默认）

    # 风控参数（2026-10-02 上页面；传 null 恢复 env 默认；范围校验见 api/scan.py）
    min_recommendation: Optional[int] = None
    daily_loss_stop_pct: Optional[float] = None
    daily_loss_stop_enabled: Optional[bool] = None
    ai_rr_min: Optional[float] = None
    ai_rr_tol: Optional[float] = None
    risk_pct: Optional[float] = None
    leverage_max: Optional[int] = None
    taker_fee_rate: Optional[float] = None
    near_anchor_factor: Optional[float] = None
    portfolio_heat_cap: Optional[float] = None
    same_direction_cap: Optional[int] = None
    btc_crash_pause_pct: Optional[float] = None
    market_wide_pause_pct: Optional[float] = None     # 大盘单边暂停 %（N-15；0=关）
    # 周/月回撤分层熔断 + ATR 跟进（docs/13 #2/#5；传 null 恢复 env 默认）
    weekly_dd_stop_pct: Optional[float] = None
    monthly_dd_stop_pct: Optional[float] = None
    atr_trail_k: Optional[float] = None
    # TP 分批比例（2026-10-09；% 值，传 null 恢复 env 默认；两档合计 ≤95 剩余跟进）
    tp1_qty_pct_with_tp2: Optional[float] = None
    tp2_qty_pct: Optional[float] = None
    tp1_qty_pct_no_tp2: Optional[float] = None

    # 初始资金（2026-09-26）：正式网主账号首次入金，资金划转面板展示口径（≥0）
    live_initial_capital: Optional[float] = None


# ===== 复盘统计（docs/04 §10 P2） =====

class ReviewGroupStats(BaseModel):
    signal_type: Optional[str] = None
    touch_ref: Optional[str] = None  # 回踩触点（docs/22：分组维度，None=无触点行）
    position: Optional[str] = None
    ema_state: Optional[str] = None
    rule_version: Optional[str] = None  # 规则版本分组维度（docs/24 A0，None=非版本组）
    is_veto: Optional[bool] = None  # 双评委否决单回放标记（docs/24 A5a，None=非 veto 维度组）
    total: int
    win_tp1: int = 0
    win_tp2: int = 0
    loss: int = 0
    expired: int = 0
    win_rate: float = 0.0  # (win_tp1+win_tp2)/(win+loss)，expired 不计
    ev_r: Optional[float] = None  # 期望值（R 倍数，含费用假设）


class ReviewStatsResponse(BaseModel):
    days: int
    total: int = 0
    win_tp1: int = 0
    win_tp2: int = 0
    loss: int = 0
    expired: int = 0
    win_rate: float = 0.0
    ev_r: Optional[float] = None  # 期望值（R 倍数，含费用假设）
    # 口径标注（docs/24 A2）：本接口是「建议质量口径」，考核主指标为实盘净胜率
    win_rate_note: str = ""
    groups: list[ReviewGroupStats] = []
    # 规则版本分组（docs/24 A0）：每版本一行，legacy=标记上线前旧行；跨版本不混谈
    rule_version_groups: list[ReviewGroupStats] = []
    # 双评委 veto 闭环（docs/24 A5a）：被否决建议的假想回放统计（is_veto=True 行），
    # 与整体（=通过单）对比 win_rate/ev_r 验证 veto 是否有正贡献
    veto_stats: Optional[ReviewGroupStats] = None
    veto_note: str = ""


# ===== 全漏斗转化率（docs/24 A5c） =====

class FunnelStage(BaseModel):
    stage: str  # 信号命中 / AI 分析 / suggest / 过评委 / 开仓 / 盈利
    count: int
    # 相对上一级的转化率（首级为 None）；未开仓原因等口径说明放 stages 旁 note
    conversion: Optional[float] = None
    note: str = ""


class ReviewFunnelResponse(BaseModel):
    days: int
    scope: str
    stages: list[FunnelStage] = []
    # veto 单不计入漏斗（在 suggest 之后被否决，本质是未建议）；
    # 闸门拦截明细 top3 随附，定位流失主因
    block_reasons_top: list[dict] = []


# ===== 实盘归因统计（复盘重设计 2026-09-18） =====

class ActualOverview(BaseModel):
    days: int
    count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0  # 净盈亏 USDT
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff: Optional[float] = None  # 盈亏比 avg_win/|avg_loss|
    ev_per_trade: float = 0.0  # 期望值/笔 USDT
    avg_hold_hours: float = 0.0
    total_fee: float = 0.0  # 手续费合计（负值）
    total_funding: float = 0.0  # 资金费合计
    planned_risk_total: float = 0.0  # 计划风险合计（risk_amount）
    plan_gap: float = 0.0  # 亏超计划的部分（费用/资金费/滑点）
    attributed: int = 0  # 已 AI 归因笔数
    mirror_trades: int = 0  # 双网镜像单笔数（同一 AI 决策双网都开了；all 口径非 0，2026-10-04）


class ActualGroupStats(BaseModel):
    dim: str  # 开单类型/方向/出场原因/开仓时段/错误标签
    key: str
    count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff: Optional[float] = None
    ev_per_trade: float = 0.0


class ActualTradeItem(BaseModel):
    id: UUID
    symbol: str
    direction: Optional[str] = None
    trade_type: str = "unknown"
    exit_reason: Optional[str] = None
    entry_plan: Optional[float] = None
    sl_plan: Optional[float] = None
    tp1_plan: Optional[float] = None
    entry_actual: Optional[float] = None
    realized_pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    risk_amount: Optional[float] = None
    fee_total: Optional[float] = None
    funding_total: Optional[float] = None
    recommendation: Optional[float] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    hold_hours: float = 0.0
    error_tags: list[str] = []
    error_lesson: Optional[str] = None
    attributed: bool = False


class ActualReviewResponse(BaseModel):
    overview: ActualOverview
    groups: list[ActualGroupStats] = []
    items: list[ActualTradeItem] = []


class AttributionTriggerResponse(BaseModel):
    processed: int
    results: list[dict] = []


# ===== 规则补丁（docs/12 §10） =====

class PatchOut(BaseModel):
    id: UUID
    target_tag: str
    origin: str  # ai / manual
    kind: str  # prompt / param
    param_key: str = ""
    old_text: str = ""  # 系统原文段落引用（仅展示）
    new_text: str
    param_value: str = ""
    evidence_json: Optional[dict] = None
    metrics_before_json: Optional[dict] = None
    metrics_after_json: Optional[dict] = None
    verdict: str = "pending"  # pending / valid / invalid / worse
    status: str  # draft / active / valid / invalid / rolled_back
    applied_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PatchListResponse(BaseModel):
    items: list[PatchOut]
    stats: dict  # 当前实时窗口统计（热度/EV/胜率/通过率）
    rule_tags: list[str]


class PatchDetailResponse(PatchOut):
    """对照明细：继承补丁全字段（metrics_before/after 内含前后指标）"""


# ===== 技能库（docs/04 §6，只读） =====

class SkillOut(BaseModel):
    name: str
    description: str = ""
    use_when: str = ""
    version: str = "1"


class SkillDetailOut(SkillOut):
    body: str = ""
