"""关注列表 API：币种的增删查、K 线手动刷新，持久保存"""
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
from app.services.scanner import _get_scan_config, classify_volume
from app.services.strategy import detect_all_signals
from app.services.strategy.ema import analyze_ema

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def normalize_symbol(raw: str) -> str:
    """规范币种输入：去空格转大写，未带 USDT 后缀时自动补全"""
    s = (raw or "").strip().upper().replace("/", "").replace("-", "")
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

    pool = ExchangePool()
    try:
        klines = pool.get_klines(target, interval, 500, force_refresh=True)
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线刷新失败（{e.summary}）")

    # 信号判定：与扫描器 scan_one 完全同一套逻辑与参数
    signals: list[dict] = []
    if klines:
        ema = analyze_ema(klines)
        config = {
            "min_klines": 30,
            "swing_order": cfg.swing_order,
            "breakout_threshold": float(cfg.breakout_threshold),
            "r_squared_threshold": float(cfg.r_squared_threshold),
            "pullback_tolerance": float(cfg.pullback_tolerance),
            "key_level_tolerance": float(cfg.key_level_tolerance),
            "level_merge_threshold": float(cfg.level_merge_threshold),
            "max_trend_slope": 0.005,
            "ema": ema,
        }
        signals = detect_all_signals(klines, config)
        vol, vol_type = classify_volume(klines)
        ema_state = ema["state"] if ema else None
        for sig in signals:
            sig["volume"] = vol
            sig["volume_type"] = vol_type
            sig["ema_state"] = ema_state

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
                signal_reason=sig.get("signal_reason"),
                strength=sig.get("strength"),
                ema_state=sig.get("ema_state"),
                position=sig.get("position"),
                key_levels=sig.get("key_levels"),
                volume_24h=volume_24h,
                volume=sig.get("volume", 0),
                volume_type=sig.get("volume_type", "平量"),
                is_repeat=is_repeat,
            )
            for sig in signals
        ]
        db.add_all(new_rows)

    item.updated_at = datetime.utcnow()
    db.commit()

    if signals:
        db.refresh(new_rows[-1])
        latest_scan = ScanResultOut.model_validate(new_rows[-1]).model_dump(mode="json")

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
