# -*- coding: utf-8 -*-
"""AI 战绩回注（2026-09-28）：把复盘统计（/review/stats 同源口径）注入分析提示词。

数据闭环：TradeReview（24h 逐K回放判定 outcome）→ 按原始 signal_type 聚合
胜率/期望值 → 分析时作为「自我战绩」块注入事实包，让 AI 评估时参考同触发
类型的历史表现（如突破家族胜率低则更谨慎）。

口径与 /review/stats 对齐（2026-09-25 拆细）：按原始 signal_type 分组、不与旧
管线键合并——trigger_pullback_confirm 与旧一段式隔离，避免污染新触发器评估。
查询失败/无数据/样本不足一律返回空串，绝不阻断分析。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scan import AIAnalysis, TradeReview

logger = logging.getLogger(__name__)

# 注入白名单：仅新管线触发器（旧管线类型已停产，样本只反映历史行为，不注入）
_INJECTABLE = {
    "trigger_breakout": "同向突破",
    "trigger_trend_breakout": "趋势突破",
    "trigger_pullback_confirm": "回踩金K确认",
    "trigger_candlestick": "同向形态",  # 2026-09-29 纯金K下线，仅历史行
}


def build_track_record_block(
    db: Session, signal_type: str, days: int = 30, min_sample: int = 5,
    touch_ref: Optional[str] = None,
) -> str:
    """生成当前信号类型的历史战绩块（注入 user 提示词）。

    胜率口径：win=win_tp1/win_tp2，loss=loss，expired 不计入；EV_R 与
    /review/stats 同式（loss=-1R，tp1=RR1，tp2=RR2，扣 taker 往返+资金费）。
    touch_ref（docs/22 触点体系）：非空时优先按（signal_type+touch_ref）分组，
    样本 < min_sample 回落纯 signal_type 组（触点维度细但样本积累慢）。
    样本 < min_sample 或查询失败 → 空串（不给 AI 噪音小样本）。
    """
    family = _INJECTABLE.get(signal_type or "")
    if not family:
        return ""
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        base = [
            TradeReview.created_at >= cutoff,
            TradeReview.signal_type == signal_type,
        ]
        # 触点分组优先（同触点位的回放行为才可比），不足回落类型组
        rows = []
        scoped = False
        if touch_ref:
            rows = db.execute(
                select(TradeReview).where(
                    *base, TradeReview.touch_ref == touch_ref,
                )
            ).scalars().all()
            scoped = len(rows) >= min_sample
        if not scoped:
            rows = db.execute(select(TradeReview).where(*base)).scalars().all()
    except Exception as e:
        logger.warning("战绩块查询失败，跳过注入: %s", e)
        return ""

    if len(rows) < min_sample:
        return ""
    wins = sum(1 for r in rows if r.outcome in ("win_tp1", "win_tp2"))
    losses = sum(1 for r in rows if r.outcome == "loss")
    expired = sum(1 for r in rows if r.outcome == "expired")
    if not (wins + losses):
        return ""
    win_rate = wins / (wins + losses)

    ev_r = _avg_ev_r(db, rows)
    if scoped:
        from app.services.strategy.trigger import TOUCH_LABELS
        family = f"{family}·{TOUCH_LABELS.get(touch_ref, touch_ref)}"
    lines = [
        f"历史战绩（近{days}日回放，触发类型「{family}」，样本{len(rows)}笔）:",
        f"- 胜率 {win_rate:.0%}（盈{wins}/亏{losses}，未决{expired}笔不计入）",
    ]
    if ev_r is not None:
        lines.append(f"- 平均期望 {ev_r:+.2f}R（含费用假设）")
    lines.append(
        "- 评估本信号时参考该类型既往表现：胜率明显偏低（<45%）时提高 skip 门槛、"
        "降低 recommendation；胜率较高（>55%）时可适度给分，但仍以当下事实为准，勿仅凭战绩定论"
    )
    return "\n".join(lines) + "\n"


def _avg_ev_r(db: Session, rows: list) -> Optional[float]:
    """EV_R 均值（与 /review/stats._ev_r 同式；无 AI 三价的行跳过）。"""
    ai_ids = [r.ai_analysis_id for r in rows if r.ai_analysis_id]
    ai_map: dict = {}
    if ai_ids:
        for a in db.execute(
            select(AIAnalysis).where(AIAnalysis.id.in_(ai_ids))
        ).scalars().all():
            ai_map[str(a.id)] = a

    vals: list[float] = []
    for rev in rows:
        a = ai_map.get(str(rev.ai_analysis_id)) if rev.ai_analysis_id else None
        if not a or not a.entry_price or not a.stop_loss or not a.take_profit_1:
            continue
        entry, stop, tp1 = float(a.entry_price), float(a.stop_loss), float(a.take_profit_1)
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or entry <= 0:
            continue
        if rev.outcome == "loss":
            r_mult = -1.0
        elif rev.outcome == "win_tp1":
            r_mult = abs(tp1 - entry) / stop_dist
        elif rev.outcome == "win_tp2" and a.take_profit_2:
            r_mult = abs(float(a.take_profit_2) - entry) / stop_dist
        else:
            continue
        cost_pct = 0.001 + 0.0003 * max(1, (rev.bars_to_exit or 1) // 8)
        vals.append(r_mult - cost_pct / (stop_dist / entry))
    return round(sum(vals) / len(vals), 2) if vals else None
