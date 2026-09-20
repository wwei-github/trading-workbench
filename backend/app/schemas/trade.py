"""交易记录 API 模型（docs/06）"""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TradeEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    detail: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TradeOut(BaseModel):
    id: uuid.UUID
    symbol: str
    direction: str
    recommendation: Optional[float] = None
    entry_price: Optional[float] = None
    qty: Optional[float] = None
    notional: Optional[float] = None
    leverage: int
    margin_used: Optional[float] = None
    risk_amount: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    status: str
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None  # 浮动盈亏快照（运行中每小时巡检刷新，docs/16）
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    fail_reason: Optional[str] = None  # 开仓失败原因（仅 FAILED 记录，读自 raw.fail_reason）
    testnet: bool = True
    ai_snapshot: Optional[dict] = None
    ai_analysis_id: Optional[uuid.UUID] = None
    ai_advice: Optional[dict] = None  # AI 持仓诊断（docs/16，NULL=未诊断）
    ai_advice_at: Optional[datetime] = None  # 最近诊断时间

    class Config:
        from_attributes = True


class AccountTransferOut(BaseModel):
    """合约账户资金划转（amount 带符号：正=转入，负=转出）"""
    id: uuid.UUID
    tran_id: str
    asset: str
    amount: float
    transfer_time: datetime

    class Config:
        from_attributes = True


class FundSummary(BaseModel):
    """资金分析概览（docs/14）"""
    closed: int = 0
    wins: int = 0
    win_rate: Optional[float] = None      # 0-100，无闭单为 None
    net_pnl: float = 0.0
    fee_total: float = 0.0
    funding_total: float = 0.0
    avg_win_r: Optional[float] = None     # 仅 risk_amount>0 的盈利单均值
    transfer_in: float = 0.0
    transfer_out: float = 0.0
    transfer_net: float = 0.0


class DailyPoint(BaseModel):
    """北京时间自然日聚合点"""
    date: str          # YYYY-MM-DD
    pnl: float = 0.0


class SymbolPnl(BaseModel):
    symbol: str
    pnl: float
    count: int
    wins: int


class ExitReasonStat(BaseModel):
    reason: str
    count: int
    pnl: float


class PnlBucket(BaseModel):
    bucket: str        # 如 "-100~-50"、"50~100"、">|1000|"
    count: int


class TransferPoint(BaseModel):
    """单日划转合计（转入/转出均为正数）"""
    date: str
    transfer_in: float = 0.0
    transfer_out: float = 0.0


class FundAnalyticsOut(BaseModel):
    """GET /api/trades/analytics 响应（docs/14 §5）"""
    summary: FundSummary
    equity_curve: list[DailyPoint]
    daily_pnl: list[DailyPoint]
    by_symbol: list[SymbolPnl]
    exit_reasons: list[ExitReasonStat]
    pnl_distribution: list[PnlBucket]
    transfers: list[TransferPoint]
