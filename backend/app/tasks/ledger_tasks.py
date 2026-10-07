"""虚拟账本周期任务（docs/16 P-07）：推进 OPEN 行到终局 + 源B 扫描重放回填。

推进（hourly，beat :18）：最老的 OPEN 行优先（单轮 400 行上限 + ~900s 时间
预算，积压有界收敛），按币分组拉 1h/1d K线（_load_klines 缓存优先+限速，
复用 P-06 backtest_tasks 同款），replay_exit 基线（生产语义）回放：
- 终局 → DONE（outcome/r/decided_at/result_json）
- 窗口内未终局 → 保持 OPEN（下轮续推；P-04 口径：不进 decided 分母）
- 账龄超 30 天且有 K 线佐证 → EXPIRED（outcome/r 保持 NULL，单列不进分母）

回填（API 手动触发）：逐币 build_signals_by_scan_replay 造假想入场（与 live
候选同管线、signal_types 对齐 allowed_triggers、账本口径 TP2=3R），唯一键
去重保证与 live/重复回填可重入。

任何失败只告警——账本绝不拖累扫描与交易主链路。
"""
import logging
import time
from datetime import datetime, timedelta

from app.celery_app import celery_app
from app.config import settings
from app.services.exchange_pool import ExchangePool
from app.services.ledger import (
    ADVANCE_BATCH_LIMIT,
    LEDGER_MAX_AGE_DAYS,
    LEDGER_TP2_RR,
    insert_signal,
)
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
_EPOCH = datetime(1970, 1, 1)
_TIME_BUDGET_S = 900       # 推进任务单轮时间预算（soft_time_limit=1500 留余量）
_SWEEP_LIMIT = 500         # AI 标注 sweep 单轮上限


def _ms(dt: datetime) -> int:
    """naive UTC datetime → epoch ms（纪元差值法，不依赖进程时区）。"""
    return int((dt - _EPOCH).total_seconds() * 1000)


@celery_app.task(name="app.tasks.ledger_tasks.run_ledger_advance_task",
                 soft_time_limit=1500, max_retries=0)
def run_ledger_advance_task() -> dict:
    """推进虚拟账本：AI 标注 sweep + OPEN 行 replay_exit 回放到终局/过期。"""
    from app.database import SessionLocal
    from app.models.ledger import VirtualLedger
    from app.services.backtest_engine import replay_exit

    task_id = open_task("ledger_advance", "虚拟账本推进")
    db = SessionLocal()
    pool = ExchangePool()
    decided = expired = 0
    try:
        # ① AI 标注 sweep（自愈：挂点失败/历史漏挂的行补挂，幂等）
        _sweep_ai_labels(db)

        # ② 最老的 OPEN 行优先（积压有界收敛）
        rows = (
            db.query(VirtualLedger)
            .filter(VirtualLedger.status == "OPEN")
            .order_by(VirtualLedger.entry_bar_open.asc())
            .limit(ADVANCE_BATCH_LIMIT)
            .all()
        )
        if not rows:
            close_task(task_id, "completed", summary="无 OPEN 行")
            return {"ok": True, "decided": 0, "expired": 0}

        by_symbol: dict[str, list] = {}
        for r in rows:
            by_symbol.setdefault(r.symbol, []).append(r)

        now = datetime.utcnow()
        now_ms = int(now.timestamp() * 1000)
        t0 = time.monotonic()
        processed_syms = 0
        for sym, sym_rows in by_symbol.items():
            if time.monotonic() - t0 > _TIME_BUDGET_S:
                logger.warning("账本推进时间预算耗尽，已推 %d/%d 币，剩余下轮续推",
                               processed_syms, len(by_symbol))
                break
            try:
                min_entry = min(r.entry_bar_open for r in sym_rows)
                start_ms = _ms(min_entry)
                kl_1h = _load(db, pool, sym, "1h", start_ms, now_ms)
                kl_d1 = _load(db, pool, sym, "1d",
                              start_ms - 45 * DAY_MS, now_ms)
            except Exception as e:
                logger.warning("账本推进 %s K线拉取失败（本币跳过）: %s", sym, e)
                continue

            for r in sym_rows:
                entry_ms = _ms(r.entry_bar_open) + HOUR_MS
                # 只取已收盘根（未收盘根的高低价未知，喂进回放会前视）
                bars = [k for k in kl_1h
                        if entry_ms <= int(k[0]) and int(k[0]) + HOUR_MS <= now_ms]
                if not bars:
                    continue  # 无已收盘数据（拉取失败/刚开单）下轮再推
                res = replay_exit(r.direction, r.entry_price, r.stop_price,
                                  r.tp1_price, r.tp2_price, bars, None, kl_d1)
                if res["outcome"] != "open":
                    r.status = "DONE"
                    r.outcome = res["outcome"]
                    r.r = res["r"]
                    r.bars = res["bars"]
                    r.decided_at = datetime.utcnow()
                    r.result_json = res
                    decided += 1
                elif (now - r.entry_bar_open).days > LEDGER_MAX_AGE_DAYS:
                    # 有数据佐证仍 open（窗口走满未终局，如窄幅横盘未触任何位）
                    r.status = "EXPIRED"
                    r.bars = res["bars"]
                    r.decided_at = datetime.utcnow()
                    expired += 1
            db.commit()
            processed_syms += 1
        close_task(task_id, "completed",
                   summary=f"推进 {len(rows)} 行：终局 {decided}、过期 {expired}、"
                           f"未决 {len(rows) - decided - expired}")
        return {"ok": True, "decided": decided, "expired": expired}
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=f"{type(e).__name__}: {e}"[:2000])
        logger.exception("虚拟账本推进失败")
        raise
    finally:
        db.close()


def _sweep_ai_labels(db) -> int:
    """AI 标注补挂：ai_decision 为空但已有 AIAnalysis 结论的账本行回填。"""
    from sqlalchemy import select

    from app.models.ledger import VirtualLedger
    from app.models.scan import AIAnalysis

    rows = db.execute(
        select(VirtualLedger, AIAnalysis)
        .join(AIAnalysis, AIAnalysis.scan_result_id == VirtualLedger.scan_result_id)
        .where(VirtualLedger.scan_result_id.isnot(None),
               VirtualLedger.ai_decision.is_(None))
        .limit(_SWEEP_LIMIT)
    ).all()
    n = 0
    for ledger_row, ai in rows:
        ledger_row.ai_decision = ai.trade_decision
        ledger_row.ai_recommendation = (
            float(ai.recommendation) if ai.recommendation is not None else None)
        n += 1
    if n:
        db.commit()
    return n


def _load(db, pool: ExchangePool, symbol: str, interval: str,
          start_ms: int, end_ms: int) -> list:
    """backtest_tasks._load_klines 转发（缓存优先+分页限速，避免循环依赖延迟导入）。"""
    from app.tasks.backtest_tasks import _load_klines

    return _load_klines(db, pool, symbol, interval, start_ms, end_ms)


@celery_app.task(name="app.tasks.ledger_tasks.run_ledger_backfill_task",
                 soft_time_limit=3600, max_retries=0)
def run_ledger_backfill_task(days: int = 30, symbols: list | None = None) -> dict:
    """源B 扫描重放回填：逐币重放历史扫描管线造假想入场插入账本。

    与 live 的边界去重靠 uq_ledger_candle 唯一键（冲突跳过保留先到行），
    可重入；signal_types 对齐 allowed_triggers、TP2=3R 账本口径。
    """
    from app.database import SessionLocal
    from app.models.system_config import SystemConfig
    from app.services.backtest_engine import build_signals_by_scan_replay
    from app.services.scan_filters import resolve_scan_filters

    days = max(1, min(int(days), int(getattr(settings, "BACKTEST_MAX_DAYS", 90))))
    task_id = open_task("ledger_backfill", f"虚拟账本回填 {days}天")
    db = SessionLocal()
    pool = ExchangePool()
    try:
        if not symbols:
            sd = pool.get_usdt_swap_symbols_with_volume() or []
            symbols = [s["symbol"] for s in sd]
        limit = int(getattr(settings, "BACKTEST_MAX_SYMBOLS", 50))
        symbols = list(symbols)[:limit]
        flt = resolve_scan_filters(db.get(SystemConfig, 1))

        window = 240  # 与源B 默认预热一致
        now_ms = _ms(datetime.utcnow())
        start_1h = now_ms - (days * 24 + window) * HOUR_MS
        inserted = skipped = 0
        for sym in symbols:
            try:
                kl = _load(db, pool, sym, "1h", start_1h, now_ms)
                htf = _load(db, pool, sym, "4h",
                            now_ms - 200 * 4 * HOUR_MS, now_ms) or []
                if len(kl) < window + 5:
                    logger.warning("账本回填 %s 1h 仅 %d 根（预热不足）跳过",
                                   sym, len(kl))
                    continue
                sigs = build_signals_by_scan_replay(
                    sym, kl, htf, flt,
                    signal_types=tuple(flt.allowed_triggers),
                    tp2_rr=LEDGER_TP2_RR,
                )
                n = sum(1 for s in sigs if insert_signal(db, s))
                db.commit()
                inserted += n
                skipped += len(sigs) - n
            except Exception as e:
                db.rollback()
                logger.warning("账本回填 %s 失败（跳过）: %s", sym, e)
                continue
            time.sleep(0.5)  # 币间限速
        close_task(task_id, "completed",
                   summary=f"回填 {days} 天 {len(symbols)} 币：插入 {inserted}、"
                           f"去重跳过 {skipped}")
        return {"ok": True, "inserted": inserted, "skipped": skipped}
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=f"{type(e).__name__}: {e}"[:2000])
        logger.exception("虚拟账本回填失败")
        raise
    finally:
        db.close()
