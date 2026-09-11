"""系统配置模型（单行表，存储运行时可变配置：AI 开关 + 扫描策略）"""
from sqlalchemy import Boolean, String, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)  # 固定 id=1

    # AI 分析开关
    ai_analysis_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Agent 管线开关：开=工具循环+锚点+技能（docs/04 P1），关=单次调用+校验回炉（P0）
    ai_pipeline_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 双评委辩论（docs/04 P2，移植 TradingAgents prompts）：对 suggest 决策做多空辩论复核；默认关
    dual_judge_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # 自定义策略提示词（MD 格式，AI 分析时可选携带）
    strategy_prompt_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy_prompt: Mapped[str] = mapped_column(Text, default="")

    # 扫描策略（运行时可修改，初始值从 env 注入）
    kline_interval: Mapped[str] = mapped_column(String(8), default="1h")
    kline_window: Mapped[int] = mapped_column(Integer, default=240)
    breakout_threshold: Mapped[float] = mapped_column(Numeric(10, 6), default=0.005)
    r_squared_threshold: Mapped[float] = mapped_column(Numeric(10, 6), default=0.5)
    repeat_window_hours: Mapped[int] = mapped_column(Integer, default=24)
    swing_order: Mapped[int] = mapped_column(Integer, default=3)
    pullback_tolerance: Mapped[float] = mapped_column(Numeric(10, 6), default=0.03)

    # 关键位参数（关键位筛选重构，见 docs/03）
    key_level_tolerance: Mapped[float] = mapped_column(Numeric(10, 6), default=0.005)  # 关键位区域半宽 ±0.5%
    level_merge_threshold: Mapped[float] = mapped_column(Numeric(10, 6), default=0.005)  # 支撑/压力聚类合并阈值
    fib_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 斐波那契位开关（二期）

    notes: Mapped[str] = mapped_column(String(255), default="系统运行时配置")
