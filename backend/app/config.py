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
    BREAKOUT_THRESHOLD: float = 0.005  # 0.5%
    R_SQUARED_THRESHOLD: float = 0.5
    REPEAT_WINDOW_HOURS: int = 24
    SWING_ORDER: int = 3
    PULLBACK_TOLERANCE: float = 0.03  # 回调容差 3%

    # 排除的币种关键字（杠杆代币等）
    EXCLUDE_KEYWORDS: list = ["UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG"]
    QUOTE_ASSET: str = "USDT"

    # AI 分析（OpenAI 兼容接口）
    AI_ENABLED: bool = False
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.openai.com/v1"
    AI_MODEL: str = "gpt-4o-mini"

    class Config:
        env_file = ".env"


settings = Settings()
