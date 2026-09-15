from app.api.health import router as health_router
from app.api.ops import router as ops_router
from app.api.scan import router as scan_router
from app.api.trades import router as trades_router
from app.api.watchlist import router as watchlist_router

__all__ = [
    "health_router",
    "ops_router",
    "scan_router",
    "trades_router",
    "watchlist_router",
]
