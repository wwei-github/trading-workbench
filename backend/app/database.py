from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migrations(engine):
    """幂等迁移：create_all 不会给已存在表加列，用 ALTER TABLE 补充。"""
    from app.config import settings

    with engine.begin() as conn:
        # Feature 1: scan_results 加 volume / volume_type
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS volume NUMERIC(20,8) NOT NULL DEFAULT 0,
              ADD COLUMN IF NOT EXISTS volume_type VARCHAR(16) NOT NULL DEFAULT '平量'
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_scan_results_volume_type "
            "ON scan_results (volume_type)"
        ))
        # Feature 2: system_config 加扫描策略列（若表已存在但缺列）
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS kline_interval VARCHAR(8) NOT NULL DEFAULT '1h',
              ADD COLUMN IF NOT EXISTS kline_window INTEGER NOT NULL DEFAULT 240,
              ADD COLUMN IF NOT EXISTS breakout_threshold NUMERIC(10,6) NOT NULL DEFAULT 0.005,
              ADD COLUMN IF NOT EXISTS r_squared_threshold NUMERIC(10,6) NOT NULL DEFAULT 0.5,
              ADD COLUMN IF NOT EXISTS repeat_window_hours INTEGER NOT NULL DEFAULT 24,
              ADD COLUMN IF NOT EXISTS swing_order INTEGER NOT NULL DEFAULT 3,
              ADD COLUMN IF NOT EXISTS pullback_tolerance NUMERIC(10,6) NOT NULL DEFAULT 0.03,
              ADD COLUMN IF NOT EXISTS strategy_prompt_enabled BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS strategy_prompt TEXT NOT NULL DEFAULT '',
              ADD COLUMN IF NOT EXISTS key_level_tolerance NUMERIC(10,6) NOT NULL DEFAULT 0.005,
              ADD COLUMN IF NOT EXISTS level_merge_threshold NUMERIC(10,6) NOT NULL DEFAULT 0.005,
              ADD COLUMN IF NOT EXISTS fib_enabled BOOLEAN NOT NULL DEFAULT FALSE
        """))
        # Feature 3: 关键位筛选重构（docs/04）—— scan_results 加位置与关键位明细
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS position VARCHAR(32),
              ADD COLUMN IF NOT EXISTS key_levels JSON
        """))
        # 确保系统配置表有默认行（初始值从 env 注入）
        conn.execute(text(
            "INSERT INTO system_config "
            "(id, ai_analysis_enabled, kline_interval, kline_window, "
            " breakout_threshold, r_squared_threshold, repeat_window_hours, "
            " swing_order, pullback_tolerance, notes) "
            "VALUES (1, false, :kline_interval, :kline_window, "
            " :breakout_threshold, :r_squared_threshold, :repeat_window_hours, "
            " :swing_order, :pullback_tolerance, '系统运行时配置') "
            "ON CONFLICT (id) DO NOTHING"
        ),
            {
                "kline_interval": settings.KLINE_INTERVAL,
                "kline_window": settings.KLINE_WINDOW,
                "breakout_threshold": settings.BREAKOUT_THRESHOLD,
                "r_squared_threshold": settings.R_SQUARED_THRESHOLD,
                "repeat_window_hours": settings.REPEAT_WINDOW_HOURS,
                "swing_order": settings.SWING_ORDER,
                "pullback_tolerance": settings.PULLBACK_TOLERANCE,
            },
        )


def init_db():
    """创建所有表 + 幂等迁移"""
    from app.models import scan  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_migrations(engine)
