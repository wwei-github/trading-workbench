"""多策略可插拔架构（docs/20）数据模型。

StrategyShadowSignal：S1 影子期策略信号独立落库（只记录不开单）——
ema_trend 等非内置策略产出的信号在此积累样本，用于 docs/20 §4.2 的
重叠率/相关性判定与后续 S2 开单权限评审。golden_k_ai 内置管线的信号
不落此表（走 scan_results/ai_analyses 原路径，零行为变化）。

StrategyObservation：L1 观测指标落库（docs/19 §3.1）——全市场周期性
快照（4h EMA144 斜率/缠绕/ADX、1h 结构发散度、震荡评分卡 8 项、KDJ、
MACD 零轴），为 P1 闸门阈值定标供数（「不采用外部默认值」的分位素材）。
纯观测，不参与任何拦截。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Numeric, DateTime, JSON, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StrategyShadowSignal(Base):
    """策略影子信号（S1，docs/20 §八）：策略插件产出、未接执行层的信号记录。

    与 TradeRecord（实盘单）严格隔离：本表任何行都不会触发开单——
    S2 只在影子期数据评审通过后，才让指定 strategy_id 的信号进执行层。
    推演回填（docs/20 影子分析）：outcome/r/eval_bars 由 replay_exit 对
    信号后 K 线回放得出（假想结果，口径同 docs/14 未开单候选回放）。
    """

    __tablename__ = "strategy_shadow_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short
    entry: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    stop: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    tp1: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    tp2: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    signal_type: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)  # pullback / cross
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 策略私有证据快照
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # ── 推演回填（NULL=未推演；"open"=窗口耗尽不进 decided 分母）──
    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # loss/win_tp1/win_tp2/be_exit/open/invalid
    r: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)  # 含费净 R（open 为 NULL）
    eval_bars: Mapped[Optional[int]] = mapped_column(nullable=True)  # 推演消耗根数
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_shadow_sig_strategy_created", "strategy_id", "created_at"),
        Index("ix_shadow_sig_outcome", "strategy_id", "outcome"),
    )


class StrategyObservation(Base):
    """L1 观测指标快照（docs/19 §3.1）：周期性全市场证据落库，只算不拦。

    metrics 键集（version=fobs-1）：gate_4h{direction,slope_pct,wraps,adx,close_vs_ema144}、
    structure_1h{aligned,div_fast_mid,div_mid_slow,atr}、range_score{score,items[]}、
    stochastic{...analyze_stochastic}、macd_zero{...macd_zero_state}。
    阈值定标素材按列出的 JSON 键做分位统计；保留期由观测任务维护（30 天）。
    """

    __tablename__ = "strategy_observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[str] = mapped_column(String(16), nullable=False, default="ema_trend", index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    metrics_version: Mapped[str] = mapped_column(String(8), nullable=False, default="fobs-1")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_obs_strategy_symbol_created", "strategy_id", "symbol", "created_at"),
    )
