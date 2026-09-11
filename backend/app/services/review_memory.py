"""复盘记忆（docs/04 §4 Stage 6 / P2 记忆注入）

把近期 TradeReview 统计压缩成一段文字注入 AI 系统提示词，形成自我校准闭环。
参考 FinMem 分层思想：只注入"哪类信号常赢/常输"的趋势性结论，不逐条复述，
避免小样本误导与上下文膨胀。开关 system_config.memory_injection_enabled（默认关）。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.scan import TradeReview

logger = logging.getLogger(__name__)

DIGEST_DAYS = 30        # 统计窗口
DIGEST_MIN_SAMPLE = 30  # 全局复盘条数低于该值时输出"样本不足"提示（小样本不注入结论）


def _win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    return wins / total if total else 0.0


def build_review_digest(db) -> str:
    """生成复盘记忆摘要文本（中文，供系统提示词注入）。

    维度优先级：signal_type 分组（样本最多时最有指导性），其次 position、ema_state。
    """
    cutoff = datetime.utcnow() - timedelta(days=DIGEST_DAYS)
    rows = db.execute(
        select(TradeReview).where(TradeReview.created_at >= cutoff)
    ).scalars().all()
    total = len(rows)
    if total == 0:
        return ""

    wins = sum(1 for r in rows if r.outcome in ("win_tp1", "win_tp2"))
    losses = sum(1 for r in rows if r.outcome == "loss")
    expired = sum(1 for r in rows if r.outcome == "expired")

    lines = [
        f"近{DIGEST_DAYS}天复盘记忆（共{total}条已定论建议：胜{wins}/负{losses}/超时{expired}，"
        f"胜率{_win_rate(wins, losses) * 100:.0f}%）："
    ]
    if total < DIGEST_MIN_SAMPLE:
        lines.append("（样本尚少，仅作趋势参考，勿过度拟合）")

    for dim in ("signal_type", "position", "ema_state"):
        groups: dict[str, list] = {}
        for r in rows:
            key = getattr(r, dim)
            if key:
                groups.setdefault(key, []).append(r)
        # 只列样本 >=3 的分组，按胜率排序
        parts = []
        for key, items in sorted(
            groups.items(),
            key=lambda kv: _win_rate(
                sum(1 for i in kv[1] if i.outcome in ("win_tp1", "win_tp2")),
                sum(1 for i in kv[1] if i.outcome == "loss"),
            ),
        ):
            w = sum(1 for i in items if i.outcome in ("win_tp1", "win_tp2"))
            l = sum(1 for i in items if i.outcome == "loss")
            if w + l < 3:
                continue
            parts.append(f"{key} 胜率{_win_rate(w, l) * 100:.0f}%({w}胜{l}负)")
        if parts:
            lines.append(f"- 按{dim}: " + "；".join(parts))
    return "\n".join(lines)
