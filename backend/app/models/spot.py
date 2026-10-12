"""现货屯币模型（docs/18，全部全新表——零侵入约束：不动合约既有表语义）

- SpotOrder：现货订单（P0 只落手动测试单/表结构就绪；P1 信号/定投执行写入）
- SpotWeeklyReview：AI 周度研判留档（P1 生成；表先建）
- SpotKlineCache：现货日/周/月K 本地缓存（P0 日检任务写入，评分实时算）
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Index, JSON, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 屯币白名单（docs/18 §1 硬编码三币）
SPOT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")


class SpotOrder(Base):
    __tablename__ = "spot_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    # signal = 折价/过热信号；dca = 周度定投；manual = 手动
    trigger_type: Mapped[str] = mapped_column(String(8), nullable=False, default="manual")
    # 触发快照：{score, rule_hits[], env:{monthly,weekly}, ai_review:{verdict,reason}}
    # （参照 docs/16 的 32KB 截断原则）
    trigger_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    qty: Mapped[Optional[float]] = mapped_column(Numeric(28, 10), nullable=True)
    quote_amount: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Numeric(28, 10), nullable=True)

    # PENDING_CONFIRM / FILLED / CANCELLED / FAILED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING_CONFIRM")
    fail_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    testnet: Mapped[bool] = mapped_column(nullable=False, default=True)
    # 归属账号（docs/17 注册表口径：testnet-1/live-N）；NULL 旧行按 testnet 回落
    account_key: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    binance_order_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SpotWeeklyReview(Base):
    __tablename__ = "spot_weekly_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ISO 周标识（如 2026-W39），唯一
    week: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)

    # 积累期 / 上升期 / 分配期 / 下降期
    regime: Mapped[str] = mapped_column(String(8), nullable=False)
    outlook: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # {btc: 偏多/…}
    risks: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # 风险提示 ≤3 条
    commentary: Mapped[str] = mapped_column(Text, default="")

    # 输入给 LLM 的三周期指标快照（回看 AI 判断依据）
    indicator_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SpotKlineCache(Base):
    __tablename__ = "spot_kline_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    # 1d / 1w / 1M
    interval: Mapped[str] = mapped_column(String(4), nullable=False)
    # 币安原始K线数组的精简版：[{t, o, h, l, c, v}]（t=ms open_time）
    klines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_spot_kline_cache_symbol_interval", "symbol", "interval", unique=True),
    )
