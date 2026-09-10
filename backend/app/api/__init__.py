from app.api.health import router as health_router
from app.api.scan import router as scan_router
from app.api.watchlist import router as watchlist_router

__all__ = ["health_router", "scan_router", "watchlist_router"]
