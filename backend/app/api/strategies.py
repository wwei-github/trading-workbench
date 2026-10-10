"""策略插件 API（docs/20 S1）：注册表只读 + 影子信号查询 + 手动触发。

S1 范围：只读与影子数据，不提供开单类端点——S2 开单权限解锁前，
执行层对本模块不可达。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.strategy import StrategyShadowSignal
from app.models.system_config import SystemConfig
from app.services.strategies import KNOWN_STRATEGIES, load_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class StrategyBrief(BaseModel):
    id: str
    display_name: str
    enabled: bool
    max_positions: int
    risk_weight: float
    config: dict
    kind: str  # native / plugin（golden_k 原生管线，其余为插件协议实现）


@router.get("", response_model=list[StrategyBrief])
def list_strategies(db: Session = Depends(get_db)):
    """策略清单（注册表条目 + 展示名）。registry NULL = 仅内置 golden_k_ai。
    含停用条目——禁用的策略也要可见，否则配置了但被禁用会被误认为配置丢失。"""
    cfg = db.get(SystemConfig, 1)
    raw = cfg.strategy_registry if cfg else None
    entries = load_registry(raw)
    display = {"golden_k_ai": "金K + AI（内置）", "ema_trend": "EMA 趋势跟踪"}
    out = []
    for e in entries:
        out.append(StrategyBrief(
            id=e.id, display_name=display.get(e.id, e.id), enabled=e.enabled,
            max_positions=e.max_positions, risk_weight=e.risk_weight,
            config=e.config, kind="native" if e.id == "golden_k_ai" else "plugin",
        ))
    return out


@router.get("/signals")
def list_signals(
    strategy_id: str | None = Query(None, description="按策略过滤"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """影子信号列表（新→旧）。仅影子数据，与实盘单无关联。"""
    q = db.query(StrategyShadowSignal)
    if strategy_id:
        if strategy_id not in KNOWN_STRATEGIES:
            raise HTTPException(status_code=400, detail=f"未知策略: {strategy_id}")
        q = q.filter(StrategyShadowSignal.strategy_id == strategy_id)
    rows = q.order_by(StrategyShadowSignal.created_at.desc()).limit(limit).all()
    return [
        {
            "id": str(r.id), "strategy_id": r.strategy_id, "symbol": r.symbol,
            "direction": r.direction, "entry": float(r.entry), "stop": float(r.stop),
            "tp1": float(r.tp1), "tp2": float(r.tp2), "signal_type": r.signal_type,
            "reason": r.reason, "confidence": float(r.confidence),
            "raw": r.raw, "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


class SignalRefreshBody(BaseModel):
    symbols: list[str] | None = None  # 缺省 = 最近一轮扫描标的集


@router.post("/signals/refresh")
def refresh_signals(body: SignalRefreshBody):
    """手动触发影子信号任务（Celery 异步，fire-and-forget）。"""
    from app.tasks.strategy_tasks import run_strategy_signals_task
    run_strategy_signals_task.delay(symbols=body.symbols)
    return {"accepted": True}


@router.post("/observations/refresh")
def refresh_observations():
    """手动触发 L1 观测任务（docs/19 §3.1，纯算不拦）。"""
    from app.tasks.strategy_tasks import run_strategy_observations_task
    run_strategy_observations_task.delay(trigger="manual")
    return {"accepted": True}


@router.post("/signals/evaluate")
def evaluate_signals():
    """手动触发影子信号推演回填（replay_exit 假想结果）。"""
    from app.tasks.strategy_tasks import evaluate_shadow_signals_task
    evaluate_shadow_signals_task.delay(trigger="manual")
    return {"accepted": True}


@router.get("/observations")
def list_observations(
    symbol: str | None = Query(None, description="按币种过滤"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """L1 观测快照列表（新→旧）。metrics 含评分卡 8 项明细。"""
    from app.models.strategy import StrategyObservation
    q = db.query(StrategyObservation)
    if symbol:
        q = q.filter(StrategyObservation.symbol == symbol)
    rows = q.order_by(StrategyObservation.created_at.desc()).limit(limit).all()
    return [
        {
            "id": str(r.id), "symbol": r.symbol, "metrics": r.metrics,
            "metrics_version": r.metrics_version,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/analysis")
def analysis(db: Session = Depends(get_db)):
    """影子期分析（docs/20 §4.2 并发准入判定的数据源）：

    - 每策略信号统计：总量/已推演/decided/胜率/期望 R（open 不进分母）；
    - 插件策略两两信号重叠率：同标的同方向、信号时间窗 ±24h 内双发的频率
      （相对较少一方的信号数）。重叠率 >50% → 伪分散，只跑其一。
    """
    from datetime import datetime, timedelta

    from app.models.strategy import StrategyShadowSignal

    rows = db.query(StrategyShadowSignal).all()
    by_strategy: dict[str, list] = {}
    for r in rows:
        by_strategy.setdefault(r.strategy_id, []).append(r)

    DECIDED_EXCLUDE = {"open", "invalid"}
    stats = []
    for sid, rs in sorted(by_strategy.items()):
        evaluated = [r for r in rs if r.outcome is not None]
        decided = [r for r in evaluated if r.outcome not in DECIDED_EXCLUDE]
        wins = [r for r in decided if (r.outcome or "").startswith("win")]
        r_vals = [float(r.r) for r in decided if r.r is not None]
        stats.append({
            "strategy_id": sid,
            "signals": len(rs),
            "evaluated": len(evaluated),
            "decided": len(decided),
            "open": len(evaluated) - len(decided),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(decided), 4) if decided else None,
            "avg_r": round(sum(r_vals) / len(r_vals), 4) if r_vals else None,
            "total_r": round(sum(r_vals), 4) if r_vals else None,
        })

    # 两两重叠率（插件策略之间；golden_k 信号不落影子表，不参与）。
    # 口径：较少一方中有多大比例与另一方 ±24h 内同标的同方向双发——
    # 遍历较少方计数（每条最多命中 1 次），保证 overlap_rate ≤ 1
    overlaps = []
    sids = [s for s in by_strategy if s != "golden_k_ai"]
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            a_rows, b_rows = by_strategy[sids[i]], by_strategy[sids[j]]
            if not a_rows or not b_rows:
                continue
            if len(a_rows) > len(b_rows):
                a_rows, b_rows = b_rows, a_rows  # 保证遍历较少方
            hits = 0
            for a in a_rows:
                for b in b_rows:
                    if a.symbol != b.symbol or a.direction != b.direction:
                        continue
                    if not (a.created_at and b.created_at):
                        continue
                    if abs((a.created_at - b.created_at).total_seconds()) <= 86400:
                        hits += 1
                        break
            overlaps.append({
                "pair": [sids[i], sids[j]],
                "overlap_rate": round(hits / len(a_rows), 4),
                "hits": hits,
            })

    return {"stats": stats, "overlaps": overlaps}
