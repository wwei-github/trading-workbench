"""虚拟账本模型（docs/16 P-07，全新表——零侵入约束：不动既有表语义）

VirtualLedger：每条扫描候选（ScanResult，含 AI 未分析/否决的）一行规则三价
虚拟单，周期任务用 P-06 replay_exit 引擎推进到终局——判定样本从「已开单」
放大 5–10 倍，S-xx 影子卡片的分母杠杆；候选特征+终局同时是 N-01/N-02 的
训练数据集。

注意：JSON 列一律用可移植 JSON（严禁 PG 专用 JSONB——sqlite 内存库测试套件
create_all 直接编译失败，2026-10-07 system_config/spot 前车之鉴）。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VirtualLedger(Base):
    __tablename__ = "virtual_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ── 关联与来源 ──
    # live 行关联 ScanResult；backfill（源B重放）行 NULL
    scan_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_results.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long / short
    # live=扫描实时挂点；backfill=源B扫描重放回填
    source: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    # ── 候选特征快照（S-xx 分桶列显式化；N-01 特征集，其余进 snapshot_json）──
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    touch_ref: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    position: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ema_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pattern: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    votes_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # 票数快照（方向可审计）
    # 候选 dict 其余字段 + 开单上下文（scan_type / breakout_pct / src 等）
    snapshot_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── 规则三价（derive_signal_prices：entry=信号K收盘, stop=近5根极值±buf,
    #    tp1=1.5R, tp2=3R；entry_bar_open=信号K开盘 UTC naive）──
    # 回放入场锚 = entry_bar_open + 1h（收盘即入场，代码派生不落列）
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp1_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp2_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 0=不设
    r_unit: Mapped[float] = mapped_column(Float, nullable=False)
    entry_bar_open: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # ── 终局（P-04 口径：OPEN/EXPIRED 的 outcome/r 为 NULL，不进 decided 分母）──
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="OPEN", index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 含费净 R
    bars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    # replay_exit 全量返回（legs/sl_price/armed_be/trail_used）
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── AI 标注回填（N-02 评分校准；suggest/skip，重分析 latest-wins）──
    ai_decision: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    ai_recommendation: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── 口径追溯（跨 RULE_VERSION 不混算是负清单铁律）──
    rule_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    engine_version: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # 同币同信号类型同根信号K线唯一：手动/定时扫描同根K线去重、
        # 回填与 live 边界去重（冲突跳过保留先到行，可重入）——一箭三雕
        UniqueConstraint("symbol", "signal_type", "entry_bar_open", name="uq_ledger_candle"),
        Index("ix_ledger_status_entry", "status", "entry_bar_open"),
        Index("ix_ledger_scan_result_id", "scan_result_id"),
    )
