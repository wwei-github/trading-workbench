import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 应用
    APP_NAME: str = "Trading Workbench API"
    DEBUG: bool = True

    # 数据库
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/trading_workbench",
    )

    # Redis / Celery
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

    # 币安
    BINANCE_BASE_URL: str = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
    BINANCE_FUTURES_URL: str = os.getenv("BINANCE_FUTURES_URL", "https://fapi.binance.com")
    BINANCE_TIMEOUT: int = 15
    BINANCE_CONCURRENCY: int = 8

    # 扫描策略
    SCAN_INTERVAL_HOURS: int = 1
    KLINE_INTERVAL: str = "1h"
    KLINE_WINDOW: int = 240
    BREAKOUT_THRESHOLD: float = 0.002  # 最小突破幅度：收盘须越过 Donchian 轨 0.2%（毛边突破不算）
    R_SQUARED_THRESHOLD: float = 0.5
    REPEAT_WINDOW_HOURS: int = 3
    SWING_ORDER: int = 3
    BREAKOUT_CHANNEL_BARS: int = 20  # Donchian 通道突破的回看根数（收盘破 N 根高低轨 + 长实体）
    BREAKOUT_VOL_RATIO: float = 1.2  # 放量加分门槛：信号K线量 ≥ 近20根中位量的1.2倍（加分项不卡死）
    BREAKOUT_MAX_PCT: float = 1.5  # 突破幅度上限：越过轨 >1.5% 视为追高，不产生突破触发（0=关闭）
    CANDLE_MAX_EMA21_DIST_PCT: float = 1.5  # 悬空金K门槛（已随纯金K下线失效，键保留向后兼容）
    RETEST_WINDOW_BARS: int = 24  # 突破回测触点有效窗口（已收盘根数，0=关闭 retest 触点）
    STRUCTURE_TOUCH_BAND_PCT: float = 0.3  # retest/swing 横向位触碰带宽（%，影线可入带收盘须守住）
    EMA_TOUCH_MIN_BARS: int = 200  # EMA 触碰最小已收盘根数（数据门，EMA144 收敛兜底）
    DAILY_LOSS_STOP_PCT: float = 9.0  # 当日亏损熔断：UTC 日已平仓净亏 ≤ -风险基数×9%（≈3笔满损）停开仓（0=关闭）
    # 当日亏损熔断总开关（2026-09-30 用户拍板开关化）：True=启用（默认，保持现行为），
    # False=关闭；阈值仍由 DAILY_LOSS_STOP_PCT 控制（9% ≈ 3 笔满损，随账户金额缩放）。
    # 注意与软熔断（CONSECUTIVE_LOSS_DERATE 提门槛）区别：本开关管的是停开仓的硬闸门
    DAILY_LOSS_STOP_ENABLED: bool = True
    # 周/月回撤分层熔断（docs/13 #2，2026-10-04）：日级熔断之上的二三级减速——
    # 周回撤（UTC 周一 0 点起已平仓净亏）≥ 风险基数×15% → 本周剩余时间单笔预算减半；
    # 月回撤 ≥ 基数×25% → 停开新仓，人工复盘确认（monthly_breaker_ack_at）后恢复。
    # 均只收紧亏损侧，盈利月份零影响；0=关闭该层
    WEEKLY_DD_STOP_PCT: float = 15.0
    MONTHLY_DD_STOP_PCT: float = 25.0
    # ATR 跟进止损倍数（docs/13 #5 Chandelier Exit，2026-10-04）：跟进 SL =
    # 持仓期极值 ∓ k×ATR(22,D1)，与摆动低点跟进取更紧者（只收紧不放松）。
    # 0=关闭 ATR 腿（退化为现行摆动跟进）；k 默认 3（海龟系标准）
    ATR_TRAIL_K: float = 3.0
    # TP 分批止盈比例（2026-10-09 配置化，% 值；system_config 页面值优先，NULL 回落此处）：
    # 有 TP2 → TP1 60% / TP2 20% / 剩余 20% 跟进；无 TP2 → TP1 80% / 剩余 20% 跟进
    TP1_QTY_PCT_WITH_TP2: float = 60.0
    TP2_QTY_PCT: float = 20.0
    TP1_QTY_PCT_NO_TP2: float = 80.0
    # P-09 事件驱动执行开关（2026-10-07，docs/16）：user-data-stream 成交事件
    # 秒级触发现有结算链路 + mark price 盈亏快照；False=纯 30 分钟巡检（原行为）。
    # 30 分钟巡检永久保留兜底，本开关只控制事件流是否叠加；默认关——部署后
    # 先在测试网账号验证（系统日志看「事件触发结算」）再放开正式网
    EVENT_EXEC_ENABLED: bool = False
    PULLBACK_TOLERANCE: float = 0.03  # 回调容差 3%
    # 规则版本标记（docs/24 A0，2026-09-29）：随规则集变更递增（改 trigger/risk_guard/
    # 提示词纪律/分批比例均算改版），开仓与分析落库时快照到 trade_records/ai_analyses；
    # 复盘/归因按版本分组对比，跨版本混谈从机制上杜绝（NULL=上线前旧行按 legacy 理解）。
    # v2026-09-29-2：回踩金K复合确认（用户拍板）——单影线锤/流星单根不再确认，
    # 须双锤或锤+趋势K（candlestick.resolve_golden_evidence）
    # v2026-09-30：决断K线影线质量门（docs/25 §4，用户拍板）——吞没第二根、启明星/
    # 黄昏星第三根总影线 > 全长 40% 不判金K（candlestick.DECISION_SHADOW_MAX_PCT）；
    # 同日撤销 docs/25 提示词注入
    # v2026-09-30-2：突破K线/趋势K线长实体门 0.75→0.70（用户拍板）——实体 ≥70% 全长
    # （影线合计 ≤30%）即算长实体；trigger.BREAKOUT_BODY_RANGE_MIN 与
    # candlestick.TREND_CANDLE_BODY_RANGE_MIN 两常量同步
    # v2026-10-01：金K规则三项（用户拍板）——①单锤形线/倒锤形线恢复单根确认
    # （倒锤加入 GOLDEN_12，复合确认仅剩流星线：双流星/流星+趋势K）；
    # ②看涨吞没影线门不对称化（上影 ≤15% 全长、下影不限；看跌维持总影线 ≤40%）；
    # ③启明星中K实体上限收紧 0.5→0.3（防退化为看涨吞没观感）
    # v2026-10-01-2：倒锤形线退出看涨白名单（KSMUSDT 回踩 EMA21 个案拍板）——
    # 看涨回踩语境长上影=上方抛压，不是企稳证据；看涨金K回到锤/吞没/启明星 3 种
    # v2026-10-01-3：恢复完整 12 金K（看涨 6 + 看跌 6 对称）——2026-09-12 精简掉的
    # 刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑回归白名单参与信号判定；倒锤维持退出、
    # 上吊线维持被流星线替代；白名单内仅流星线走单影线复合确认
    # v2026-10-02：战法模块上线（仅借鉴标注，不影响开单结论）——AI 输出
    # tactic_name/tactic_reason 经白名单闸门防幻觉，复盘按战法分组
    # v2026-10-09：孕线突破（两向）与蜻蜓/墓碑十字移出金K白名单（用户拍板，
    # 同 2026-09-12 精简口径）——金K清单 12→8（看涨/看跌各 4）；检测器保留
    # 仅历史展示。GOLDEN_12 更名 GOLDEN_PATTERNS（数量随拍板浮动，不再入名）
    # v2026-10-10：trail RR 铁律条件豁免（拍板 B，docs/20 §四）+ 4h 结构化行/
    # 取舍提示行 signal_type 互斥/止损宽度纪律句/skip_reason 类别前缀
    RULE_VERSION: str = "v2026-10-10"

    # 排除的币种关键字（杠杆代币等）
    EXCLUDE_KEYWORDS: list = ["UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG"]
    QUOTE_ASSET: str = "USDT"

    # AI 分析（OpenAI 兼容接口）
    AI_ENABLED: bool = False
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.openai.com/v1"
    AI_MODEL: str = "gpt-4o-mini"
    # 飞书通知（业务日报/持仓建议/平仓结果）统一走自建应用机器人；
    # 旧 FEISHU_NOTIFY_WEBHOOK 自定义机器人已弃用（收不到卡片按钮回调）
    # 飞书卡片操作（市价平仓按钮）：FEISHU_ACTION_TOKEN=操作令牌（按钮 value 携带校验）。
    # 自建应用三件套（APP_ID/APP_SECRET/TARGET_ID）未配置时通知静默跳过
    FEISHU_ACTION_TOKEN: str = ""
    # 飞书自建应用机器人（卡片按钮回调走长连接，点击平仓 → 会话内新卡片回报结果）
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    # 卡片发送目标：群 chat_id（oc_ 开头）或个人 open_id（ou_ 开头），发送时按前缀自动识别
    FEISHU_TARGET_ID: str = ""

    # AI 管线（P0，docs/04 §10）
    LLM_CONCURRENCY: int = 4          # LLM 全局并发上限（Redis 信号量；2026-09-20 4→8，
                                      # 2026-09-24 8→4：降低网关拥塞，单次调用回落 85~130s 区间，
                                      # 10 币批量需 3 轮 ≈5~7min）
    AI_CALL_TIMEOUT_S: int = 300      # 单次 LLM 调用超时（防网关挂起拖垮整轮分析；
                                      # 实测单轮 85~130s，拥塞尾延迟随机破 240s →
                                      # 2026-09-28 240→300 覆盖尾部，留足余量）
    # JSON 输出模式（docs/24 D2，2026-09-29）：开启时 chat/completions 请求带
    # response_format={"type":"json_object"}（OpenAI 兼容参数），促使模型直接产出
    # 合法 JSON、降低解析失败重试率；默认关——部分兼容网关不支持该参数（会 400）
    LLM_JSON_MODE: bool = False
    AI_RR_MIN: float = 1.5            # 盈亏比复算下限（交易系统铁律）
    RISK_BUDGET_PCT: float = 3.0      # 单笔固定亏损预算（%账户资金）：仓位 = 预算÷止损距离%，触止损恰好亏3%
    FINGERPRINT_TTL_MIN: int = 60     # 指纹缓存复用窗口（分钟，0.3% 价格分桶）
    AI_MAX_PER_SCAN: int = 10         # 单次扫描批量 AI 分析上限（按24h成交额取前N）
    MIN_VOLUME_24H: float = 3_000_000  # 扫描候选池 24h 成交额下限（USDT），低于此不进扫描
    NEW_COIN_COOLDOWN_DAYS: int = 14   # 新币冷静期（天）：上市未满 N 天不进扫描（0=关闭），
                                       # scan_filters.new_coin_cooldown_days 可覆盖

    # 合约回测（docs/16 P-06）：单任务规模上限（防误配拖垮 worker 与交易所限速）
    BACKTEST_MAX_SYMBOLS: int = 50     # 单任务币种数上限（源B 每币一次全段拉取）
    BACKTEST_MAX_DAYS: int = 90        # 单任务回看天数上限

    # 市场环境注入（docs/24 A4①）：AI 事实包（P0 单次调用管线）追加本币资金费率
    # 与 BTC 状态摘要——系统提示词要求"勿臆测"，普跌日无环境数据 AI 只能凭本币
    # 指标判断；任一数据源失败静默降级为不注入
    MARKET_CONTEXT_ENABLED: bool = True   # 环境注入总开关（False=不注入，测试/降级用）
    BTC_CRASH_PAUSE_PCT: float = 1.0      # BTC 1h 近3根累计急跌阈值（%，跌破注入
                                          # 「BTC 短线急跌，山寨开单需谨慎」警示；0=关闭警示）
    MARKET_WIDE_PAUSE_PCT: float = 3.0    # 大盘单边阈值（N-15）：BTC+ETH 24h 涨跌幅
                                          # 同时超值视为单边行情——普跌拦山寨开多、
                                          # 普涨拦山寨开空（顺势方向照常；0=关闭）

    # 自动交易（docs/06，币安 USDT-M 合约）
    # 账号凭据（docs/17 注册表在 account_registry.py 读取）：正式网统一命名
    # BINANCE_LIVE_{N}_API_KEY/SECRET_KEY（N≥1，注册表直接扫 os.environ，不走本字段）；
    # 此处仅保留旧变量 BINANCE_API_KEY/SECRET_KEY 作 live-1 兼容回落与测试网兜底
    # （测试网未单独配置时回落正式网 key，兼容只有一组 key 的部署）
    BINANCE_API_KEY: str = ""
    BINANCE_SECRET_KEY: str = ""
    BINANCE_TESTNET_API_KEY: str = ""
    BINANCE_TESTNET_SECRET_KEY: str = ""
    # 现货测试网专用 key（可选）：现货测试网（testnet.binance.vision）与合约测试网
    # （testnet.binancefuture.com）是两套独立系统，key 不互通（2026-09-24 实证）。
    # 未配置时回落 BINANCE_TESTNET_*，保持旧行为
    BINANCE_SPOT_TESTNET_API_KEY: str = ""
    BINANCE_SPOT_TESTNET_SECRET_KEY: str = ""
    TRADING_ENABLED: bool = False     # （废弃 2026-10-03：拆分为下方双网开关；仅旧列首次注入用）
    # 双网自动交易开关初始值（运行时以 system_config.trading_enabled_testnet/live 为准，
    # 此值仅首次建行注入）：测试网默认开 / 正式网默认关（实盘安全基线，需页面显式开启）。
    # TRADING_TESTNET 已废弃（同日删环境切换：自动开单按双网开关、手动开单显式选账号）
    TRADING_ENABLED_TESTNET: bool = True
    TRADING_ENABLED_LIVE: bool = False
    TRADING_MIN_RECOMMENDATION: float = 60.0  # 开单推荐度门槛
    TRADING_RISK_PCT: float = 3.0     # 止损金额占风险基数比例（%）
    # 近锚单风险折价（docs/23，2026-09-29）：突破类 stop_anchor=near 的单，
    # 风险预算 ×此系数——止损贴突破K极值被扫率未知（观察期敞口折价），
    # 且止损窄使名义仓位放大、手续费/滑点磨损绝对额相应放大（30→21 USDT 例）。
    # 不改变 EV/R 符号只缩敞口；复盘胜率验证后可调回 1.0 放开
    NEAR_ANCHOR_RISK_FACTOR: float = 0.7
    # 连损降速软熔断（docs/24 A3）：当日连续亏损时提升开仓评分门槛
    # （连损 2 → 评分 ≥75，连损 ≥3 → ≥85，绝对档位；低于现行门槛值时不生效），
    # 且定时批量分析配额减半——连损趋势日降频观望，但不像当日亏损熔断那样停摆；
    # False=关闭。2026-09-30 用户拍板默认关闭（False）：评分门槛已由
    # TRADING_MIN_RECOMMENDATION 统一把关，连损日额外提分档属重复设限，
    # 且拦截理由「连损3笔降速」对用户不可预期
    CONSECUTIVE_LOSS_DERATE: bool = False
    # 组合热度上限（docs/24 A3'）：在跑单（OPENED/TP1_HIT/TP2_HIT）止损金额合计
    # / 钱包余额的上限（0.15=15%），本单预估止损计入后超限则不开新单；
    # 0=关闭。2026-09-29 用户拍板默认 0（关闭）：单数控制（max_open_trades +
    # 同向上限）已足够，金额维度不重复设限
    PORTFOLIO_HEAT_CAP: float = 0.0
    # 同向持仓上限（docs/24 A3'）：同方向（long/short）在跑单数上限，
    # 防趋势同震时单边敞口过度集中；0=不限。2026-09-30 用户拍板默认 0（关闭）：
    # 单数总量控制（max_open_trades=10）已足够，方向维度也不设限；
    # 代码保留，env 置正数可随时启用
    SAME_DIRECTION_CAP: int = 0
    # 盘口价差预检（docs/24 A4'）：开仓前 bookTicker 的 bid-ask 价差/中间价上限
    # （0.003=0.3%），价差过宽说明流动性枯竭/行情剧烈，市价成交即承担点差成本，
    # 放弃开单（查询异常不拦截，走滑点实测兜底）；0=关闭
    SPREAD_MAX_PCT: float = 0.003
    # Taker 手续费率（docs/24 A1''）：保本止损价 = 成本价 ± 往返费用缓冲
    # （开+平两腿各收一次）——窄止损下费用/R 被名义杠杆放大，
    # 纯成本价保本实际仍亏往返手续费
    TAKER_FEE_RATE: float = 0.0005
    # 风险基数取整（2026-09-21 由分档表改首位数取整：4800→4000、5200→5000，
    # 低于 10不开仓；字段保留供 .env 兼容，代码已不再读取）
    TRADING_CAPITAL_TIERS: str = "100,200,500,1000,2000,5000,10000"
    TRADING_MIN_FREE_PCT: float = 0.5  # 可用余额/总资金 下限（2026-09-15 由 70% 改 50%；系统配置表 trading_min_free_pct 可改，此值仅作缺列/缺行兜底）
    TRADING_LEVERAGE: int = 20        # 杠杆上限（逐仓 ISOLATED；止损距离超闸门时动态下调）
    TRADING_LEVERAGE_MIN: int = 8     # 动态杠杆下限默认值（system_config.min_adapt_leverage 可页面覆盖）：
                                      # 限制层——安全杠杆低于下限的宽止损单放弃，不是所有单都算完就开

    class Config:
        env_file = ".env"
        # 多账号 .env 变量（BINANCE_LIVE_{N}_*）由 account_registry 直接扫
        # os.environ，Settings 不逐个声明；ignore 避免 extra_forbidden 误杀
        extra = "ignore"


settings = Settings()
