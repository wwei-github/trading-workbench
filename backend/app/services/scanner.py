import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanRecord, ScanResult
from app.services.binance_client import BinanceClient
from app.services.strategy import detect_all_signals

logger = logging.getLogger(__name__)


class Scanner:
    """扫描编排器（同步 + 线程池并发）"""

    def __init__(self):
        self.client = BinanceClient()
        self.concurrency = settings.BINANCE_CONCURRENCY

    def run(self, scan_record_id: UUID) -> None:
        db = SessionLocal()
        try:
            record = db.get(ScanRecord, scan_record_id)
            if not record:
                logger.error("扫描记录不存在: %s", scan_record_id)
                return

            logger.info("开始扫描 #%s", scan_record_id)

            # 1. 获取交易对列表
            try:
                symbols = self.client.get_usdt_symbols()
            except Exception as e:
                logger.exception("获取交易对列表失败: %s", e)
                record.status = "failed"
                record.finished_at = datetime.utcnow()
                db.commit()
                return

            record.coin_count = len(symbols)
            db.commit()
            logger.info("共 %d 个交易对待扫描", len(symbols))

            # 2. 线程池并发扫描每个币种
            hits: list[dict] = []
            errors = 0

            def scan_one(symbol: str) -> tuple[str, list[dict], bool]:
                """返回 (symbol, [signals...], is_error)"""
                try:
                    klines = self.client.get_klines(
                        symbol,
                        interval=settings.KLINE_INTERVAL,
                        limit=settings.KLINE_WINDOW,
                    )
                    config = {
                        "min_klines": 30,
                        "swing_order": settings.SWING_ORDER,
                        "breakout_threshold": settings.BREAKOUT_THRESHOLD,
                        "r_squared_threshold": settings.R_SQUARED_THRESHOLD,
                        "pullback_tolerance": settings.PULLBACK_TOLERANCE,
                        "max_trend_slope": 0.005,
                    }
                    signals = detect_all_signals(klines, config)
                    return symbol, signals, False
                except Exception as e:
                    logger.warning("扫描 %s 失败: %s", symbol, e)
                    return symbol, [], True

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(scan_one, s): s for s in symbols}
                for future in as_completed(futures):
                    symbol, signals, is_error = future.result()
                    if is_error:
                        errors += 1
                    for sig in signals:
                        hits.append({"symbol": symbol, **sig})

            record.error_count = errors

            # 3. 标记重复命中
            hit_symbols = [h["symbol"] for h in hits]
            repeat_symbols = self._get_repeat_symbols(db, hit_symbols)

            # 4. 写入结果
            for h in hits:
                db.add(
                    ScanResult(
                        scan_record_id=scan_record_id,
                        symbol=h["symbol"],
                        signal_type=h.get("signal_type", "downtrend_breakout"),
                        current_price=h["current_price"],
                        breakout_pct=h["breakout_pct"],
                        trend_slope=h.get("trend_slope", 0),
                        r_squared=h.get("r_squared", 0),
                        is_repeat=h["symbol"] in repeat_symbols,
                    )
                )

            record.hit_count = len(hits)
            record.status = "completed"
            record.finished_at = datetime.utcnow()
            db.commit()

            logger.info(
                "扫描完成 #%s: 扫描%d个, 命中%d个",
                scan_record_id,
                len(symbols),
                len(hits),
            )
        except Exception as e:
            logger.exception("扫描异常 #%s: %s", scan_record_id, e)
            record = db.get(ScanRecord, scan_record_id)
            if record:
                record.status = "failed"
                record.finished_at = datetime.utcnow()
                db.commit()
        finally:
            self.client.close()
            db.close()

    @staticmethod
    def _get_repeat_symbols(db: Session, symbols: list[str]) -> set[str]:
        """检查哪些币种在 REPEAT_WINDOW_HOURS 内已有命中记录"""
        if not symbols:
            return set()
        since = datetime.utcnow() - timedelta(hours=settings.REPEAT_WINDOW_HOURS)
        rows = db.execute(
            select(ScanResult.symbol)
            .where(ScanResult.symbol.in_(symbols), ScanResult.created_at >= since)
            .distinct()
        ).scalars().all()
        return set(rows)
