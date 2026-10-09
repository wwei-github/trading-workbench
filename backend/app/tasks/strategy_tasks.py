"""策略插件信号任务（docs/20 S1）：影子期产信号落库，绝不触发开单。

调度：run_scan_task 收尾后 fire-and-forget 派发（golden_k 全链路无感知）；
本任务任何异常只记日志，不影响扫描主流程（影子纪律，同 docs/14 §九）。
开单权限 S2 才解锁（经 docs/20 §4.2 重叠率/相关性评审），本文件刻意
不 import 任何执行层模块。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.scan import ScanResult
from app.models.strategy import StrategyShadowSignal
from app.models.system_config import SystemConfig
from app.services.exchange_pool import ExchangePool
from app.services.strategies import enabled_plugin_entries
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)

KLINE_LIMIT = 300  # 4h EMA144 斜率/缠绕需 ≥165 根已收盘，1h 结构需 ≥165，300 留余量
DEDUP_HOURS = 24   # 同策略同标的同方向信号去重窗口


def _recent_signal_keys(db, strategy_id: str) -> set[tuple[str, str]]:
    """去重窗口内已产信号 (symbol, direction) 集合。"""
    since = datetime.utcnow() - timedelta(hours=DEDUP_HOURS)
    rows = (
        db.query(StrategyShadowSignal.symbol, StrategyShadowSignal.direction)
        .filter(
            StrategyShadowSignal.strategy_id == strategy_id,
            StrategyShadowSignal.created_at >= since,
        )
        .all()
    )
    return {(r[0], r[1]) for r in rows}


@celery_app.task(name="app.tasks.strategy_tasks.run_strategy_signals_task",
                 max_retries=0)
def run_strategy_signals_task(symbols: Optional[list] = None,
                              scan_record_id: Optional[str] = None) -> dict:
    """遍历 enabled 插件策略，逐 symbol 产影子信号并落库。

    symbols 缺省时回落最近一轮扫描的标的集（绑定 golden_k 扫描节奏，
    控制行情 API 负载；后续可扩展独立 universe）。
    """
    db = SessionLocal()
    task_id = ""
    produced = 0
    try:
        cfg = db.get(SystemConfig, 1)
        registry_raw = cfg.strategy_registry if cfg else None
        plugins = [e for e in enabled_plugin_entries(registry_raw) if e.id != "golden_k_ai"]
        if not plugins:
            return {"skipped": "无启用的插件策略"}

        if not symbols:
            q = db.query(ScanResult.symbol)
            if scan_record_id:
                q = q.filter(ScanResult.scan_record_id == scan_record_id)
            else:
                latest = (
                    db.query(ScanResult.scan_record_id)
                    .order_by(ScanResult.created_at.desc())
                    .first()
                )
                q = q.filter(ScanResult.scan_record_id == latest[0]) if latest else q
            symbols = [r[0] for r in q.distinct().all()]
        if not symbols:
            return {"skipped": "universe 为空"}

        task_id = open_task(
            task_type="strategy_signals",
            task_name="策略影子信号",
            trigger="scan" if scan_record_id else "manual",
        )

        pool = ExchangePool()
        for entry in plugins:
            from app.services.strategies import get_strategy
            strategy = get_strategy(entry.id, registry_raw)
            seen = _recent_signal_keys(db, entry.id)
            for sym in strategy.scan_candidates(symbols):
                try:
                    k1 = pool.get_klines(sym, "1h", KLINE_LIMIT)
                    k4 = pool.get_klines(sym, "4h", KLINE_LIMIT)
                    sig = strategy.build_signal(sym, klines_1h=k1, klines_4h=k4)
                    if sig is None or (sym, sig.direction) in seen:
                        continue
                    db.add(StrategyShadowSignal(
                        strategy_id=sig.strategy_id, symbol=sig.symbol,
                        direction=sig.direction, entry=sig.entry, stop=sig.stop,
                        tp1=sig.tp1, tp2=sig.tp2, signal_type=sig.signal_type,
                        reason=sig.reason[:255] if sig.reason else None,
                        confidence=sig.confidence, raw=sig.raw,
                    ))
                    db.commit()
                    produced += 1
                except Exception as e:  # 影子纪律：单币失败吞掉，绝不影响其余
                    db.rollback()
                    logger.warning("影子信号 %s %s 失败: %s", entry.id, sym, e)
        summary = f"策略信号 {plugins[0].display_name if plugins else ''}：产出 {produced} 条"
        close_task(task_id, "completed", summary=summary,
                   detail={"produced": produced, "universe": len(symbols)})
        return {"produced": produced, "universe": len(symbols)}
    except Exception as e:
        logger.exception("策略影子信号任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
        return {"error": str(e)}
    finally:
        db.close()
