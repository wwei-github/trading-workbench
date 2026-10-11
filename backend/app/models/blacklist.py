"""币种黑名单模型（2026-09-18，GRIFFAIN/4USDT 批次教训）

三个来源：
- manual：用户在页面手动添加（永久，除非手动移除）
- auto：止损金额严重超出计划风险预算（≥1.2 倍/强平）自动加入，冷却 30 天，
  可提前手动移除
- stop_hard：止损实亏 ≥ 计划风险 1.5 倍（2026-10-11 用户拍板）自动加入，
  永久生效，只能手动移出（expires_at 恒 NULL）

过滤位置：扫描宇宙阶段（进不了指标候选）+ AI 分析/开仓兜底（扫描漏网时双保险）
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SymbolBlacklist(Base):
    __tablename__ = "symbol_blacklist"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    # manual = 手动添加（永久）；auto = 止损严重超预算自动加入（冷却期）；
    # stop_hard = 止损实亏 ≥1.5R（永久，只能手动移出）
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    # 人工备注 / 自动理由（含计划风险与实亏金额）
    reason: Mapped[str] = mapped_column(Text, default="")

    # auto 记录的过期时间（到点自动失效）；manual 恒 NULL（永久）
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 手动停用（不删行，保留历史；恢复=置 false）
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # symbol 唯一约束自带索引，无需再建 Index（否则 create_all 与迁移重复建
    # 同名索引，beat 启动崩溃 DuplicateTable——2026-09-18 部署实测）
