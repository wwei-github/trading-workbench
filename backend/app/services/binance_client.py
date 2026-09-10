import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.scan import KlineCache

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0

# interval 字符串 → 对应分钟数
_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}


class BinanceClient:
    """币安公开 API 封装（同步，合约优先）"""

    def __init__(self):
        self.timeout = settings.BINANCE_TIMEOUT
        # 合约客户端
        self.futures_client = httpx.Client(
            base_url=settings.BINANCE_FUTURES_URL,
            timeout=self.timeout,
            headers={"User-Agent": "trading-workbench/1.0"},
        )
        # 现货客户端（保留备用）
        self.spot_client = httpx.Client(
            base_url=settings.BINANCE_BASE_URL,
            timeout=self.timeout,
            headers={"User-Agent": "trading-workbench/1.0"},
        )

    # ── 内部请求封装（429 退避 + 418 封禁保护）──

    def _request(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """带限流退避的 HTTP 请求

        - 429: 读取 Retry-After 头，等待后重试
        - 418: IP 被封禁，立即抛出异常（不可重试）
        - 5xx / 超时: 指数退避重试
        """
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = client.get(url, params=params)

                if resp.status_code == 429:
                    if attempt >= _MAX_RETRIES:
                        raise httpx.HTTPStatusError(
                            "429 Too Many Requests（重试次数耗尽）",
                            request=resp.request,
                            response=resp,
                        )
                    wait = self._parse_retry_after(resp)
                    logger.warning(
                        "429 限流，等待 %.1fs 后重试 (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code == 418:
                    wait = self._parse_retry_after(resp)
                    raise httpx.HTTPStatusError(
                        f"418 IP 被封禁，需等待 {wait:.0f}s 解禁",
                        request=resp.request,
                        response=resp,
                    )

                if resp.status_code >= 500:
                    if attempt >= _MAX_RETRIES:
                        break
                    wait = _BASE_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "%d 服务端错误，等待 %.1fs 后重试 (attempt %d/%d)",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp

            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt >= _MAX_RETRIES:
                    raise
                wait = _BASE_BACKOFF * (2 ** attempt)
                logger.warning(
                    "请求异常: %s，等待 %.1fs 后重试 (attempt %d/%d)",
                    e, wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)

        raise last_exc or httpx.HTTPError("未知请求错误")

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float:
        """解析 Retry-After 头（秒），默认 10s"""
        raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return 10.0

    # ── 合约 API ──

    def get_futures_exchange_info(self) -> dict:
        """获取合约交易对信息"""
        resp = self._request(self.futures_client, "/fapi/v1/exchangeInfo")
        return resp.json()

    def get_24h_tickers(self) -> list[dict]:
        """获取所有合约 24h 行情统计

        返回: [{symbol, lastPrice, quoteVolume, ...}, ...]
        """
        resp = self._request(self.futures_client, "/fapi/v1/ticker/24hr")
        return resp.json()

    def get_futures_symbols_with_volume(self) -> list[dict]:
        """获取 USDT 永续合约交易对，按 24h 成交额降序排列

        返回: [{symbol, volume_24h, last_price}, ...]
        """
        info = self.get_futures_exchange_info()
        exclude_kw = settings.EXCLUDE_KEYWORDS
        stable_quotes = {"USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP", "PAX"}

        # 筛选 TRADING 状态的 USDT 永续合约
        valid_symbols = set()
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != settings.QUOTE_ASSET:
                continue
            if s.get("contractType") != "PERPETUAL":
                continue
            base = s.get("baseAsset", "")
            if base in stable_quotes:
                continue
            if any(kw in base for kw in exclude_kw):
                continue
            valid_symbols.add(s["symbol"])

        # 获取 24h 行情
        tickers = self.get_24h_tickers()
        result = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in valid_symbols:
                continue
            try:
                vol = float(t.get("quoteVolume", 0))
                price = float(t.get("lastPrice", 0))
            except (ValueError, TypeError):
                continue
            if vol <= 0:
                continue
            result.append({"symbol": sym, "volume_24h": vol, "last_price": price})

        # 按 24h 成交额降序
        result.sort(key=lambda x: x["volume_24h"], reverse=True)
        return result

    def get_klines(
        self, symbol: str, interval: str = "1h", limit: int = 240
    ) -> list[list]:
        """获取合约 K 线数据（带小时级缓存）

        - 同一 symbol+interval 在同一 K 线周期内命中缓存
        - 跨 K 线周期自动拉取新数据
        - 缓存查询失败降级为直接请求币安

        返回: [[open_time, open, high, low, close, volume, ...], ...]
        """
        kline_hour = self._calc_kline_hour(interval)

        # 1. 查缓存
        try:
            cached = self._get_cached_klines(symbol, interval, kline_hour)
            if cached is not None:
                logger.debug("K线缓存命中: %s %s @ %s (len=%d)", symbol, interval, kline_hour, len(cached))
                return cached
        except Exception as e:
            logger.warning("K线缓存查询失败（降级直连币安）: %s", e)

        # 2. 请求币安
        resp = self._request(
            self.futures_client,
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        klines = resp.json()

        # 3. 写入缓存（忽略错误，不影响主流程）
        try:
            self._upsert_kline_cache(symbol, interval, kline_hour, klines)
        except Exception as e:
            logger.warning("K线缓存写入失败（忽略）: %s", e)

        return klines

    @staticmethod
    def _calc_kline_hour(interval: str) -> datetime:
        """根据 interval 计算当前 K 线周期的起点（UTC，对齐到整点）

        1h → 当前 UTC 时间去掉分秒毫秒；
        4h → 对齐到 0/4/8/12/16/20 点；
        其他周期同理。
        """
        minutes = _INTERVAL_MINUTES.get(interval, 60)
        now = datetime.now(timezone.utc)
        # 对齐到 interval 起点
        total_minutes = now.hour * 60 + now.minute
        aligned_minutes = (total_minutes // minutes) * minutes
        h = aligned_minutes // 60
        m = aligned_minutes % 60
        return now.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=None)

    @staticmethod
    def _get_cached_klines(symbol: str, interval: str, kline_hour: datetime) -> Optional[list]:
        """查缓存，命中返回 klines 列表，未命中返回 None"""
        sdb = SessionLocal()
        try:
            row = sdb.execute(
                select(KlineCache).where(
                    KlineCache.symbol == symbol,
                    KlineCache.interval == interval,
                    KlineCache.kline_hour == kline_hour,
                )
            ).scalars().first()
            if row:
                return row.klines
            return None
        finally:
            sdb.close()

    @staticmethod
    def _upsert_kline_cache(symbol: str, interval: str, kline_hour: datetime, klines: list) -> None:
        """写入或更新缓存"""
        sdb = SessionLocal()
        try:
            row = sdb.execute(
                select(KlineCache).where(
                    KlineCache.symbol == symbol,
                    KlineCache.interval == interval,
                    KlineCache.kline_hour == kline_hour,
                )
            ).scalars().first()
            if row:
                row.klines = klines
                row.updated_at = datetime.utcnow()
            else:
                row = KlineCache(
                    symbol=symbol,
                    interval=interval,
                    kline_hour=kline_hour,
                    klines=klines,
                )
                sdb.add(row)
            sdb.commit()
        except Exception:
            sdb.rollback()
            raise
        finally:
            sdb.close()

    # ── 现货 API（保留备用）──

    def get_exchange_info(self) -> dict:
        """获取现货交易对信息"""
        resp = self._request(self.spot_client, "/api/v3/exchangeInfo")
        return resp.json()

    def get_usdt_symbols(self) -> list[str]:
        """获取所有 USDT 现货交易对，排除杠杆代币和稳定币"""
        info = self.get_exchange_info()
        exclude_kw = settings.EXCLUDE_KEYWORDS
        stable_quotes = {"USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP", "PAX"}
        symbols = []
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != settings.QUOTE_ASSET:
                continue
            base = s.get("baseAsset", "")
            if base in stable_quotes:
                continue
            if any(kw in base for kw in exclude_kw):
                continue
            symbols.append(s["symbol"])
        return symbols

    def close(self):
        self.futures_client.close()
        self.spot_client.close()
