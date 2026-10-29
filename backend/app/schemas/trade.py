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


class OpenFromAnalysisRequest(BaseModel):
    """手动开单请求（扫描结果表「开单」按钮）：AI 分析 id + 目标账号 key。

    account_key 必传（前端弹窗选账号）——2026-10-03 删环境切换后无「当前
    环境」概念，开到哪个网由用户显式选择，后端不猜。"""
    analysis_id: str
    account_key: str


class TradeOut(BaseModel):
    id: uuid.UUID
    symbol: str
    direction: str
    recommendation: Optional[float] = None
    entry_price: Optional[float] = None
    qty: Optional[float] = None
    qty_tp1: Optional[float] = None  # TP1 分批数量（读自 raw，旧记录/未开仓 None）
    qty_tp2: Optional[float] = None  # TP2 分批数量（读自 raw，无 TP2 单为 0/None）
    notional: Optional[float] = None
    leverage: int
    margin_used: Optional[float] = None
    risk_amount: Optional[float] = None
    planned_risk: Optional[float] = None   # 下单前计划风险（参考价/限价口径）
    initial_risk: Optional[float] = None   # 成交后初始风险（实际成交价，即 1R）
    remaining_risk: Optional[float] = None  # 当前剩余风险（随 TP/减仓/移损更新）
    risk_budget_mode: Optional[str] = None  # 开仓时止损金额模式快照：pct/fixed（NULL=旧行按 pct 理解）
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    status: str
    tp_mode: Optional[str] = None  # 止盈模式（历史字段：2026-10-12 策略二退役后停写；旧行 None 按 ladder 理解）
    trailing_mode: Optional[str] = None  # 移动止损方式：hl=HL/LH 结构跟踪 / three_line=三线；None=旧行按 three_line
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None  # 浮动盈亏快照（运行中每小时巡检刷新，docs/16）
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    fail_reason: Optional[str] = None  # 开仓失败原因（仅 FAILED 记录，读自 raw.fail_reason）
    # TP2 未挂上原因（docs/23 T14d）：{tp2, qty_tp2, min_qty, full_exit, reason}；
    # null=未发生（策略本就没要 TP2 或旧行），前端据此标注「无 TP2」而非静默 0
    tp2_skipped: Optional[dict] = None
    signal_type: Optional[str] = None  # 触发信号类型（trigger_breakout 等；NULL=导入持仓/旧行）
    pattern: Optional[str] = None  # 触发K线形态（hammer 等；突破类为 NULL）
    testnet: bool = True
    account_key: Optional[str] = None  # 开单账号（docs/17：testnet-1/live-N；null=旧行按 testnet 回落）
    ai_snapshot: Optional[dict] = None
    ai_analysis_id: Optional[uuid.UUID] = None
    htf: Optional[dict] = None  # 4h 背景快照（经 AI分析→扫描结果 votes_json 取，旧数据 None）
    ai_advice: Optional[dict] = None  # AI 持仓诊断（docs/16，NULL=未诊断）
    ai_advice_at: Optional[datetime] = None  # 最近诊断时间
    excluded: bool = False  # 异常单标记（2026-10-18）：标记后全链路剔除（统计/复盘/熔断/日报）
    excluded_at: Optional[datetime] = None  # 标记时刻（未标记 None）

    class Config:
        from_attributes = True


class PendingEntryOut(BaseModel):
    """挂单入场（docs/27，2026-10-17）：现价盈亏比不足时在 AI 入场价挂的限价单。

    未成交前不建 TradeRecord/不占仓位；此处透出待入场计划参数供前端
    「挂单中」表展示与一键撤销。seconds_left 为距过期剩余秒数（前端倒计时），
    已过期/已了结行取 0（负数归零）。"""
    id: uuid.UUID
    symbol: str
    direction: str
    account_key: Optional[str] = None
    testnet: bool = True
    entry_price: Optional[float] = None   # 限价单挂单价（tick 取整后）
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    qty: Optional[float] = None
    leverage: Optional[int] = None
    margin_used: Optional[float] = None
    risk_amount: Optional[float] = None
    ai_analysis_id: Optional[uuid.UUID] = None
    status: str                          # pending/filled/canceled/expired/failed
    resolve_reason: Optional[str] = None
    expires_at: Optional[datetime] = None
    seconds_left: Optional[int] = None   # 距过期剩余秒（pending 有效；其余 0）
    # 了结信息（2026-10-17 交易记录挂单历史展示）：成交/撤销/过期/失败的收尾时刻、
    # 成交均价（仅 filled）、以及成交后关联的 TradeRecord（前端跳转复盘）
    resolved_at: Optional[datetime] = None
    filled_price: Optional[float] = None
    trade_record_id: Optional[uuid.UUID] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AccountTransferOut(BaseModel):
    """合约账户资金划转（amount 带符号：正=转入，负=转出）"""
    id: uuid.UUID
    tran_id: str
    asset: str
    amount: float
    transfer_time: datetime
    account_key: Optional[str] = None  # 所属账号（docs/17；null=旧行按 testnet 回落）

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
    avg_win_r: Optional[float] = None     # 仅 risk_amount>0 的盈利单均值（资金分析「盈亏比R（止盈单）」）
    avg_r: Optional[float] = None         # 全部已结算单的 R 均值，含止盈止损（2026-10-12，交易记录「平均盈亏比」）
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
