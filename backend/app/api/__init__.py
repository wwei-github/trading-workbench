from app.api.backtest import router as backtest_router
from app.api.health import router as health_router
from app.api.ledger import router as ledger_router
from app.api.market import router as market_router
from app.api.ops import router as ops_router
from app.api.scan import router as scan_router
from app.api.shadow import router as shadow_router
from app.api.spot import router as spot_router
from app.api.strategies import router as strategies_router
from app.api.tactics import router as tactics_router
from app.api.trades import router as trades_router
from app.api.watchlist import router as watchlist_router

__all__ = [
    "backtest_router",
    "health_router",
    "ledger_router",
    "market_router",
    "ops_router",
    "scan_router",
    "shadow_router",
    "spot_router",
    "strategies_router",
    "tactics_router",
    "trades_router",
    "watchlist_router",
]
