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


def init_db():
    """创建所有表 + 幂等迁移"""
    from app.models import scan  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_migrations(engine)
