import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health_router, kline_stream_router, scan_router, watchlist_router
from app.config import settings
from app.database import init_db
from app.services.kline_hub import kline_hub

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        yield
    finally:
        await kline_hub.stop()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(scan_router)
app.include_router(watchlist_router)
app.include_router(kline_stream_router)


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "docs": "/docs"}
