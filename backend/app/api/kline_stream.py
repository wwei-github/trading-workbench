"""实时 K 线 SSE 端点（docs/07）

GET /api/scans/klines/{symbol}/stream?interval=1h

事件流：
- snapshot: {symbol, interval, bar: {t,o,h,l,c,v} | null}  连接首帧
- bar:      {t,o,h,l,c,v,x}                                每次上游推送（WS 250ms / REST 3s）
- degraded: {message}                                      上游失败（按频道去重）
- `: ping`  SSE 注释行，15s 无事件时保活

注意：业务错误事件命名为 degraded 而非 error——与 EventSource 原生 error 事件
（连接失败/断开）共享 addEventListener 通道，命名冲突会混淆两类错误。
"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.watchlist import normalize_symbol
from app.services.binance_client import _INTERVAL_MINUTES
from app.services.exchange_pool import ExchangePool
from app.services.kline_hub import kline_hub

router = APIRouter(prefix="/api/scans", tags=["kline-stream"])

_HEARTBEAT_S = 15  # 无事件心跳间隔（nginx read timeout 600s，余量充足）


def _bar_from_kline(k: list) -> dict:
    """8 字段 K 线行 → SSE bar（x 由 close_time 是否已过推导）"""
    return {
        "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
        "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
        "x": float(k[6]) <= time.time() * 1000,
    }


async def _rest_snapshot(symbol: str, interval: str) -> dict | None:
    """REST 快照：实时链路取最新一根（缓存双旁路），失败降级旧缓存，全失败 None"""
    pool = ExchangePool()
    try:
        klines = await asyncio.to_thread(pool.get_recent_klines, symbol, interval, 2)
        if klines:
            return _bar_from_kline(klines[-1])
    except Exception:
        pass
    try:
        # allow_stale=True：交易所全挂时退回最近一份缓存展示
        klines = await asyncio.to_thread(
            pool.get_klines, symbol, interval, 2, True, False
        )
        if klines:
            return _bar_from_kline(klines[-1])
    except Exception:
        pass
    return None


@router.get("/klines/{symbol}/stream")
async def stream_klines(symbol: str, interval: str = Query("1h")):
    """订阅指定币种的实时 K 线推送（SSE，原生 EventSource 直连）"""
    sym = normalize_symbol(symbol)
    if interval not in _INTERVAL_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的周期 {interval}，可用：{sorted(_INTERVAL_MINUTES)}",
        )
    ch = (sym.lower(), interval)

    async def gen():
        q = kline_hub.subscribe(ch)
        try:
            # EventSource 断线自动重连间隔（服务端重启/网络抖动后 5s 回连）
            yield "retry: 5000\n\n"
            bar = kline_hub.snapshot(ch)
            if bar is None:
                bar = await _rest_snapshot(sym, interval)
            payload = json.dumps(
                {"symbol": sym, "interval": interval, "bar": bar},
                ensure_ascii=False,
            )
            yield f"event: snapshot\ndata: {payload}\n\n"
            while True:
                try:
                    kind, item = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {kind}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            kline_hub.unsubscribe(ch, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 按此响应头逐请求关闭 proxy buffering
        },
    )
