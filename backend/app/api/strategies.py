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
