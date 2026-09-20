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

    # 运行时环境与总开关（2026-09-20 由 env 静态配置迁移而来，页面可改、任务即时生效）：
    # trading_testnet：当前交易环境（true=测试网 false=正式网），开仓/扫描宇宙/复核用；
    #                  存量单按记录自带 testnet 标记结算，切环境不影响在跑单
    # trading_enabled：自动交易总开关（关=定时/即时开仓全部跳过，结算巡检照常）
    trading_testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # 余额风控：开仓最低可用余额占钱包余额比例（0~1，页面可改）。
    # 2026-09-15 由固定 70% 规则改为默认 50% 可配置。开单类型策略开关已移除
    # （2026-09-15 拍板：trade_type 仅作记录不再拦截开仓；DB 历史列保留不删）
    trading_min_free_pct: Mapped[float] = mapped_column(Numeric(10, 6), default=0.5)

    # 规则补丁参数（docs/12 §10，AI 复盘补丁的程序参数落点；NULL/默认=不限制）：
    # max_stop_pct：止损宽度硬上限（比例，如 0.04=4%），开仓闸门读取；
    #                NULL 历史默认=不强平闸门兜底（liquidation_gate_pct）
    # liq_gate_buffer：强平闸门缓冲（现值常量 LIQ_GATE_BUFFER=0.005），
    #                程序参数补丁「执行偏差」落点
    max_stop_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    liq_gate_buffer: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)

    # min_adapt_leverage：动态杠杆下限（整数倍数）。止损距离超 20× 强平闸门时
    # 按安全杠杆降杠杆开单（2026-09-18），安全杠杆低于此下限的宽止损单放弃——
    # 限制层：不是所有单都动态算完就开。NULL=恢复默认（config.TRADING_LEVERAGE_MIN=8）
    min_adapt_leverage: Mapped[int] = mapped_column(nullable=True, default=None)

    # pullback_market_pct：入场偏离容差（比例，0.01=1%）。现价偏离闸门 +
    # 待回踩单市价入场判定共用（距离 ≤ 容差 → 市价进场，RR 按现价复算）；
    # NULL=恢复默认（risk_guard.PULLBACK_MARKET_PCT=0.01）
    pullback_market_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)

    # max_slippage_pct：开仓前盘口滑点实测上限（比例，0.003=0.3%）。按本单数量
    # 走订单簿逐层吃的预期滑点超线放弃（GRIFFAIN 薄盘案例：0.637% 滑点撑宽
    # 止损 17%）；NULL=恢复默认 0.003
    max_slippage_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)

    notes: Mapped[str] = mapped_column(String(255), default="系统运行时配置")
