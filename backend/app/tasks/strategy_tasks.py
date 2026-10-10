"""策略插件信号任务（docs/20 S1）：影子期产信号落库，绝不触发开单。

调度：run_scan_task 收尾后 fire-and-forget 派发（golden_k 全链路无感知）；
本任务任何异常只记日志，不影响扫描主流程（影子纪律，同 docs/14 §九）。
开单权限 S2 才解锁（经 docs/20 §4.2 重叠率/相关性评审），本文件刻意
不 import 任何执行层模块。

三件事：
- run_strategy_signals_task：插件策略产影子信号（docs/20 S1）；
- run_strategy_observations_task：L1 观测快照（docs/19 §3.1，纯算不拦）；
- evaluate_shadow_signals_task：影子信号推演回填（replay_exit 假想结果，
  口径同 docs/14 未开单候选回放；open 不进 decided 分母）。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.scan import ScanResult
from app.models.strategy import StrategyObservation, StrategyShadowSignal
from app.models.system_config import SystemConfig
from app.services.backtest_engine import replay_exit
from app.services.exchange_pool import ExchangePool
from app.services.strategies import enabled_plugin_entries
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)

KLINE_LIMIT = 300  # 4h EMA144 斜率/缠绕需 ≥165 根已收盘，1h 结构需 ≥165，300 留余量
DEDUP_HOURS = 24   # 同策略同标的同方向信号去重窗口
OBS_MIN_HOURS = 3  # 同标的观测快照最小间隔（4h 节奏）
OBS_RETENTION_DAYS = 30
EVAL_MIN_AGE_HOURS = 4   # 信号至少 4h 龄才推演（留出走势空间）
EVAL_FWD_BARS = 120      # 推演前向窗口（1h 根数）

# 策略出场语义 → replay_exit overrides（docs/19 §3.4 / docs/20 §六）。
# golden_k_ai 缺省 = 引擎基线（TP 分批+跟进）；ema_trend = 纯跟踪 V1 +
# 1R 武装保本 + ATR×2 跟进 + 破 EMA55 信号出场（1h 根、无 D1 → ATR 腿
# 退化摆动腿，与生产数据不可得时同口径）
EVAL_EXIT_OVERRIDES: dict[str, dict] = {
    "ema_trend": {
        "tp_qty_override": (0.0, 0.0),
        "be_trigger_r": 1.0,
        "trail_atr_k": 2.0,
        "sig_exit": "ema55_break",
    },
}


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
                if not latest:  # 对齐观测任务：无扫描记录不跑全表
                    return {"skipped": "无扫描记录"}
                q = q.filter(ScanResult.scan_record_id == latest[0])
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
        summary = (f"策略信号 {'+'.join(e.display_name for e in plugins)}"
                   f"：产出 {produced} 条")
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


@celery_app.task(name="app.tasks.strategy_tasks.run_strategy_observations_task",
                 max_retries=0)
def run_strategy_observations_task(symbols: Optional[list] = None,
                                   trigger: str = "scan") -> dict:
    """L1 观测快照（docs/19 §3.1）：对 universe 逐币计算证据包落库。

    纯观测不拦截；同标的 3h 内已有快照则跳过；保留期 30 天滚动清理。
    观测不依赖 strategy_registry enabled——即使 ema_trend 停用也持续
    积累定标素材（P1 启用条件要求 ≥4 周连续样本）。
    """
    db = SessionLocal()
    task_id = ""
    saved = 0
    try:
        if not symbols:
            latest = (
                db.query(ScanResult.scan_record_id)
                .order_by(ScanResult.created_at.desc())
                .first()
            )
            if not latest:
                return {"skipped": "无扫描记录"}
            symbols = [
                r[0] for r in db.query(ScanResult.symbol)
                .filter(ScanResult.scan_record_id == latest[0])
                .distinct().all()
            ]
        if not symbols:
            return {"skipped": "universe 为空"}

        task_id = open_task(
            task_type="strategy_observations",
            task_name="策略L1观测",
            trigger=trigger,
        )
        cutoff = datetime.utcnow() - timedelta(hours=OBS_MIN_HOURS)
        recent = {
            r[0] for r in db.query(StrategyObservation.symbol)
            .filter(StrategyObservation.created_at >= cutoff).all()
        }

        from app.services.strategies.ema_trend import EmaTrendStrategy
        observer = EmaTrendStrategy({})  # 观测用出厂参数（不随 registry 变动，口径稳定）
        pool = ExchangePool()
        for sym in symbols:
            if sym in recent:
                continue
            try:
                k1 = pool.get_klines(sym, "1h", KLINE_LIMIT)
                k4 = pool.get_klines(sym, "4h", KLINE_LIMIT)
                metrics = observer.observe(klines_1h=k1, klines_4h=k4)
                db.add(StrategyObservation(symbol=sym, metrics=metrics))
                db.commit()
                saved += 1
            except Exception as e:  # 观测纪律：单币失败吞掉
                db.rollback()
                logger.warning("L1 观测 %s 失败: %s", sym, e)

        # 保留期清理（30 天前快照）
        db.query(StrategyObservation).filter(
            StrategyObservation.created_at
            < datetime.utcnow() - timedelta(days=OBS_RETENTION_DAYS)
        ).delete()
        db.commit()
        close_task(task_id, "completed", summary=f"L1 观测：新增 {saved} 条",
                   detail={"saved": saved, "universe": len(symbols)})
        return {"saved": saved, "universe": len(symbols)}
    except Exception as e:
        logger.exception("L1 观测任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.strategy_tasks.evaluate_shadow_signals_task",
                 max_retries=0)
def evaluate_shadow_signals_task(strategy_id: Optional[str] = None,
                                 trigger: str = "scan") -> dict:
    """影子信号推演回填：对未推演且已过 4h 龄的信号跑 replay_exit。

    出场参数用策略 exit_spec 映射（ema_trend：无固定止盈 → tp1/tp2 传 0、
    ATR 跟进需要 D1 K 线，缺省退化摆动腿——与生产数据不可得时同口径）。
    窗口耗尽 outcome=open（不进 decided 分母，docs/16 P-04 口径）。
    """
    db = SessionLocal()
    task_id = ""
    evaluated = 0
    try:
        q = (
            db.query(StrategyShadowSignal)
            .filter(StrategyShadowSignal.outcome.is_(None))
            .filter(StrategyShadowSignal.created_at
                    <= datetime.utcnow() - timedelta(hours=EVAL_MIN_AGE_HOURS))
        )
        if strategy_id:
            q = q.filter(StrategyShadowSignal.strategy_id == strategy_id)
        rows = q.order_by(StrategyShadowSignal.created_at.asc()).limit(200).all()
        if not rows:
            return {"evaluated": 0}

        task_id = open_task(
            task_type="shadow_evaluate",
            task_name="影子信号推演",
            trigger=trigger,
        )
        pool = ExchangePool()
        for sig in rows:
            try:
                k1 = pool.get_klines(sig.symbol, "1h", KLINE_LIMIT + EVAL_FWD_BARS)
                # 信号时间之后的已收盘 K 线（自入场后第一根起）。
                # DB 存 naive UTC（datetime.utcnow），显式按 UTC 换算——
                # naive.timestamp() 会按本地时区解释（Asia/Shanghai 偏 +8h）
                sig_ms = (sig.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000
                          if sig.created_at else 0)
                fwd = [k for k in k1[:-1] if k[0] >= sig_ms][:EVAL_FWD_BARS]
                if not fwd:
                    continue
                # 断档守卫：首根距信号 >2h 说明拉取窗口未覆盖信号时刻
                #（Celery 长期停机/币种停盘的积压信号），从中段回放会算错 R——
                # 标 invalid 落袋（不进 decided 分母），防止永久积压 outcome=None
                if fwd[0][0] > sig_ms + 2 * 3_600_000:
                    sig.outcome = "invalid"
                    sig.evaluated_at = datetime.utcnow()
                    db.commit()
                    continue
                res = replay_exit(
                    sig.direction, float(sig.entry), float(sig.stop),
                    float(sig.tp1), float(sig.tp2), fwd,
                    overrides={
                        "fwd_bars": EVAL_FWD_BARS,
                        **EVAL_EXIT_OVERRIDES.get(sig.strategy_id, {}),
                    },
                )
                sig.outcome = res["outcome"]
                sig.r = res["r"]
                sig.eval_bars = res["bars"]
                sig.evaluated_at = datetime.utcnow()
                db.commit()
                evaluated += 1
            except Exception as e:  # 推演纪律：单条失败吞掉
                db.rollback()
                logger.warning("影子推演 %s 失败: %s", sig.symbol, e)
        close_task(task_id, "completed", summary=f"影子推演：回填 {evaluated} 条",
                   detail={"evaluated": evaluated})
        return {"evaluated": evaluated}
    except Exception as e:
        logger.exception("影子推演任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
        return {"error": str(e)}
    finally:
        db.close()
