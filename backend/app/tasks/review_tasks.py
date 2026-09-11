"""AI 建议复盘任务（docs/04 §4 Stage 6 / P2）

对 suggest 决策在 24h 观察期后做一次 1h K 线逐根回放：
- 先触止损 → loss；先触 tp2 → win_tp2；触 tp1（后又触止损）→ win_tp1
- 同一根 K 线同时触碰止损与止盈 → 保守计 loss
- 观察期内未触任何价位 → expired
统计维度快照（signal_type/position/ema_state）随复盘落库，供分组胜率统计。
"""
import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import AIAnalysis, ScanResult, TradeReview
from app.services.exchange_pool import ExchangePool

logger = logging.getLogger(__name__)

OBSERVATION_HOURS = 24


def replay_suggestion(
    direction: str, entry: float, stop: float, tp1: float, tp2: float,
    bars: list,
) -> tuple[str, bool, int]:
    """逐 K 回放。bars: [[ts,o,h,l,c,v,...]] 已收盘、时间升序。

    返回 (outcome, tp1_hit, bars_to_exit)；未定论 outcome="open"。
    """
    tp1_hit = False
    for i, k in enumerate(bars):
        high, low = float(k[2]), float(k[3])
        if direction == "long":
            hit_stop = low <= stop
            hit_tp1 = high >= tp1
            hit_tp2 = tp2 > 0 and high >= tp2
        else:
            hit_stop = high >= stop
            hit_tp1 = low <= tp1
            hit_tp2 = tp2 > 0 and low <= tp2

        if hit_stop:
            # 同根先到先得无法判定 → 保守 loss；但已落袋 tp1 的仍计 win_tp1
            return ("loss" if not tp1_hit else "win_tp1"), tp1_hit, i + 1
        if hit_tp2:
            return "win_tp2", True, i + 1
        if hit_tp1:
            tp1_hit = True

    return ("win_tp1" if tp1_hit else "open"), tp1_hit, len(bars)


@celery_app.task(name="app.tasks.review_tasks.run_trade_review_task", max_retries=0)
def run_trade_review_task():
    """定时复盘：找观察期结束且未复盘的 suggest 决策，逐 K 回放落库"""
    db = SessionLocal()
    pool = ExchangePool()
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=OBSERVATION_HOURS)
        stale = now - timedelta(hours=OBSERVATION_HOURS * 2)  # 数据缺口兜底

        rows = db.execute(
            select(AIAnalysis, ScanResult)
            .join(ScanResult, AIAnalysis.scan_result_id == ScanResult.id)
            .outerjoin(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
            .where(
                AIAnalysis.trade_decision == "suggest",
                AIAnalysis.created_at <= cutoff,
                TradeReview.id.is_(None),
            )
        ).all()
        if not rows:
            return
        logger.info("复盘任务：%d 条建议待复盘", len(rows))

        for a, sr in rows:
            try:
                klines = pool.get_klines(a.symbol, settings.KLINE_INTERVAL, 500)
                closed = klines[:-1] if len(klines) >= 2 else klines
                start_ms = int(a.created_at.replace(microsecond=0).timestamp() * 1000)
                bars = [k for k in closed if int(k[0]) >= start_ms]

                entry = float(a.entry_price or 0)
                stop = float(a.stop_loss or 0)
                tp1 = float(a.take_profit_1 or 0)
                tp2 = float(a.take_profit_2 or 0)
                if entry <= 0 or stop <= 0 or tp1 <= 0 or not a.direction:
                    outcome, tp1_hit, nbars = "expired", False, len(bars)
                elif not bars:
                    # 数据不足：超过双倍观察期仍无数据则定 expired，否则下轮再试
                    if a.created_at <= stale:
                        outcome, tp1_hit, nbars = "expired", False, 0
                    else:
                        continue
                else:
                    outcome, tp1_hit, nbars = replay_suggestion(
                        a.direction, entry, stop, tp1, tp2, bars
                    )
                    if outcome == "open":
                        continue  # 理论上不会发生（bars 覆盖 24h），防御

                db.add(TradeReview(
                    ai_analysis_id=UUID(str(a.id)),
                    symbol=a.symbol,
                    direction=a.direction,
                    outcome=outcome,
                    tp1_hit=tp1_hit,
                    bars_to_exit=nbars or None,
                    signal_type=sr.signal_type,
                    position=sr.position,
                    ema_state=sr.ema_state,
                    recommendation=float(a.recommendation) if a.recommendation is not None else None,
                ))
                db.commit()
                logger.info("复盘完成: %s %s -> %s", a.symbol, a.direction, outcome)
            except Exception as e:
                logger.warning("复盘 %s 失败: %s", a.symbol, e)
                db.rollback()
                continue
    except Exception as e:
        logger.exception("复盘任务失败: %s", e)
    finally:
        db.close()
