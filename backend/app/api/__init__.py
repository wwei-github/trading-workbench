from app.api.health import router as health_router
from app.api.kline_stream import router as kline_stream_router
from app.api.scan import router as scan_router
from app.api.trades import router as trades_router
from app.api.watchlist import router as watchlist_router

__all__ = [
    "health_router",
    "kline_stream_router",
    "scan_router",
    "trades_router",
    "watchlist_router",
]
