import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    health_router,
    market_router,
    ops_router,
    scan_router,
    shadow_router,
    spot_router,
    tactics_router,
    trades_router,
    watchlist_router,
)
from app.config import settings
from app.database import init_db
from app.services.feishu_bot import start_card_listener
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
    start_card_listener()  # 飞书卡片回调长连接（自建应用，未配置自动跳过）
    # P-09 事件驱动执行（docs/16）：user-data-stream + mark price 长连接，
    # EVENT_EXEC_ENABLED 开关控制（默认关）；失败吞掉不影响主服务，巡检兜底
    try:
        from app.services.event_stream import start_event_stream
        start_event_stream()
    except Exception:
        logging.getLogger("app.api").exception("事件流启动失败（巡检兜底不受影响）")
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求级访问日志（2026-10-05 补缺口 1）：uvicorn access log 只进 stdout 不落库，
# 系统日志页看不到「谁何时调了什么接口、哪些慢、哪些 4xx/5xx」。
# 只落「值得关注」的请求防刷库（正常 2xx 静默——量大会把 WARNING 级日志淹没）：
# - 耗时 ≥2s 的慢请求（WARNING）
# - 状态 ≥400 的错误请求（WARNING；未处理异常已由 500 handler 以 ERROR 落库，此处不重复 traceback）
_SLOW_REQUEST_MS = 2000
_req_logger = logging.getLogger("app.api.request")


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500  # call_next 抛异常时按 500 计（异常细节由 500 handler 落库）
    try:
        resp = await call_next(request)
        status_code = resp.status_code
        return resp
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        slow = elapsed_ms >= _SLOW_REQUEST_MS
        if slow or status_code >= 400:
            level = logging.ERROR if status_code >= 500 else logging.WARNING
            _req_logger.log(
                level, "接口%s %s %s 状态 %d 耗时 %dms",
                "慢请求" if slow and status_code < 400 else "错误",
                request.method, request.url.path, status_code, elapsed_ms)

app.include_router(health_router)
app.include_router(market_router)  # 大盘环境/市场情绪（Header 大盘条）
app.include_router(scan_router)
app.include_router(watchlist_router)
app.include_router(trades_router)
app.include_router(ops_router)
app.include_router(spot_router)  # 现货屯币（docs/18，独立模块）
app.include_router(tactics_router)  # 战法（2026-10-02，仅借鉴标注不拦截）
app.include_router(shadow_router)  # 影子模式评估报告（docs/14，只读不决策）


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """未处理接口异常：落「系统日志」并返回 500（4xx 客户端错误不在此列）"""
    logger.exception("接口异常 %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{exc}"})


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "docs": "/docs"}
