"""市场环境数据源（docs/04 第一梯队①②）：资金费率/持仓量、恐惧贪婪指数、大盘广度

- 全部静默降级：任何失败返回 None，prompt 中显示"不可用"，绝不阻塞 AI 分析
- 进程内 TTL 缓存：funding 10min、F&G 30min、大盘 5min
"""
import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/"

# 进程内缓存 {key: (expire_ts, data)}
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str) -> Optional[object]:
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _cache_set(key: str, data: object, ttl: float) -> None:
    _cache[key] = (time.time() + ttl, data)


def _futures_client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.BINANCE_FUTURES_URL,
        timeout=10,
        headers={"User-Agent": "trading-workbench/1.0"},
    )


def get_funding(symbol: str) -> Optional[dict]:
    """资金费率 + 持仓量（币安合约）。返回 {"funding_rate_pct", "open_interest"} 或 None"""
    key = f"funding:{symbol}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        with _futures_client() as c:
            resp = c.get("/fapi/v1/premiumIndex", params={"symbol": symbol})
            resp.raise_for_status()
            rate = float(resp.json().get("lastFundingRate", 0)) * 100
            oi = None
            try:
                resp2 = c.get("/fapi/v1/openInterest", params={"symbol": symbol})
                resp2.raise_for_status()
                oi = float(resp2.json().get("openInterest", 0))
            except Exception:
                pass
        data = {"funding_rate_pct": round(rate, 4), "open_interest": oi}
        _cache_set(key, data, 600)
        return data
    except Exception as e:
        logger.debug("get_funding %s 失败: %s", symbol, e)
        return None


def get_fear_greed() -> Optional[dict]:
    """恐惧贪婪指数（alternative.me）。返回 {"value", "label"} 或 None"""
    cached = _cache_get("fng")
    if cached is not None:
        return cached
    try:
        resp = httpx.get(_FNG_URL, params={"limit": 1}, timeout=10)
        resp.raise_for_status()
        d = resp.json()["data"][0]
        data = {"value": int(d["value"]), "label": d["value_classification"]}
        _cache_set("fng", data, 1800)
        return data
    except Exception as e:
        logger.debug("get_fear_greed 失败: %s", e)
        return None


def get_market_breadth(pool) -> Optional[dict]:
    """大盘广度：BTC/ETH 的 EMA 形态 + 24h 涨跌幅。

    K 线走 ExchangePool（同周期内命中缓存，零交易所 IO）。
    返回 {"BTCUSDT": {"ema_state", "ema_label", "change_24h_pct"}, ...} 或 None
    """
    cached = _cache_get("breadth")
    if cached is not None:
        return cached
    from app.services.strategy.ema import analyze_ema

    out: dict = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        try:
            klines = pool.get_klines(sym, "1h", 300)
        except Exception as e:
            logger.debug("大盘K线 %s 获取失败: %s", sym, e)
            continue
        ema = analyze_ema(klines)
        closed = klines[:-1]
        change = 0.0
        if len(closed) >= 25:
            change = (float(closed[-1][4]) / float(closed[-25][4]) - 1) * 100
        out[sym] = {
            "ema_state": ema["state"] if ema else None,
            "ema_label": ema["state_label"] if ema else None,
            "change_24h_pct": round(change, 2),
        }
    if not out:
        return None
    _cache_set("breadth", out, 300)
    return out
