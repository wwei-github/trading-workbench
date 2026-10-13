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


def _current_rule_version() -> str:
    """打标规则版本默认值（惰性导入避循环）：评估按卡声明的 since_rule_version
    过滤，令「跨版本不混算」纪律可自动执行（docs/14 §六.1，2026-10-13）。"""
    from app.config import settings
    return settings.RULE_VERSION


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
    # 打标时刻的规则版本（settings.RULE_VERSION，2026-10-13 补列）：规则集变更
    # 改变候选池/出场链，「跨版本不混算」纪律——评估按卡声明的 since_rule_version
    # 过滤（docs/14 §六.1）；历史行 NULL，视为最早版本、自动出窗
    rule_version: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, default=_current_rule_version)


class ShadowEarlyStop(Base):
    """P-08 早停宣告落锁（docs/14 §七，2026-10-09）。

    预注册语义：序贯检验（SPRT）首次跨界（h1/h0）即终局宣告——宣告后该卡
    sequential 读数固定用宣告快照，不再随滚动窗口内新样本重算（应停不停
    会放大 α/β 错误率保证的失真；跨界→回摆→再跨界的状态抖动也污染面板）。
    build_report 首次算出跨界 verdict 时落一条（card 唯一，一卡一宣告），
    detail 存宣告时刻完整 seq 快照（llr/wins/n/p0/p1/posterior...）供展示。
    仅限预注册含早停通道的卡（CARD-07）；CARD-09 为固定窗口协议，不落锁。
    """
    __tablename__ = "shadow_early_stops"
    __table_args__ = (
        UniqueConstraint("card", name="uq_shadow_early_stop_card"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    card: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[str] = mapped_column(String(4), nullable=False)  # h1 / h0
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
