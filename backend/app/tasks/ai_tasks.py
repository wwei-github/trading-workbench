"""AI 分析 Celery 任务"""
import logging
import time
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanResult, AIAnalysis
from app.models.system_config import SystemConfig
from app.services.ai_analyzer import analyze_coin
from app.services.binance_client import BinanceClient

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_task", bind=True, max_retries=0)
def run_ai_analysis_task(
    self, scan_record_id: str, only_scan_result_id: Optional[str] = None
):
    """对指定扫描记录的命中币种执行 AI 分析。

    scan_record_id: 扫描记录 ID
    only_scan_result_id: 若提供，只分析该单个 ScanResult（用于手动重新分析）
    """
    db = SessionLocal()
    client = BinanceClient()
    try:
        # 从数据库读取 AI 开关
        cfg = db.get(SystemConfig, 1)
        if not cfg or not cfg.ai_analysis_enabled or not settings.AI_API_KEY:
            logger.warning("AI 分析未启用，跳过")
            return
        q = select(ScanResult).where(ScanResult.scan_record_id == UUID(scan_record_id))
        if only_scan_result_id:
            q = q.where(ScanResult.id == UUID(only_scan_result_id))
        results = db.execute(q).scalars().all()

        logger.info("开始 AI 分析，共 %d 个币种", len(results))

        for r in results:
            try:
                klines = client.get_klines(
                    r.symbol, cfg.kline_interval, 100
                )
                signal = {
                    "symbol": r.symbol,
                    "signal_type": r.signal_type,
                    "current_price": float(r.current_price),
                    "breakout_pct": float(r.breakout_pct),
                    "pattern": r.pattern,
                    "signal_reason": r.signal_reason,
                    "volume_type": r.volume_type,
                    "volume": float(r.volume),
                    "volume_24h": float(r.volume_24h),
                }
                ai_result = analyze_coin(signal, klines)
                _upsert_ai_analysis(db, r.id, r.symbol, ai_result)
                db.commit()
                logger.info("AI 分析完成: %s", r.symbol)
                time.sleep(1.0)  # 限速
            except Exception as e:
                logger.warning("AI 分析 %s 失败: %s", r.symbol, e)
                db.rollback()
                continue

        logger.info("AI 分析批次完成")
    except Exception as e:
        logger.exception("AI 分析任务失败: %s", e)
    finally:
        client.close()
        db.close()


def _upsert_ai_analysis(db, scan_result_id: UUID, symbol: str, ai_result: dict):
    """latest-wins：有则更新，无则新建"""
    existing = db.execute(
        select(AIAnalysis).where(AIAnalysis.scan_result_id == scan_result_id)
    ).scalars().first()

    if existing:
        existing.analysis = ai_result.get("analysis")
        existing.entry_price = ai_result.get("entry_price")
        existing.stop_loss = ai_result.get("stop_loss")
        existing.take_profit_1 = ai_result.get("take_profit_1")
        existing.take_profit_2 = ai_result.get("take_profit_2")
        existing.risk_reward_ratio = ai_result.get("risk_reward_ratio")
        existing.position_pct = ai_result.get("position_pct")
    else:
        db.add(
            AIAnalysis(
                scan_result_id=scan_result_id,
                symbol=symbol,
                analysis=ai_result.get("analysis"),
                entry_price=ai_result.get("entry_price"),
                stop_loss=ai_result.get("stop_loss"),
                take_profit_1=ai_result.get("take_profit_1"),
                take_profit_2=ai_result.get("take_profit_2"),
                risk_reward_ratio=ai_result.get("risk_reward_ratio"),
                position_pct=ai_result.get("position_pct"),
            )
        )
