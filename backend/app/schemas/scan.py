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
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    position_pct: Optional[float] = None
    recommendation: Optional[float] = None  # 推荐程度 0-100
    pullback_wait: bool = False  # AI 建议等回踩（仅展示"待回踩"标识，不下单）
    open_block_reason: Optional[str] = None  # 未开单原因（开仓闸门拦截时记录）
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
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    position_pct: Optional[float] = None
    recommendation: Optional[float] = None  # 推荐程度 0-100
    pullback_wait: bool = False  # AI 建议等回踩（仅展示"待回踩"标识，不下单）
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

    fib_enabled: bool

    # 自动交易（docs/06）
    max_open_trades: int = 5
    trading_min_free_pct: float = 0.5  # 开仓最低可用余额占钱包比例（0~1；2026-09-15 由固定 70% 改为可配置）

    class Config:
        from_attributes = True


class SystemConfigUpdate(BaseModel):
    # AI 分析开关
    ai_analysis_enabled: Optional[bool] = None
    ai_pipeline_enabled: Optional[bool] = None
    dual_judge_enabled: Optional[bool] = None

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

    fib_enabled: Optional[bool] = None

    # 自动交易（docs/06）
    max_open_trades: Optional[int] = None
    trading_min_free_pct: Optional[float] = None  # 0~1，开仓最低可用余额占比


# ===== 复盘统计（docs/04 §10 P2） =====

class ReviewGroupStats(BaseModel):
    signal_type: Optional[str] = None
    position: Optional[str] = None
    ema_state: Optional[str] = None
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
    groups: list[ReviewGroupStats] = []


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


# ===== 技能库（docs/04 §6，只读） =====

class SkillOut(BaseModel):
    name: str
    description: str = ""
    use_when: str = ""
    version: str = "1"


class SkillDetailOut(SkillOut):
    body: str = ""
