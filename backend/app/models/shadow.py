"""影子模式打标模型（docs/14，2026-10-04）

ShadowMark：候选信号特征「只计算+打标+记录，不参与任何真实决策」；
事后按 feature × 结果 分组统计，为 docs/13 各条目的升格/关闭提供数据。

- 单表通用所有特征（不同特征挂不同管线点），不侵入 ScanResult/AIAnalysis 列
- value_json 存原始数值 + 分桶结论（防评估期改口径需重算历史）
- decision_json 存打标时刻真实系统处置快照（消除幸存者偏差）
- 幂等：uq_shadow_mark (feature_key, ref_type, ref_id) 唯一，重复打标覆盖
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, DateTime, JSON, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ShadowMark(Base):
    __tablename__ = "shadow_marks"
    __table_args__ = (
        UniqueConstraint("feature_key", "ref_type", "ref_id", name="uq_shadow_mark"),
        Index("ix_shadow_marks_feature_time", "feature_key", "created_at"),
        Index("ix_shadow_marks_ref", "ref_type", "ref_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 特征标识：cvd_div / vol_regime / ...（docs/14 §四 特征注册表 key）
    feature_key: Mapped[str] = mapped_column(String(32), nullable=False)
    # 标记宿主：scan_result / ai_analysis / trade（docs/14 §五 归因链路）
    ref_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 特征值 + 分桶 + 计算快照：如 {"price_chg_pct": 2.1, "cvd_chg_pct": -0.4,
    #   "bucket": "diverge_bear"}（docs/14 §三：原始数值落库防事后改口径）
    value_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 打标时刻真实系统行为快照：{"scan_type": "scheduled", "stage": "scan_hit"}
    decision_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
