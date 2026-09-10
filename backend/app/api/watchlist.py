"""关注列表 API：币种的增删查，持久保存"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.scan import Watchlist
from app.schemas.scan import (
    WatchlistAddRequest,
    WatchlistItemOut,
    WatchlistListResponse,
)
from app.services.binance_client import BinanceClient
from app.services.exchange_pool import ExchangePool, AllExchangesFailed

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
    """关注列表（按添加时间倒序）"""
    items = db.execute(
        select(Watchlist).order_by(Watchlist.created_at.desc())
    ).scalars().all()
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
