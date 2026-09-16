"""交易记录 API（docs/06）：列表 + 操作历史 + 复盘K线"""
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.system_config import SystemConfig
from app.models.trade import TradeEvent, TradeRecord
from app.schemas.trade import TradeEventOut, TradeOut
from app.services.binance_client import _INTERVAL_MINUTES
from app.services.exchange_pool import ExchangePool, AllExchangesFailed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("", response_model=list[TradeOut])
def list_trades(
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """自动交易记录（新→旧）。status 可选过滤：OPENED/TP1_HIT/TP2_HIT/CLOSED/FAILED"""
    q = select(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(TradeRecord.status == status.upper())
    rows = db.execute(q).scalars().all()
    return [TradeOut.model_validate(r) for r in rows]


@router.get("/{trade_id}/events", response_model=list[TradeEventOut])
def trade_events(trade_id: str, db: Session = Depends(get_db)):
    """一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    rows = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()
    return [TradeEventOut.model_validate(e) for e in rows]


@router.get("/{trade_id}/klines")
def trade_klines(trade_id: str, db: Session = Depends(get_db)):
    """交易复盘K线：开单前 100 根 + 持仓期间直到止损/平仓，附初始止损止盈与关键事件

    - 初始 SL/TP 取自 OPEN 事件 detail（记录上的 stop_loss 是现值，跟踪止损后已被更新）
    - 总根数 = 100 + 持仓根数，上限 500（超长持仓自动压缩开仓前根数，持仓段始终完整）
    - 历史区间直接请求交易所（多所故障转移），不读不写缓存
    """
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    if rec.status == "FAILED" or rec.opened_at is None:
        raise HTTPException(status_code=400, detail="该记录未实际开仓，无复盘K线")

    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    interval_ms = _INTERVAL_MINUTES.get(interval, 60) * 60_000

    # DB 存 UTC naive 时间戳，显式按 UTC 换算（naive.timestamp() 会按本地时区解释）
    opened_dt: datetime = rec.opened_at
    end_dt: datetime = rec.closed_at or datetime.utcnow()
    held_bars = math.ceil(max(0, (end_dt - opened_dt).total_seconds() * 1000) / interval_ms)
    bars = min(500, 100 + held_bars)
    # 减 1ms：closed_at 恰在整点边界时不含平仓后的新K线；平仓在K线中间时仍含所在K线
    end_ms = int(end_dt.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1

    pool = ExchangePool()
    try:
        klines = pool.get_klines_range(rec.symbol, interval, bars, end_ms)
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线获取失败（{e.summary}）")

    evts = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()

    # 初始风控价：OPEN 事件 detail 优先，老记录（无 OPEN 事件）回退记录现值；
    # 0/None 归一为 null（未设 TP2 的单子 tp2=0，传 0 到前端会画出异常区域）
    open_detail = next((e.detail or {} for e in evts if e.event_type == "OPEN"), {})

    def _nz(v) -> Optional[float]:
        if v is None:
            return None
        f = float(v)
        return f if f != 0 else None

    entry_price = _nz(open_detail.get("price") or rec.entry_price)
    stop_loss = _nz(open_detail.get("stop_loss") or rec.stop_loss)
    tp1 = _nz(open_detail.get("tp1") or rec.tp1)
    tp2 = _nz(open_detail.get("tp2") or rec.tp2)

    return {
        "trade_id": str(rec.id),
        "symbol": rec.symbol,
        "direction": rec.direction,
        "interval": interval,
        "status": rec.status,
        "exit_reason": rec.exit_reason,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "opened_at": opened_dt.isoformat() if opened_dt else None,
        "closed_at": rec.closed_at.isoformat() if rec.closed_at else None,
        "klines": [
            {
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines
        ],
        # 图上标箭头的关键事件（SL_MOVE 等过程事件不标）
        "events": [
            {
                "event_type": e.event_type,
                "created_at": e.created_at.isoformat(),
                "detail": e.detail,
            }
            for e in evts
            if e.event_type in ("OPEN", "TP1_FILL", "TP2_FILL", "SL_FILL")
        ],
    }
