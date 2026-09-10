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
from app.models.system_config import SystemConfig
from app.services.exchange_pool import ExchangePool
from app.services.strategy import detect_all_signals

logger = logging.getLogger(__name__)


def classify_volume(klines: list[list]) -> tuple[float, str]:
    """提取最新已收盘 K 线成交量并分类。

    klines[-1] 是未收盘 K 线，用 klines[-2]；前 20 根用 klines[-22:-2]。
    返回 (volume, volume_type)。
    """
    if len(klines) < 22:
        return 0.0, "平量"
    vol = float(klines[-2][5])
    prev_vols = [float(k[5]) for k in klines[-22:-2]]
    avg = sum(prev_vols) / len(prev_vols)
    if avg <= 0:
        return vol, "平量"
    ratio = vol / avg
    if ratio < 0.5:
        vt = "地量"
    elif ratio < 0.8:
        vt = "缩量"
    elif ratio <= 1.2:
        vt = "平量"
    elif ratio < 2.0:
        vt = "放量"
    else:
        vt = "倍量"
    return vol, vt


def _get_scan_config(db) -> SystemConfig:
    """从数据库读取扫描策略配置"""
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        # 回退到 env 默认值
        cfg = SystemConfig(
            id=1,
            ai_analysis_enabled=False,
            kline_interval=settings.KLINE_INTERVAL,
            kline_window=settings.KLINE_WINDOW,
            breakout_threshold=settings.BREAKOUT_THRESHOLD,
            r_squared_threshold=settings.R_SQUARED_THRESHOLD,
            repeat_window_hours=settings.REPEAT_WINDOW_HOURS,
            swing_order=settings.SWING_ORDER,
            pullback_tolerance=settings.PULLBACK_TOLERANCE,
        )
    return cfg


class Scanner:
    """扫描编排器（同步 + 线程池并发）"""

    def __init__(self):
        self.pool = ExchangePool()
        self.concurrency = settings.BINANCE_CONCURRENCY

    def run(self, scan_record_id: UUID) -> None:
        db = SessionLocal()
        status = "failed"
        hit_count = 0
        error_count = 0
        coin_count = 0
        try:
            record = db.get(ScanRecord, scan_record_id)
            if not record:
                logger.error("扫描记录不存在: %s", scan_record_id)
                return

            # 从数据库读取扫描策略配置
            cfg = _get_scan_config(db)
            kline_interval = cfg.kline_interval
            kline_window = cfg.kline_window
            repeat_window_hours = cfg.repeat_window_hours

            logger.info("开始扫描 #%s (interval=%s, window=%d)", scan_record_id, kline_interval, kline_window)

            # 1. 获取合约交易对列表（按 24h 成交额降序）
            symbols_data = self.pool.get_usdt_swap_symbols_with_volume()

            coin_count = len(symbols_data)
            logger.info("共 %d 个合约交易对待扫描（按24h成交额降序）", len(symbols_data))

            # 构建 symbol -> volume 映射
            volume_map = {s["symbol"]: s["volume_24h"] for s in symbols_data}
            symbols = [s["symbol"] for s in symbols_data]

            # 2. 线程池并发扫描每个币种
            hits: list[dict] = []

            def scan_one(symbol: str) -> tuple[str, list[dict], bool]:
                """返回 (symbol, [signals...], is_error)"""
                try:
                    klines = self.pool.get_klines(
                        symbol,
                        interval=kline_interval,
                        limit=kline_window,
                    )
                    config = {
                        "min_klines": 30,
                        "swing_order": cfg.swing_order,
                        "breakout_threshold": float(cfg.breakout_threshold),
                        "r_squared_threshold": float(cfg.r_squared_threshold),
                        "pullback_tolerance": float(cfg.pullback_tolerance),
                        "key_level_tolerance": float(cfg.key_level_tolerance),
                        "level_merge_threshold": float(cfg.level_merge_threshold),
                        "max_trend_slope": 0.005,
                    }
                    signals = detect_all_signals(klines, config)
                    vol, vol_type = classify_volume(klines)
                    for sig in signals:
                        sig["volume"] = vol
                        sig["volume_type"] = vol_type
                    return symbol, signals, False
                except Exception as e:
                    logger.warning("扫描 %s 失败: %s", symbol, e)
                    return symbol, [], True

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(scan_one, s): s for s in symbols}
                for future in as_completed(futures):
                    symbol, signals, is_error = future.result()
                    if is_error:
                        error_count += 1
                    for sig in signals:
                        sig["volume_24h"] = volume_map.get(symbol, 0)
                        hits.append({"symbol": symbol, **sig})

            # 3. 按 24h 成交额降序排列命中结果
            hits.sort(key=lambda x: x.get("volume_24h", 0), reverse=True)

            # 4. 标记重复命中（用独立 session 避免死锁）
            hit_symbols = [h["symbol"] for h in hits]
            repeat_symbols = self._get_repeat_symbols_safe(hit_symbols, repeat_window_hours)

            # 5. 写入结果（用独立 session，避免长时间扫描后主 session 连接失效）
            self._save_results(scan_record_id, hits, repeat_symbols)

            hit_count = len(hits)
            status = "completed"

            logger.info(
                "扫描完成 #%s: 扫描%d个, 命中%d个",
                scan_record_id,
                len(symbols),
                len(hits),
            )
        except Exception as e:
            logger.exception("扫描异常 #%s: %s", scan_record_id, e)
        finally:
            # 确保扫描状态总是更新（用独立 session 避免事务已中止）
            try:
                db.rollback()
            except Exception:
                pass
            self._update_scan_status(scan_record_id, status, hit_count, error_count, coin_count)
            db.close()

    @staticmethod
    def _save_results(
        scan_record_id: UUID,
        hits: list[dict],
        repeat_symbols: set[str],
    ) -> None:
        """用独立 session 写入扫描结果，避免主 session 连接失效或死锁"""
        if not hits:
            return
        sdb = SessionLocal()
        try:
            for h in hits:
                sdb.add(
                    ScanResult(
                        scan_record_id=scan_record_id,
                        symbol=h["symbol"],
                        signal_type=h.get("signal_type", "unknown"),
                        current_price=h["current_price"],
                        breakout_pct=h["breakout_pct"],
                        trend_slope=h.get("trend_slope", 0),
                        r_squared=h.get("r_squared", 0),
                        pattern=h.get("pattern"),
                        signal_reason=h.get("signal_reason"),
                        position=h.get("position"),
                        key_levels=h.get("key_levels"),
                        volume_24h=h.get("volume_24h", 0),
                        volume=h.get("volume", 0),
                        volume_type=h.get("volume_type", "平量"),
                        is_repeat=h["symbol"] in repeat_symbols,
                    )
                )
            sdb.commit()
            logger.info("写入 %d 条扫描结果 #%s", len(hits), scan_record_id)
        except Exception as e:
            logger.exception("写入扫描结果失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
            raise
        finally:
            sdb.close()

    @staticmethod
    def _update_scan_status(
        scan_record_id: UUID,
        status: str,
        hit_count: int,
        error_count: int,
        coin_count: int,
    ) -> None:
        """独立 session 更新扫描状态，确保即使主事务失败也能写入"""
        sdb = SessionLocal()
        try:
            record = sdb.get(ScanRecord, scan_record_id)
            if record:
                record.status = status
                record.hit_count = hit_count
                record.error_count = error_count
                if coin_count:
                    record.coin_count = coin_count
                record.finished_at = datetime.utcnow()
                sdb.commit()
        except Exception as e:
            logger.exception("更新扫描状态失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _get_repeat_symbols_safe(symbols: list[str], repeat_window_hours: int) -> set[str]:
        """检查重复命中，用独立 session 避免与主事务死锁"""
        if not symbols:
            return set()
        sdb = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(hours=repeat_window_hours)
            rows = (
                sdb.execute(
                    select(ScanResult.symbol)
                    .where(ScanResult.symbol.in_(symbols), ScanResult.created_at >= since)
                    .distinct()
                )
                .scalars()
                .all()
            )
            return set(rows)
        except Exception as e:
            logger.warning("查询重复命中失败（忽略）: %s", e)
            sdb.rollback()
            return set()
        finally:
            sdb.close()

    @staticmethod
    def _get_repeat_symbols(db: Session, symbols: list[str], repeat_window_hours: int) -> set[str]:
        """检查哪些币种在 repeat_window_hours 内已有命中记录"""
        if not symbols:
            return set()
        since = datetime.utcnow() - timedelta(hours=repeat_window_hours)
        rows = db.execute(
            select(ScanResult.symbol)
            .where(ScanResult.symbol.in_(symbols), ScanResult.created_at >= since)
            .distinct()
        ).scalars().all()
        return set(rows)
