"""系统配置模型（单行表，存储运行时可变配置：AI 开关 + 扫描策略）"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, String, Integer, Numeric, Text, DateTime, JSON
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

    # 环境与总开关演进：
    # trading_testnet：已废弃（2026-10-03 删环境切换——自动开单由双网独立开关
    # 控制、手动开单由前端显式选账号、统计/巡检双网口径，无全局「当前环境」；
    # 代码不再读写，列保留防旧库升级丢数据）
    # trading_enabled：已废弃（同日拆分为下方双网独立开关，代码不再读取；列保留）
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 双网自动交易开关（2026-10-03 拆分，2026-09-28 收窄语义不变：仅控每小时
    # AI 分析后自动开新单；手动开单按钮/AI 分析/结算巡检/复盘/日报均不受控。
    # AI 分析由 ai_analysis_enabled 独立控制）：
    # - trading_enabled_testnet：测试网开关（默认开）→ 扇出 testnet_accounts
    # - trading_enabled_live：正式网开关（默认关，实盘安全基线）→ 扇出 live_enabled_accounts
    # 两开关独立判定、可同时开启（同一 AI 分析双网各开一单，扇出见
    # account_registry.auto_trade_accounts；DB 防重为
    # (ai_analysis_id, account_key, testnet) 复合唯一）
    trading_enabled_testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_enabled_live: Mapped[bool] = mapped_column(Boolean, default=False)
    # 当前正式网主账号（docs/17 P0）：记选中的 live 账号 key（live-1/live-2…），
    # NULL=回落 live-1。2026-10-03 删环境切换后仅作前端展示/默认 scope 用，
    # 后端开单/扇出一律显式传账号 key 或走账号清单
    current_account_key: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # 启用自动交易的正式网账号集合（docs/17 P1 扇出）：逗号分隔 key（live-1,live-2…），
    # 空回落 live-1（P0 单账号语义）；trading_enabled_live 开启时由
    # auto_trade_accounts 按此清单扇出
    live_trading_keys: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 余额风控：开仓最低可用余额占钱包余额比例（0~1，页面可改）。
    # 2026-09-15 由固定 70% 规则改为默认 50% 可配置。开单类型策略开关已移除
    # （2026-09-15 拍板：trade_type 仅作记录不再拦截开仓；DB 历史列保留不删）
    trading_min_free_pct: Mapped[float] = mapped_column(Numeric(10, 6), default=0.5)

    # 止损金额（单笔固定亏损预算）模式（2026-09-27，交易配置弹窗可改）：
    # pct   = 风险基数分档（_capital_base 首位取整，100~5000）× TRADING_RISK_PCT%（现行默认）
    # fixed = 固定 USDT 金额 risk_fixed_amount（默认 3），不随钱包缩放
    risk_budget_mode: Mapped[str] = mapped_column(String(8), default="pct")
    risk_fixed_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=3.0)

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

    # ── 风控参数上页面（2026-10-02，docs/12 §10 补丁参数同款惯例）──
    # 以下列 NULL/None = 回落 config.py env 默认（页面留空即恢复默认）；
    # 消费侧统一 `cfg 列 ?? settings.*` 取值，运行时改保存即生效（任务每轮读 DB）。
    # 评分门槛：开单推荐度下限（NULL=默认 60）
    min_recommendation: Mapped[int] = mapped_column(nullable=True, default=None)
    # 当日亏损熔断：阈值 %（NULL=默认 9）+ 开关（NULL=默认 True；False=页面显式关闭）
    daily_loss_stop_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    daily_loss_stop_enabled: Mapped[bool] = mapped_column(nullable=True, default=None)
    # 周/月回撤分层熔断（docs/13 #2，2026-10-04）：阈值 %（NULL=回落 env 15/25；0=关闭该层）。
    # 周 = UTC 周起净亏 ≥ 基数×阈值 → 在跑上限/同向上限减半（2026-10-07 改口径，原单笔预算减半）；
    # 月 = UTC 月起净亏 ≥ 基数×阈值 → 停开新仓
    weekly_dd_stop_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    monthly_dd_stop_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 月熔断人工确认时刻：ack 端点写 utcnow；确认时刻在当前 UTC 月内 = 本月已复盘放行（至月底）
    monthly_breaker_ack_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, default=None)
    # 周减速人工确认时刻（2026-10-07）：ack 端点写 utcnow；确认时刻在当前 UTC 周内
    # = 本周已复盘放行（在跑/同向上限恢复常规），下周一自动重新武装
    weekly_breaker_ack_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, default=None)
    # ATR 跟进止损倍数 k（docs/13 #5，2026-10-04）：SL = max(现SL, 持仓期极值 ∓ k×ATR22)；
    # NULL=回落 env（默认 3）
    atr_trail_k: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)

    # TP2 分批止盈开关（2026-10-08，交易配置页）：默认关闭——AI 报出的 TP2 不挂档，
    # 出场走「仅 TP1 80% + 剩余 20% 跟进」；开启后有 TP2 的单按 TP1 60% / TP2 20%
    # 两档分批。手动接管单不受此开关限制
    tp2_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # TP 分批比例（2026-10-09，% 值，NULL=回落 env 默认 60/20/80）：
    # 有 TP2 → TP1 tp1_qty_pct_with_tp2% / TP2 tp2_qty_pct% / 剩余跟进；
    # 无 TP2 → TP1 tp1_qty_pct_no_tp2% / 剩余跟进
    tp1_qty_pct_with_tp2: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True, default=None)
    tp2_qty_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True, default=None)
    tp1_qty_pct_no_tp2: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True, default=None)
    # 盈亏比铁律下限（NULL=默认 1.5）：risk_guard 校验 + 开仓现价复算 + AI 提示词共用
    ai_rr_min: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 盈亏比现价复算容差（NULL=默认 0.02，0=严格 ≥rr_min）：仅开仓闸现价复算层，
    # AI 入场价口径铁律不受此参数影响
    ai_rr_tol: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 单笔风险比例 %（NULL=默认 3，pct 模式：风险基数 × 该比例）
    risk_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 杠杆上限（NULL=默认 20，逐仓；动态杠杆上边界）
    leverage_max: Mapped[int] = mapped_column(nullable=True, default=None)
    # Taker 费率（NULL=默认 0.0005）：保本止损价 = 成本价 ± 往返费缓冲
    taker_fee_rate: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 近锚折价系数（NULL=默认 0.7；1.0=关闭折价放开近锚单）
    near_anchor_factor: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 组合热度上限（比例，NULL=默认 0=关闭）：在跑单止损金额合计/钱包余额上限
    portfolio_heat_cap: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 同向持仓上限（NULL=默认 0=不限）：同方向在跑单数上限
    same_direction_cap: Mapped[int] = mapped_column(nullable=True, default=None)
    # BTC 急跌暂停阈值 %（NULL=默认 1.0；0=关闭普跌防护）
    btc_crash_pause_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)
    # 大盘单边暂停阈值 %（N-15，NULL=默认 3.0；0=关闭）：BTC+ETH 24h 双跌超值
    # 暂停山寨开多、双涨超值暂停山寨开空（双向逆势拦截）
    market_wide_pause_pct: Mapped[float] = mapped_column(Numeric(10, 6), nullable=True, default=None)

    # 现货屯币配置（docs/18）：JSON 列（三币开关/单份金额/投入上限/定投 + 全局预算）。
    # 零侵入约束：新增独立列，不改既有列语义；NULL=全部默认值。
    # 类型用可移植 JSON（与 shadow.py 等模型一致）：线上 PG 实际列仍为 JSONB
    # （database.py ADD COLUMN DDL），ORM JSON 对 jsonb 列读写兼容；
    # PG 专用 JSONB 会让 sqlite 内存库测试套件 create_all 直接编译失败
    # （2026-10-07 修复：全部 db 测试 ERROR 的根因）
    spot_settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)
    # 筛选参数组（2026-09-28 筛选配置上页面）：JSON 列，键集见 services/scan_filters.py。
    # NULL=全部回落 env/代码默认；旧 breakout_threshold 等 3 个散列已废弃不再读
    scan_filters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)
    # 现货环境开关（docs/18，2026-09-23 用户拍板：现货环境与合约完全独立）：
    # true=现货测试网（默认），false=现货正式网；现货环境与合约完全独立
    # （合约侧环境切换已于 2026-10-03 删除）
    spot_trading_testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    # 现货正式网启用账号集合（docs/17 注册表共用；docs/18）：逗号分隔 key（live-1,live-2…），
    # 空回落 live-1；现货测试网恒 testnet-1 单账号（忽略此值）
    spot_live_trading_keys: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # testnet_transfer_purge_at：测试网划转清除水位（2026-09-20 一键清空测试网数据）。
    # 清空功能删光测试网划转后记录此刻；sync_account_transfers 的 testnet 分支以它
    # 为下限，防止「无记录→从 7 天前重拉」把刚删掉的划转回灌（水位只对 testnet 生效）
    testnet_transfer_purge_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )

    # 初始资金（2026-09-26）：正式网主账号首次入金口径。划转同步首次接入只回看
    # 7 天，早于接入的入金不会进 account_transfers——资金划转面板以本字段展示
    # 初始资金，配合当前钱包余额和划转累计形成账目闭环（页面可改）
    live_initial_capital: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0)

    notes: Mapped[str] = mapped_column(String(255), default="系统运行时配置")
