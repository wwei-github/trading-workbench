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
    position: Optional[str] = None  # 12金K出现的位置（关键位类型）
    key_levels: Optional[list] = None  # 命中的关键位明细
    volume_24h: float
    volume: float
    volume_type: str
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
    memory_injection_enabled: bool = False  # 复盘记忆注入（P2）
    dual_judge_enabled: bool = False  # 双评委辩论（P2）

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

    # 关键位参数
    key_level_tolerance: float
    level_merge_threshold: float
    fib_enabled: bool

    class Config:
        from_attributes = True


class SystemConfigUpdate(BaseModel):
    # AI 分析开关
    ai_analysis_enabled: Optional[bool] = None
    ai_pipeline_enabled: Optional[bool] = None
    memory_injection_enabled: Optional[bool] = None
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

    # 关键位参数
    key_level_tolerance: Optional[float] = None
    level_merge_threshold: Optional[float] = None
    fib_enabled: Optional[bool] = None


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


class ReviewStatsResponse(BaseModel):
    days: int
    total: int = 0
    win_tp1: int = 0
    win_tp2: int = 0
    loss: int = 0
    expired: int = 0
    win_rate: float = 0.0
    groups: list[ReviewGroupStats] = []


# ===== 技能库（docs/04 §6，只读） =====

class SkillOut(BaseModel):
    name: str
    description: str = ""
    use_when: str = ""
    version: str = "1"


class SkillDetailOut(SkillOut):
    body: str = ""
