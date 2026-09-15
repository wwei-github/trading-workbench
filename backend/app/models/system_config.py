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
    # 双评委辩论（docs/04 P2，移植 TradingAgents prompts）：对 suggest 决策做多空辩论复核；默认开
    dual_judge_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

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

    # 斐波那契位开关（二期）
    fib_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # 自动交易（docs/06）：同时在跑单子上限（页面可改）
    max_open_trades: Mapped[int] = mapped_column(Integer, default=5)

    # 余额风控：开仓最低可用余额占钱包余额比例（0~1，页面可改）。
    # 2026-09-15 由固定 70% 规则改为默认 50% 可配置。开单类型策略开关已移除
    # （2026-09-15 拍板：trade_type 仅作记录不再拦截开仓；DB 历史列保留不删）
    trading_min_free_pct: Mapped[float] = mapped_column(Numeric(10, 6), default=0.5)

    notes: Mapped[str] = mapped_column(String(255), default="系统运行时配置")
