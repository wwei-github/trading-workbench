"""关注列表 API：币种的增删查、K 线手动刷新，持久保存"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.scan import ScanRecord, ScanResult, Watchlist
from app.models.system_config import SystemConfig
from app.schemas.scan import (
    ScanResultOut,
    WatchlistAddRequest,
    WatchlistItemOut,
    WatchlistListResponse,
)
from app.services.binance_client import BinanceClient
from app.services.exchange_pool import ExchangePool, AllExchangesFailed
from app.services import market_data as md
from app.services.scanner import Scanner, _get_scan_config, detect_indicator_candidate
from app.services.scan_filters import resolve_scan_filters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

# 默认关注（首次启动初始化）：关注页只保留图表，直接切换这几个主流币即可看图。
# 顺序即页面展示顺序（列表按 created_at 倒序，故种子时间按倒序写入）。
DEFAULT_WATCHLIST_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "OKBUSDT", "SOLUSDT")


def seed_default_watchlist(db: Session) -> int:
    """幂等补入默认关注币种：只补缺失项，不删用户已有币种。

    返回新增数量；已有同名币种（用户手动关注过）保持原样与原有排序。
    """
    existing = set(db.execute(select(Watchlist.symbol)).scalars().all())
    now = datetime.utcnow()
    added: list[str] = []
    for i, symbol in enumerate(DEFAULT_WATCHLIST_SYMBOLS):
        if symbol in existing:
            continue
        # 微秒级递减：BTC 最新 → SOL 最旧，与列表倒序一致
        ts = now - timedelta(microseconds=i)
        db.add(Watchlist(symbol=symbol, created_at=ts, updated_at=ts))
        added.append(symbol)
    if added:
        db.commit()
        logger.info("默认关注已补入 %d 个币种: %s", len(added), ", ".join(added))
    return len(added)


def normalize_symbol(raw: str) -> str:
    """规范币种输入：去空格转大写，未带 USDT 后缀时自动补全"""
    s = (raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    if not s:
        raise HTTPException(status_code=400, detail="币种名称不能为空")
    if not s.endswith(("USDT", "USDC", "BUSD", "USD")):
        s += "USDT"
    return s


@router.get("", response_model=WatchlistListResponse)
def list_watchlist(db: Session = Depends(get_db)):
    """关注列表（按添加时间倒序），附带每个币种最近一次扫描命中结果"""
    items = db.execute(
        select(Watchlist).order_by(Watchlist.created_at.desc())
    ).scalars().all()

    # 每个币种最新一条扫描结果：信号类型/位置/K线形态/EMA 等列的数据来源
    latest_map: dict[str, ScanResult] = {}
    if items:
        symbols = [i.symbol for i in items]
        latest = db.execute(
            select(ScanResult)
            .where(ScanResult.symbol.in_(symbols))
            .distinct(ScanResult.symbol)
            .order_by(ScanResult.symbol, ScanResult.created_at.desc())
        ).scalars().all()
        latest_map = {r.symbol: r for r in latest}

    for i in items:
        # 非映射属性，仅供 pydantic from_attributes 读取
        i.latest_scan = latest_map.get(i.symbol)

    return WatchlistListResponse(
        items=[WatchlistItemOut.model_validate(i) for i in items],
        total=len(items),
    )


@router.post("", response_model=WatchlistItemOut)
def add_watchlist(body: WatchlistAddRequest, db: Session = Depends(get_db)):
    """添加关注（幂等：已存在时直接返回）"""
    symbol = normalize_symbol(body.symbol)

    # 校验币种在任一交易所合约市场存在（拉 2 根 K 线即可，多链路故障转移）
    pool = ExchangePool()
    try:
        pool.get_klines(symbol, "1h", 2)
    except AllExchangesFailed as e:
        raise HTTPException(
            status_code=400,
            detail=f"币种 {symbol} 不存在或不可交易（{e.summary}）",
        )

    existing = db.execute(
        select(Watchlist).where(Watchlist.symbol == symbol)
    ).scalars().first()
    if existing:
        return WatchlistItemOut.model_validate(existing)

    item = Watchlist(symbol=symbol, note=body.note)
    db.add(item)
    db.commit()
    db.refresh(item)
    return WatchlistItemOut.model_validate(item)


@router.get("/quotes")
def get_quotes(db: Session = Depends(get_db)):
    """关注币种的实时行情（当前价 + 24h 成交额），供关注列表表格展示"""
    symbols = [
        row[0]
        for row in db.execute(select(Watchlist.symbol)).all()
    ]
    if not symbols:
        return {"items": []}

    client = BinanceClient()
    try:
        tickers = {t.get("symbol"): t for t in client.get_24h_tickers()}
    finally:
        client.close()

    items = []
    for sym in symbols:
        t = tickers.get(sym)
        if not t:
            continue
        try:
            items.append(
                {
                    "symbol": sym,
                    "price": float(t.get("lastPrice", 0)),
                    "volume_24h": float(t.get("quoteVolume", 0)),
                }
            )
        except (ValueError, TypeError):
            continue
    return {"items": items}


@router.post("/{symbol}/refresh")
def refresh_watchlist(symbol: str, db: Session = Depends(get_db)):
    """手动刷新关注币种：强制拉最新 K 线写回缓存，并用同一套扫描逻辑重跑信号判定。

    命中时写入新的 ScanResult（挂在 scan_type=watchlist 的刷新记录下），
    关注列表行即展示最新信号状态；未命中时不动原有命中记录——
    是否删除旧信号由用户自行决定。
    """
    target = (symbol or "").strip().upper()
    item = db.execute(
        select(Watchlist).where(Watchlist.symbol == target)
    ).scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail=f"关注列表中不存在 {target}")

    cfg = _get_scan_config(db)
    interval = cfg.kline_interval

    # 尾窗/整点竞态基准时刻（2026-10-26，docs/11 I-1）：与定时扫描同口径，只取一次
    # 时钟——screen_klines 与 trigger 共用同一基准，避免跨整点时视图与判定错位
    _tail_now = datetime.utcnow()

    pool = ExchangePool()
    try:
        klines = pool.get_klines(target, interval, 500, force_refresh=True)
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线刷新失败（{e.summary}）")

    # 幽灵尾根视图（2026-10-11-4；2026-10-13 扩整点竞态；2026-10-26 docs/11 I-1）：
    # 本入口是最后一条不 screen 就把 klines 喂 trigger 的路径——竞态下末根实为已
    # 收盘根时，trigger 判的根会比 _signal_candle_open 早一根，落库的
    # pattern_candle_open / entry_lag_pct 与图表锚点整体错位一根。正常时原样返回
    klines = md.screen_klines(klines, interval, _tail_now)

    # 信号判定：与扫描器 scan_one 完全同一套方向票多数决+两段式触发确认逻辑（每币至多 1 个候选）
    signals: list[dict] = []
    pending_op = None
    if klines:
        try:
            pend = Scanner._prefetch_pendings([target]).get(target)
            cand, pending_op = detect_indicator_candidate(
                pool, target, klines, pending=pend,
                flt=resolve_scan_filters(cfg),
                # 4h 末根竞态兜底基准（与定时扫描同口径）；与 screen 同一时刻
                now=_tail_now,
            )
        except Exception as e:
            logger.warning("关注刷新 %s 信号判定失败（按无命中处理）: %s", target, e)
            cand, pending_op = None, None
        if pending_op and pending_op.get("op") == "confirm" and cand:
            pending_op["confirm_pattern"] = cand.get("pattern")
        if cand:
            signals = [cand]

    latest_scan = None
    if signals:
        # 24h 成交额（行展示走 /quotes 实时接口，此处供 ScanResult 落库与后续 AI 分析）
        volume_24h = 0.0
        try:
            client = BinanceClient()
            try:
                volume_24h = next(
                    (
                        float(t.get("quoteVolume", 0))
                        for t in client.get_24h_tickers()
                        if t.get("symbol") == target
                    ),
                    0.0,
                )
            finally:
                client.close()
        except Exception:
            volume_24h = 0.0

        # 刷新记录独立于常规扫描（前端历史/最新结果/状态栏均排除该类型）
        record = ScanRecord(
            scan_type="watchlist", status="completed",
            coin_count=1, hit_count=len(signals),
            started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
        )
        db.add(record)
        db.flush()

        # 重复标记与扫描一致：窗口内该币种已有命中
        since = datetime.utcnow() - timedelta(hours=cfg.repeat_window_hours)
        is_repeat = db.execute(
            select(func.count())
            .select_from(ScanResult)
            .where(ScanResult.symbol == target, ScanResult.created_at >= since)
        ).scalar_one() > 0

        new_rows = [
            ScanResult(
                scan_record_id=record.id,
                symbol=target,
                signal_type=sig.get("signal_type", "unknown"),
                current_price=sig["current_price"],
                breakout_pct=sig["breakout_pct"],
                trend_slope=sig.get("trend_slope", 0),
                r_squared=sig.get("r_squared", 0),
                pattern=sig.get("pattern"),
                # 组合确认口径（2026-10-26，docs/11）：关注列表「立即扫描」与定时扫描同源
                # （detect_indicator_candidate 已带三键），不传会把组合确认静默降级成单形态
                golden_combo=bool(sig.get("golden_combo")),
                pattern_candle_open=sig.get("pattern_candle_open"),
                entry_lag_pct=sig.get("entry_lag_pct"),
                signal_reason=sig.get("signal_reason"),
                strength=sig.get("strength"),
                ema_state=sig.get("ema_state"),
                position=sig.get("position"),
                volume_24h=volume_24h,
                volume=sig.get("volume", 0),
                volume_type=sig.get("volume_type", "平量"),
                quality_score=sig.get("quality_score"),
                votes_json=sig.get("votes"),
                is_repeat=is_repeat,
                # 信号命中时刻（2026-10-21 观测 B1）：与定时扫描同口径，
                # 取候选产生时刻而非落库时刻
                signal_created_at=sig.get("detected_at"),
            )
            for sig in signals
        ]
        db.add_all(new_rows)

    item.updated_at = datetime.utcnow()
    db.commit()

    if signals:
        db.refresh(new_rows[-1])
        latest_scan = ScanResultOut.model_validate(new_rows[-1]).model_dump(mode="json")

    # 两段式 pending 处置（docs/19）：confirm 回写新落库的 scan_result_id；
    # upsert/expire 与候选无关也要落库，放在主事务提交之后用独立 session 执行
    if pending_op:
        result_ids = {target: new_rows[-1].id} if signals else {}
        Scanner._apply_pending_ops({target: pending_op}, result_ids)

    return {
        "symbol": target,
        "updated_at": item.updated_at,
        "kline_count": len(klines),
        "last_close": float(klines[-1][4]) if klines else None,
        "hit": bool(signals),
        "signal_count": len(signals),
        "latest_scan": latest_scan,
    }


@router.delete("/{symbol}")
def delete_watchlist(symbol: str, db: Session = Depends(get_db)):
    """删除关注（按币种名）"""
    target = (symbol or "").strip().upper()
    item = db.execute(
        select(Watchlist).where(Watchlist.symbol == target)
    ).scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail=f"关注列表中不存在 {target}")
    db.delete(item)
    db.commit()
    return {"ok": True, "symbol": target}
