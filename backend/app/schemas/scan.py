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
