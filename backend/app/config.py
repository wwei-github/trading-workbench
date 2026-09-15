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
    BINANCE_WS_URL: str = os.getenv("BINANCE_WS_URL", "wss://fstream.binance.com")
    BINANCE_TIMEOUT: int = 15
    BINANCE_CONCURRENCY: int = 8

    # 扫描策略
    SCAN_INTERVAL_HOURS: int = 1
    KLINE_INTERVAL: str = "1h"
    KLINE_WINDOW: int = 240
    BREAKOUT_THRESHOLD: float = 0.005  # 0.5%
    R_SQUARED_THRESHOLD: float = 0.5
    REPEAT_WINDOW_HOURS: int = 3
    SWING_ORDER: int = 3
    BREAKOUT_CHANNEL_BARS: int = 20  # Donchian 通道突破的回看根数（收盘破 N 根高低轨 + 放量）
    PULLBACK_TOLERANCE: float = 0.03  # 回调容差 3%

    # 排除的币种关键字（杠杆代币等）
    EXCLUDE_KEYWORDS: list = ["UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG"]
    QUOTE_ASSET: str = "USDT"

    # AI 分析（OpenAI 兼容接口）
    AI_ENABLED: bool = False
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.openai.com/v1"
    AI_MODEL: str = "gpt-4o-mini"

    # AI 管线（P0，docs/04 §10）
    LLM_CONCURRENCY: int = 4          # LLM 全局并发上限（Redis 信号量）
    AI_CALL_TIMEOUT_S: int = 240      # 单次 LLM 调用超时（防网关挂起拖垮整轮分析；实测单轮 85~130s，拥塞时 >180s，留足余量）
    AI_RR_MIN: float = 1.5            # 盈亏比复算下限（交易系统铁律）
    RISK_BUDGET_PCT: float = 3.0      # 单笔固定亏损预算（%账户资金）：仓位 = 预算÷止损距离%，触止损恰好亏3%
    FINGERPRINT_TTL_MIN: int = 60     # 指纹缓存复用窗口（分钟，0.3% 价格分桶）
    AI_MAX_PER_SCAN: int = 10         # 单次扫描批量 AI 分析上限（按24h成交额取前N）
    MIN_VOLUME_24H: float = 3_000_000  # 扫描候选池 24h 成交额下限（USDT），低于此不进扫描

    # 自动交易（docs/06，币安 USDT-M 合约）
    BINANCE_API_KEY: str = ""
    BINANCE_SECRET_KEY: str = ""
    TRADING_TESTNET: bool = True      # 首期走 testnet 验证链路
    TRADING_ENABLED: bool = False     # 自动交易总开关
    TRADING_MIN_RECOMMENDATION: float = 60.0  # 开单推荐度门槛
    TRADING_RISK_PCT: float = 3.0     # 止损金额占风险基数比例（%）
    TRADING_CAPITAL_TIERS: str = "100,200,500,1000,2000,5000,10000"  # 风险基数分档（向下落档）
    TRADING_MIN_FREE_PCT: float = 0.5  # 可用余额/总资金 下限（2026-09-15 由 70% 改 50%；系统配置表 trading_min_free_pct 可改，此值仅作缺列/缺行兜底）
    TRADING_LEVERAGE: int = 20        # 杠杆（逐仓 ISOLATED）

    class Config:
        env_file = ".env"


settings = Settings()
