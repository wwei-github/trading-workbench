"""自动交易数据模型（docs/06）

- TradeRecord：一笔自动交易的全生命周期（开仓信息/三价现值/状态/收益结算）
- TradeEvent：操作历史（每步迁移/撤单/改价/成交/异常各一行）
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (String, Integer, Boolean, Numeric, DateTime, ForeignKey,
                        JSON, Index, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class TradeRecord(Base):
    """一笔自动交易。同一 AI 分析最多每账号一单（docs/17 P1 扇出）：
    (ai_analysis_id, account_key, testnet) 复合唯一（2026-10-03 由全局 unique
    改复合——双网同开需要同一分析在两网各落一单）"""

    __tablename__ = "trade_records"
    __table_args__ = (
        UniqueConstraint("ai_analysis_id", "account_key", "testnet",
                         name="uq_trade_analysis_account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short

    # 溯源（scan_result 可空：防误删历史行导致交易记录断链）
    scan_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_results.id"), nullable=True
    )
    # 导入持仓（2026-09-24）无 AI 分析行 → nullable；复合唯一见 __table_args__
    ai_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_analyses.id"), nullable=True, index=True
    )
    recommendation: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)  # 开单时评分快照
    signal_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 触发信号类型（trigger_breakout 等；NULL=导入持仓/旧行）
    pattern: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 触发K线形态（hammer 等；突破类为 NULL）
    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 测试网/正式网标识
    account_key: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 开单账号（docs/17：testnet-1/live-N；NULL=迁移前旧行，读路径按 testnet 回落）
    ai_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 开单时 AI 分析结论快照（字段同前端 AIAnalysisCard 所需，交易记录自持不随 ai_analyses 断链）

    # 开仓
    entry_price: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)  # 实际成交均价
    qty: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)  # 开仓数量（基准量）
    notional: Mapped[Optional[float]] = mapped_column(Numeric(20, 2), nullable=True)  # 名义仓位 USDT
    leverage: Mapped[int] = mapped_column(Integer, default=20)
    margin_mode: Mapped[str] = mapped_column(String(12), default="isolated")
    margin_used: Mapped[Optional[float]] = mapped_column(Numeric(20, 4), nullable=True)
    risk_amount: Mapped[Optional[float]] = mapped_column(Numeric(20, 4), nullable=True)  # 止损金额（USDT）
    risk_budget_mode: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # 开仓时止损金额模式快照：pct/fixed（NULL=旧行，按 pct 理解；2026-09-27）

    # 三价（现值；SL 跟进移动后更新，历史在 trade_events）
    stop_loss: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    tp1: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    tp2: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)

    # 状态与结算
    status: Mapped[str] = mapped_column(String(16), default="OPENED", index=True)
    # OPENED（运行中）/ TP1_HIT（部分止盈，止损已移至成本价保本）/ TP2_HIT（仅剩跟进仓）/ CLOSED / FAILED
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 正/负值 USDT（运行中=已止盈部分，结算后=全程净额）
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 浮动盈亏快照（docs/16，运行中每小时整点巡检刷新，终态 0）
    pnl_pct: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)  # 相对止损金额 %（运行中 = (已实现+浮动)/risk_amount，docs/16）
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # sl / tp1_then_sl / tp1_trail / trail_sl / breakeven_sl / manual / error
    # 实盘归因（复盘重设计 2026-09-18）：结算时的费用拆分 + AI 错误归因结论
    fee_total: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 手续费合计（负值）
    funding_total: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 资金费合计（正负皆可）
    error_tags: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # AI 归因错误标签数组（中文标签）
    error_lesson: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # AI 归因一句话教训
    attributed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 归因时间（NULL=待归因）

    # AI 持仓建议（docs/16）：浮亏在跑单的持仓诊断
    ai_advice: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # {thesis_valid, trend_broken, action: hold/tighten/reduce/exit, reason, detail}
    ai_advice_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 最近诊断时间（NULL=未诊断，冷却判定基准）
    ai_advice_pnl: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 诊断时全程盈亏（恶化 ≥0.5R 提前刷新的基线）
    peak_total_pnl: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)  # 持仓期间全程盈亏峰值（浮盈回撤 ≥0.6R 触发诊断的参照，2026-09-26）

    # 持仓时长警告级别（2026-09-27）：0=未告警 / 1=已发 12h 黄卡 / 2=已发 24h 红卡。
    # 巡检幂等去重用（每 30 分钟一轮，不重发已发级别；平仓后不再参与判定）
    hold_warn_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 规则版本（docs/24 A0，2026-09-29）：开单时规则集快照（如 v2026-09-29），
    # 复盘/归因按版本分组对比，NULL=标记上线前的旧行（按 legacy 理解）
    rule_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # {entry_order_id, sl_order_id, tp1_order_id, tp2_order_id, qty_tp1, qty_tp2, capital_base, wallet}
    # FAILED 记录另存 fail_reason（开仓失败原始报错，见 _attempt_open 的 -1121 落记录分支）

    @property
    def fail_reason(self) -> Optional[str]:
        """开仓失败原因（仅 FAILED 记录写入 raw.fail_reason，API 经 TradeOut 透出）"""
        return (self.raw or {}).get("fail_reason")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    events: Mapped[list["TradeEvent"]] = relationship(
        back_populates="trade_record", cascade="all, delete-orphan", order_by="TradeEvent.created_at"
    )


class TradeEvent(Base):
    """交易操作历史事件"""

    __tablename__ = "trade_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_records.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # OPEN / TP1_FILL / TP2_FILL / SL_MOVE / SL_FILL / CANCEL / ERROR / SETTLE / SKIP
    # / LIQUIDATION / AI_ADVICE / MANUAL_CLOSE / EXTERNAL_CLOSE（2026-10-02 外部平仓）
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    trade_record: Mapped[TradeRecord] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_trade_events_record_created", "trade_record_id", "created_at"),
    )


class AccountTransfer(Base):
    """合约账户资金划转记录（每小时结算巡检时从币安 income TRANSFER 同步）

    amount 带符号：正=转入（现货→合约），负=转出（合约→现货）。
    tran_id 为币安 income.tranId，唯一约束幂等去重（重复同步不插重复行）。
    """

    __tablename__ = "account_transfers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tran_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    amount: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    transfer_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # 环境标识（2026-09-20）：同步自当前配置账户（TRADING_TESTNET），资金分析按 scope 过滤
    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    account_key: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 划转归属账号（docs/17；NULL=旧行按 testnet 回落）
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
