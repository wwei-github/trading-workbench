import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    health_router,
    kline_stream_router,
    ops_router,
    scan_router,
    trades_router,
    watchlist_router,
)
from app.config import settings
from app.database import init_db
from app.services.kline_hub import kline_hub
from app.services.log_sink import attach_db_log_sink

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
attach_db_log_sink()  # WARNING 及以上日志落库（前端「系统日志」）

logger = logging.getLogger("app.api")


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
app.include_router(trades_router)
app.include_router(ops_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """未处理接口异常：落「系统日志」并返回 500（4xx 客户端错误不在此列）"""
    logger.exception("接口异常 %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{exc}"})


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "docs": "/docs"}
