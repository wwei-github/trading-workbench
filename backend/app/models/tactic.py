"""战法模型（2026-10-02 用户拍板：战法仅作借鉴参考，不做硬闸）

用户自建"战术剧本"（名称+剧本描述），AI 分析时对照判定该信号是否符合
某个战法并落库标注（tactic_name/tactic_reason），复盘按战法分组对比
实盘胜率/EV——哪个战法真能打，数据说话。

定位边界：
- trade_type（trend_follow 等）= 程序结构分类，固定枚举不可自定义
- 策略提示词（skill_library）= 全局纪律，注入所有分析无个体判定
- 战法（本模型）= 用户自建、可启停、分环境，仅标注不拦截

快照自持：命中结果随 ai_snapshot 进 trade_records，删除战法不断链
（API 层已有交易引用时仍拒绝硬删，提示改停用）。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 方向适用：all/long/short（AI 判定与展示共用）
TACTIC_DIRECTIONS = ("all", "long", "short")
# 环境适用：testnet/live/both（分析时按信号所属环境过滤注入）
TACTIC_SCOPES = ("testnet", "live", "both")


class Tactic(Base):
    __tablename__ = "tactics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # 战法剧本（自由文本，≤2000 字，AI 判定的唯一依据）
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 适用方向 all/long/short；适用环境 testnet/live/both
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="all")
    scope: Mapped[str] = mapped_column(String(8), nullable=False, default="both")
    # 启停开关（新建默认停用，用户确认后启用才参与 AI 判定）
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
