"""交易记录 API（docs/06）：列表 + 操作历史 + 复盘K线；资金分析聚合接口见 docs/14"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.system_config import SystemConfig
from app.models.trade import AccountTransfer, TradeEvent, TradeRecord
from app.schemas.trade import (
    AccountTransferOut,
    DailyPoint,
    ExitReasonStat,
    FundAnalyticsOut,
    FundSummary,
    PnlBucket,
    SymbolPnl,
    TradeEventOut,
    TradeOut,
    TransferPoint,
)
from app.services.binance_client import _INTERVAL_MINUTES
from app.services.exchange_pool import ExchangePool, AllExchangesFailed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("/account-summary")
def account_summary(db: Session = Depends(get_db)):
    """当前交易环境合约账户资金概览（顶部状态栏展示，docs/06 账户维度补充）：
    总资金=钱包+未实现盈亏；总收益=已实现+未实现；收益率=总收益÷本金，
    本金=总资金−总收益（自洽口径，不依赖划转记录）。请求失败返回 available=False
    前端降级显示「--」，不阻塞页面"""
    from app.services.binance_trader import BinanceTrader

    cfg = db.get(SystemConfig, 1)
    testnet = bool(cfg.trading_testnet) if cfg else True
    out: dict = {"testnet": testnet, "available": False}
    try:
        trader = BinanceTrader(testnet)
        wallet = trader.wallet_balance()          # 1 请求：wallet + available
        positions = trader.positions()            # 1 请求：未实现盈亏
        unrealized = 0.0
        for p in positions.values():
            up = float(p.get("unrealized_pnl") or 0)
            unrealized += up
        margin_balance = wallet["wallet"] + unrealized
        realized = db.execute(
            select(func.coalesce(func.sum(TradeRecord.realized_pnl), 0.0)).where(
                TradeRecord.status == "CLOSED",
                TradeRecord.testnet == testnet,
            )
        ).scalar_one()
        profit = float(realized) + unrealized
        principal = margin_balance - profit
        out.update(
            available=True,
            wallet=round(wallet["wallet"], 2),
            available_balance=round(wallet["available"], 2),
            unrealized=round(unrealized, 2),
            margin_balance=round(margin_balance, 2),
            realized=round(float(realized), 2),
            profit=round(profit, 2),
            profit_rate=round(profit / principal * 100, 2) if principal > 0 else None,
        )
    except Exception as e:
        logger.warning("账户资金概览获取失败（testnet=%s）：%s", testnet, e)
    return out


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


@router.get("/transfers", response_model=list[AccountTransferOut])
def list_transfers(
    scope: str = "live",
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """合约账户资金划转记录（新→旧，amount 正=转入/负=转出）。
    由每小时结算巡检从币安 income TRANSFER 同步，本接口只读；
    scope 区分环境（live/testnet），按 testnet 标记过滤。"""
    if scope not in ("live", "testnet"):
        raise HTTPException(status_code=400, detail="scope 只支持 live/testnet")
    rows = db.execute(
        select(AccountTransfer)
        .where(AccountTransfer.testnet == (scope == "testnet"))
        .order_by(AccountTransfer.transfer_time.desc())
        .limit(min(limit, 500))
    ).scalars().all()
    return [AccountTransferOut.model_validate(r) for r in rows]


def _bj_day(dt: datetime) -> str:
    """UTC naive → 北京时间自然日键（DB 存 UTC，北京 = UTC+8）"""
    return (dt + timedelta(hours=8)).date().isoformat()


_PNL_EDGES = (5, 10, 20, 50, 100, 200, 500, 1000)  # |pnl| 档位切分边界


def _pnl_bucket(v: float) -> str:
    """实盈亏 → 固定档位名：负/正各 8 档 + 溢出档（边界值归离 0 更远的一档）"""
    mag = abs(v)
    for i, edge in enumerate(_PNL_EDGES):
        if mag < edge:
            lo = _PNL_EDGES[i - 1] if i else 0
            if v > 0:
                return f"{lo}~{edge}"
            return f"0~-{edge}" if lo == 0 else f"-{edge}~-{lo}"
    return ">1000" if v > 0 else ">|1000|"


def _pnl_bucket_names() -> list[str]:
    """全部档位名（深亏 → 深盈），供盈亏分布固定 x 轴顺序"""
    neg = []
    for i in range(len(_PNL_EDGES) - 1, -1, -1):
        edge = _PNL_EDGES[i]
        lo = _PNL_EDGES[i - 1] if i else 0
        neg.append(f"0~-{edge}" if lo == 0 else f"-{edge}~-{lo}")
    pos = []
    for i, e in enumerate(_PNL_EDGES):
        lo = _PNL_EDGES[i - 1] if i else 0
        pos.append(f"{lo}~{e}")
    pos.append(">1000")
    return [">|1000|"] + neg + pos


@router.get("/analytics", response_model=FundAnalyticsOut)
def fund_analytics(
    scope: str = "live",
    start: Optional[str] = None,   # YYYY-MM-DD（北京时间）
    end: Optional[str] = None,     # 闭区间，含当日
    db: Session = Depends(get_db),
):
    """资金分析聚合（docs/14）：闭单按 closed_at 落北京时间自然日桶，划转按 transfer_time

    - start/end 为北京时间 YYYY-MM-DD 闭区间；DB 存 UTC naive，过滤用换算后的 UTC 边界
    - scope=testnet 时闭单照常统计，划转按 testnet 标记过滤（两环境各自同步落库）
    """
    def _parse_day(s: str, name: str) -> datetime:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{name} 参数格式应为 YYYY-MM-DD")

    testnet = scope == "testnet"
    # 北京日边界 → UTC：北京 00:00 = 前一日 16:00 UTC
    lo = _parse_day(start, "start") - timedelta(hours=8) if start else None
    hi = _parse_day(end, "end") + timedelta(days=1) - timedelta(hours=8) if end else None

    base = select(TradeRecord).where(
        TradeRecord.status == "CLOSED",
        TradeRecord.testnet == testnet,
        TradeRecord.closed_at.is_not(None),
    )
    if lo is not None:
        base = base.where(TradeRecord.closed_at >= lo)
    if hi is not None:
        base = base.where(TradeRecord.closed_at < hi)
    rows = db.execute(base).scalars().all()

    closed = len(rows)
    wins = sum(1 for r in rows if (r.realized_pnl or 0) > 0)
    net_pnl = round(sum(float(r.realized_pnl or 0) for r in rows), 2)
    fee_total = round(sum(float(r.fee_total or 0) for r in rows), 2)
    funding_total = round(sum(float(r.funding_total or 0) for r in rows), 2)
    win_rs = [
        float(r.realized_pnl) / float(r.risk_amount)
        for r in rows
        if (r.realized_pnl or 0) > 0 and r.risk_amount and float(r.risk_amount) > 0
    ]
    avg_win_r = round(sum(win_rs) / len(win_rs), 2) if win_rs else None

    daily: dict[str, float] = {}
    for r in rows:
        d = _bj_day(r.closed_at)
        daily[d] = daily.get(d, 0.0) + float(r.realized_pnl or 0)
    days = sorted(daily)
    equity_curve: list[DailyPoint] = []
    cum = 0.0
    for d in days:
        cum += daily[d]
        equity_curve.append(DailyPoint(date=d, pnl=round(cum, 2)))

    by_symbol_map: dict[str, dict] = {}
    for r in rows:
        m = by_symbol_map.setdefault(r.symbol, {"pnl": 0.0, "count": 0, "wins": 0})
        m["pnl"] += float(r.realized_pnl or 0)
        m["count"] += 1
        if (r.realized_pnl or 0) > 0:
            m["wins"] += 1

    exit_map: dict[str, dict] = {}
    for r in rows:
        m = exit_map.setdefault(r.exit_reason or "unknown", {"count": 0, "pnl": 0.0})
        m["count"] += 1
        m["pnl"] += float(r.realized_pnl or 0)

    buckets: dict[str, int] = {}
    for r in rows:
        name = _pnl_bucket(float(r.realized_pnl or 0))
        buckets[name] = buckets.get(name, 0) + 1

    # 划转（amount 带符号：正=转入），只统计 USDT；按环境标记过滤
    # （testnet 列 2026-09-20 补上，存量回填 TRUE = 测试网账户流水）
    tq = select(AccountTransfer).where(
        AccountTransfer.asset == "USDT",
        AccountTransfer.testnet == testnet,
    )
    if lo is not None:
        tq = tq.where(AccountTransfer.transfer_time >= lo)
    if hi is not None:
        tq = tq.where(AccountTransfer.transfer_time < hi)
    rows_t = db.execute(tq).scalars().all()
    t_daily: dict[str, dict] = {}
    for t in rows_t:
        m = t_daily.setdefault(_bj_day(t.transfer_time), {"in": 0.0, "out": 0.0})
        amt = float(t.amount)
        if amt >= 0:
            m["in"] += amt
        else:
            m["out"] += -amt
    t_in = round(sum(m["in"] for m in t_daily.values()), 2)
    t_out = round(sum(m["out"] for m in t_daily.values()), 2)

    return FundAnalyticsOut(
        summary=FundSummary(
            closed=closed,
            wins=wins,
            win_rate=round(wins / closed * 100, 1) if closed else None,
            net_pnl=net_pnl,
            fee_total=fee_total,
            funding_total=funding_total,
            avg_win_r=avg_win_r,
            transfer_in=t_in,
            transfer_out=t_out,
            transfer_net=round(t_in - t_out, 2),
        ),
        equity_curve=equity_curve,
        daily_pnl=[DailyPoint(date=d, pnl=round(daily[d], 2)) for d in days],
        by_symbol=sorted(
            (
                SymbolPnl(symbol=s, pnl=round(m["pnl"], 2), count=m["count"], wins=m["wins"])
                for s, m in by_symbol_map.items()
            ),
            key=lambda x: abs(x.pnl),
            reverse=True,
        ),
        exit_reasons=sorted(
            (
                ExitReasonStat(reason=k, count=m["count"], pnl=round(m["pnl"], 2))
                for k, m in exit_map.items()
            ),
            key=lambda x: x.count,
            reverse=True,
        ),
        pnl_distribution=[
            PnlBucket(bucket=b, count=buckets[b]) for b in _pnl_bucket_names() if b in buckets
        ],
        transfers=[
            TransferPoint(date=d, transfer_in=round(m["in"], 2), transfer_out=round(m["out"], 2))
            for d, m in sorted(t_daily.items())
        ],
    )


def _get_trade_events(trade_id: str, db: Session) -> list[TradeEvent]:
    """按记录 ID 升序取操作历史（不存在抛 404）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()


@router.post("/purge-testnet")
def purge_testnet(confirm: bool = False, db: Session = Depends(get_db)):
    """一键清空测试网交易数据（两阶段：confirm=false 预检返回将删数量，
    confirm=true 执行删除；范围=测试网交易记录+操作历史+划转流水，
    不含复盘统计）。执行写入系统日志（任务记录）供审计"""
    from app.services.trade_engine import purge_testnet_data

    try:
        return purge_testnet_data(db, force=confirm)
    except Exception as e:
        logger.error("清空测试网数据失败: %s", e)
        raise HTTPException(status_code=500, detail=f"清空失败: {e}")


@router.get("/{trade_id}/events", response_model=list[TradeEventOut])
def trade_events(trade_id: str, db: Session = Depends(get_db)):
    """一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）"""
    rows = _get_trade_events(trade_id, db)
    return [TradeEventOut.model_validate(e) for e in rows]


def _load_running_trade(trade_id: str, db: Session) -> TradeRecord:
    """手动诊断前置校验：记录存在且在跑状态（docs/16）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
        raise HTTPException(status_code=400, detail="仅运行中的单子支持 AI 持仓诊断")
    return rec


@router.post("/{trade_id}/advice")
def run_advice(trade_id: str, db: Session = Depends(get_db)):
    """手动 AI 持仓诊断（docs/16）：异步派发诊断子任务，立即返回。

    同步等 LLM 会被代理层/前端超时切断（GLM 思考模型常超 30s），改为与定时
    诊断同链路（run_position_advice，锁外 celery 执行）；任务不做冷却判断，
    手动派发天然无视冷却。前端提交后轮询列表对比 ai_advice_at 获取结果。
    """
    from app.config import settings
    from app.tasks.trade_tasks import run_position_advice

    rec = _load_running_trade(trade_id, db)
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="AI 未配置（AI_API_KEY 为空）")
    run_position_advice.delay(str(rec.id))
    return {"queued": True, "symbol": rec.symbol}


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
        "current_stop_loss": _nz(rec.stop_loss),  # 跟踪止损现值（SL_MOVE 后更新）；与初始快照不同时前端加画现止损线
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
