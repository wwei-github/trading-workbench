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
    PULLBACK_TOLERANCE: float = 0.03  # 回调容差 3%

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
    AI_RR_MIN: float = 1.5            # 盈亏比复算下限（交易系统铁律）
    RISK_BUDGET_PCT: float = 3.0      # 单笔固定亏损预算（%账户资金）：仓位 = 预算÷止损距离%，触止损恰好亏3%
    FINGERPRINT_TTL_MIN: int = 60     # 指纹缓存复用窗口（分钟，0.3% 价格分桶）
    AI_MAX_PER_SCAN: int = 10         # 单次扫描批量 AI 分析上限（按24h成交额取前N）
    MIN_VOLUME_24H: float = 3_000_000  # 扫描候选池 24h 成交额下限（USDT），低于此不进扫描

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
    TRADING_TESTNET: bool = True      # 初始交易环境（运行时以 system_config.trading_testnet 为准，此值仅首次建行注入）
    TRADING_ENABLED: bool = False     # 自动交易总开关初始值（运行时以 system_config.trading_enabled 为准）
    TRADING_MIN_RECOMMENDATION: float = 60.0  # 开单推荐度门槛
    TRADING_RISK_PCT: float = 3.0     # 止损金额占风险基数比例（%）
    # 近锚单风险折价（docs/23，2026-09-29）：突破类 stop_anchor=near 的单，
    # 风险预算 ×此系数——止损贴突破K极值被扫率未知（观察期敞口折价），
    # 且止损窄使名义仓位放大、手续费/滑点磨损绝对额相应放大（30→21 USDT 例）。
    # 不改变 EV/R 符号只缩敞口；复盘胜率验证后可调回 1.0 放开
    NEAR_ANCHOR_RISK_FACTOR: float = 0.7
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
