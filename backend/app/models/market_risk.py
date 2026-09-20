"""市场风险防护状态（docs/15）。状态单行 id=1，历史只增不改。"""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MarketRiskState(Base):
    __tablename__ = "market_risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    level: Mapped[int] = mapped_column(Integer, default=0)      # 0~3
    cause: Mapped[str] = mapped_column(String(16), default="normal")  # normal / s4
    signals: Mapped[dict] = mapped_column(JSON, default=dict)   # 最近一次信号快照 {s1,s2,s3,s4,btc_24h_pct,...}
    pending_level: Mapped[int] = mapped_column(Integer, default=0)   # 升级确认中的候选等级
    pending_count: Mapped[int] = mapped_column(Integer, default=0)   # 连续命中轮数
    below_count: Mapped[int] = mapped_column(Integer, default=0)     # 低于当前等级的连续轮数（降级防抖）
    s4_last_release_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    osc_lock_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # L2 震荡锁截止
    entered_level_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 进入当前等级时间
    # 仓位动作待重试（2026-09-20）：L2/L3 动作失败或 trader 未配置时置位，
    # 下轮快检即便等级未变也重试执行；成功后清零
    actions_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketRiskHistory(Base):
    __tablename__ = "market_risk_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_level: Mapped[int] = mapped_column(Integer)
    to_level: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[dict] = mapped_column(JSON, default=dict)  # {reason, s1, s2, s3, s4}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
