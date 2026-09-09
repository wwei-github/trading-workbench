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
    direction: Optional[str] = None  # long / short
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


# ===== 系统配置（AI 开关 + 扫描策略） =====

class SystemConfigOut(BaseModel):
    # AI 分析
    ai_analysis_enabled: bool
    ai_configured: bool  # 后端是否已配置 AI_API_KEY（不返回 key 本身）

    # 扫描策略
    kline_interval: str
    kline_window: int
    breakout_threshold: float
    r_squared_threshold: float
    repeat_window_hours: int
    swing_order: int
    pullback_tolerance: float

    class Config:
        from_attributes = True


class SystemConfigUpdate(BaseModel):
    # AI 分析开关
    ai_analysis_enabled: Optional[bool] = None

    # 扫描策略（均为可选，只更新传入的字段）
    kline_interval: Optional[str] = None
    kline_window: Optional[int] = None
    breakout_threshold: Optional[float] = None
    r_squared_threshold: Optional[float] = None
    repeat_window_hours: Optional[int] = None
    swing_order: Optional[int] = None
    pullback_tolerance: Optional[float] = None
