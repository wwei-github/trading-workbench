"""系统配置模型（单行表，存储运行时可变配置：AI 开关 + 扫描策略）"""
from sqlalchemy import Boolean, String, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)  # 固定 id=1

    # AI 分析开关
    ai_analysis_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # 扫描策略（运行时可修改，初始值从 env 注入）
    kline_interval: Mapped[str] = mapped_column(String(8), default="1h")
    kline_window: Mapped[int] = mapped_column(Integer, default=240)
    breakout_threshold: Mapped[float] = mapped_column(Numeric(10, 6), default=0.005)
    r_squared_threshold: Mapped[float] = mapped_column(Numeric(10, 6), default=0.5)
    repeat_window_hours: Mapped[int] = mapped_column(Integer, default=24)
    swing_order: Mapped[int] = mapped_column(Integer, default=3)
    pullback_tolerance: Mapped[float] = mapped_column(Numeric(10, 6), default=0.03)

    notes: Mapped[str] = mapped_column(String(255), default="系统运行时配置")
