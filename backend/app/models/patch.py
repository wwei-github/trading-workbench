"""提示词补丁模型（docs/12 §10 补丁闭环）

AI 错误归因发现某错误标签高频出现 → 生成规则补丁草稿 → 人工确认合入
（写 strategy_prompt 覆盖段或程序参数列）→ 每日评估三态（有效/无效/恶化）
→ 页面一键回滚。系统自带提示词（代码常量）永不改动。
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid():
    return str(uuid4())


class PromptPatch(Base):
    __tablename__ = "prompt_patches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 目标错误标签（ai_attribution.ERROR_TAGS 词表内，排除非规则性标签）
    target_tag: Mapped[str] = mapped_column(String(32), index=True)
    # 来源：ai=AI 复盘任务生成（经人工确认）/ manual=手动（预留）
    origin: Mapped[str] = mapped_column(String(8), default="ai")
    # 补丁类型：prompt=提示词覆盖规则 / param=程序参数建议值
    kind: Mapped[str] = mapped_column(String(8))
    # 参数类的参数键（max_stop_pct / liq_gate_buffer）
    param_key: Mapped[str] = mapped_column(String(32), default="")
    # 提示词类：old_text=系统原文段落引用（仅展示，程序校验逐字存在）
    old_text: Mapped[str] = mapped_column(Text, default="")
    # 提示词类：覆盖规则文本（合入时自动加 patch 标记注释）；参数类：建议理由
    new_text: Mapped[str] = mapped_column(Text)
    # 参数类建议值（字符串存储，apply 时按 param_key 类型转换）
    param_value: Mapped[str] = mapped_column(String(32), default="")
    # 触发证据：{heat, sample, case_trade_ids, window_days}
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 合入前基线（apply 时快照）：{heat, ev_per_trade, win_rate, pass_rate, window}
    metrics_before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 合入前来源值（2026-10-21 T5）：param 类记录合入前的自定义值
    # {"param_key":..., "param_value": 旧值或 None, "decision_version": 旧版本}；
    # 回滚恢复到该值，而非一律回落 NULL/默认。旧补丁无本快照（空 dict）时
    # 保持原行为（回落默认），无法还原更早的自定义值
    before_value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 评估结果明细：{after: {...}, before: {...}, checks: [...]}
    metrics_after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 三态判定：pending(评估中) / valid / invalid / worse（恶化告警）
    verdict: Mapped[str] = mapped_column(String(8), default="pending", index=True)
    # 生命周期：draft 待确认 / active 评估中 / valid 有效（终评通过）/
    # invalid 无效（终评不过）/ rolled_back 已回滚
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    applied_at: Mapped[object] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=datetime.utcnow)
