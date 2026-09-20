"""AI 建议复盘任务（docs/04 §4 Stage 6 / P2）

对 suggest 决策在 24h 观察期后做一次 1h K 线逐根回放：
- 先触止损 → loss；先触 tp2 → win_tp2；触 tp1（后又触止损）→ win_tp1
- 同一根 K 线同时触碰止损与止盈 → 保守计 loss
- 观察期内未触任何价位 → expired
统计维度快照（signal_type/position/ema_state）随复盘落库，供分组胜率统计。
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import AIAnalysis, ScanResult, TradeReview
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.exchange_pool import ExchangePool
from app.services.task_log import close_task, open_task

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
    task_id = ""
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
            return  # 无待复盘不记任务记录（每 30 分钟一次，避免刷屏）
        logger.info("复盘任务：%d 条建议待复盘", len(rows))
        task_id = open_task(task_type="review", task_name="建议复盘", trigger="scheduled")
        reviewed = 0
        # 明细随任务记录落库（前端任务记录页可展开查看）：逐条结论 + 分组计数
        items = []
        counts = {"win_tp1": 0, "win_tp2": 0, "loss": 0, "expired": 0}

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
                reviewed += 1
                counts[outcome] = counts.get(outcome, 0) + 1
                items.append({
                    "symbol": a.symbol,
                    "direction": a.direction,
                    "outcome": outcome,
                    "bars_to_exit": nbars or None,
                    "recommendation": float(a.recommendation) if a.recommendation is not None else None,
                })
                logger.info("复盘完成: %s %s -> %s", a.symbol, a.direction, outcome)
            except Exception as e:
                logger.warning("复盘 %s 失败: %s", a.symbol, e)
                db.rollback()
                continue
        close_task(task_id, "completed", summary=f"复盘 {reviewed} 条建议", detail={
            "counts": counts,
            "items": items,
        })
    except Exception as e:
        logger.exception("复盘任务失败: %s", e)
        close_task(task_id, "failed", error=e)
    finally:
        db.close()


# ===== AI 错误归因（复盘重设计 2026-09-18） =====

# 单轮最多归因笔数（每笔一次 LLM 调用，控制开销与单轮时长）
ATTRIBUTION_BATCH = 5


def run_ai_attribution(db, pool, trader, trades: list) -> list[dict]:
    """对给定已平仓交易逐笔 AI 归因并落库（定时任务与手动触发共用）。

    返回逐笔结果 [{symbol, ok, tags/lesson/error}]。
    """
    from app.services.ai_attribution import _interval_ms, attribute_trade

    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"

    results = []
    for rec in trades:
        item = {"symbol": rec.symbol, "opened_at": rec.opened_at.isoformat() if rec.opened_at else None}
        try:
            # K 线区间：开仓前 ~100 根 + 持仓全程（与交易复盘K线同源口径）
            opened = rec.opened_at
            closed = rec.closed_at or datetime.utcnow()
            held_bars = 0
            if opened:
                held_bars = int((closed - opened).total_seconds() * 1000 / _interval_ms(interval)) + 1
            bars = min(100 + max(held_bars, 1), 480)
            end_ms = int(closed.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1
            klines = pool.get_klines_range(rec.symbol, interval, bars, end_ms) or []

            verdict = attribute_trade(rec, klines)
            rec.error_tags = verdict["tags"]
            rec.error_lesson = verdict["lesson"]
            rec.attributed_at = datetime.utcnow()
            db.commit()
            item.update(ok=True, tags=verdict["tags"], lesson=verdict["lesson"])
            logger.info("AI 归因 %s %s: %s", rec.symbol, rec.direction, verdict["tags"])
        except Exception as e:
            db.rollback()
            item.update(ok=False, error=str(e)[:200])
            logger.warning("AI 归因失败 %s: %s", rec.symbol, e)
        results.append(item)
    return results


@celery_app.task(name="app.tasks.review_tasks.run_ai_attribution_task", max_retries=0)
def run_ai_attribution_task():
    """定时归因：找已平仓未归因的交易，逐笔 AI 复盘出错误标签与教训"""
    if not settings.AI_API_KEY:
        return  # AI 未配置，静默跳过
    db = SessionLocal()
    pool = ExchangePool()
    trader = None
    task_id = ""
    try:
        # 费用回补先行（归因事实包需要 fee/funding）；环境以 DB 为运行时权威
        from app.services.binance_trader import BinanceTrader
        from app.services.actual_review import backfill_costs
        cfg = db.get(SystemConfig, 1)
        env_testnet = bool(cfg.trading_testnet) if cfg else settings.TRADING_TESTNET
        trader = BinanceTrader(env_testnet)
        backfill_costs(db, trader, limit=ATTRIBUTION_BATCH)

        trades = db.execute(
            select(TradeRecord)
            .where(
                TradeRecord.status == "CLOSED",
                TradeRecord.exit_reason.isnot(None),
                TradeRecord.attributed_at.is_(None),
                TradeRecord.testnet == env_testnet,
            )
            .order_by(TradeRecord.closed_at.desc())
            .limit(ATTRIBUTION_BATCH)
        ).scalars().all()
        if not trades:
            return  # 无待归因不记任务记录（避免刷屏）

        task_id = open_task(task_type="review", task_name="AI错误归因", trigger="scheduled")
        results = run_ai_attribution(db, pool, trader, trades)
        ok_n = sum(1 for r in results if r.get("ok"))
        close_task(task_id, "completed", summary=f"归因 {ok_n}/{len(results)} 笔", detail={
            "items": results,
        })
    except Exception as e:
        logger.exception("AI 归因任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()


# ===== 规则补丁闭环（docs/12 §10，2026-09-18） =====

@celery_app.task(name="app.tasks.review_tasks.run_patch_trigger_task", max_retries=0)
def run_patch_trigger_task():
    """每日触发：标签热度检查 → AI 生成补丁草稿（待人工确认合入）"""
    if not settings.AI_API_KEY:
        return
    db = SessionLocal()
    task_id = ""
    try:
        from app.services.patch_service import run_trigger
        task_id = open_task(task_type="review", task_name="补丁触发", trigger="scheduled")
        result = run_trigger(db)
        drafts = result["drafts"]
        summary = f"生成 {len(drafts)} 条补丁草稿"
        if not drafts and result["skips"]:
            summary = f"未生成补丁：{result['skips'][0]}"
        close_task(task_id, "completed",
                    summary=summary,
                    detail={
                        # 未生成时 skips 直接给出卡在哪关（样本/热度/已有草稿/冷却）
                        "skips": result["skips"],
                        "attributed_total_7d": result["attributed_total"],
                        "drafts": [
                            {"tag": d.target_tag, "kind": d.kind, "id": d.id}
                            for d in drafts
                        ],
                    })
    except Exception as e:
        logger.exception("补丁触发任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()


@celery_app.task(name="app.tasks.review_tasks.run_patch_evaluate_task", max_retries=0)
def run_patch_evaluate_task():
    """每日评估：active 补丁的三态判定（有效/无效/恶化告警）"""
    db = SessionLocal()
    task_id = ""
    try:
        from app.services.patch_service import run_evaluate
        task_id = open_task(task_type="review", task_name="补丁评估", trigger="scheduled")
        updated = run_evaluate(db)
        alerts = [p for p in updated if p.verdict == "worse"]
        close_task(task_id, "completed",
                   summary=f"评估 {len(updated)} 条补丁，告警 {len(alerts)} 条",
                   detail={"alerts": [
                       {"tag": p.target_tag, "id": p.id,
                        "checks": p.metrics_after_json.get("checks")}
                       for p in alerts
                   ]})
    except Exception as e:
        logger.exception("补丁评估任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()
