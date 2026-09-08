import httpx

from app.config import settings


class BinanceClient:
    """币安公开 API 封装（同步）"""

    def __init__(self):
        self.base_url = settings.BINANCE_BASE_URL
        self.timeout = settings.BINANCE_TIMEOUT
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"User-Agent": "trading-workbench/1.0"},
        )

    def get_exchange_info(self) -> dict:
        """获取交易对信息"""
        resp = self.client.get("/api/v3/exchangeInfo")
        resp.raise_for_status()
        return resp.json()

    def get_klines(
        self, symbol: str, interval: str = "1d", limit: int = 120
    ) -> list[list]:
        """获取 K 线数据
        返回: [[open_time, open, high, low, close, volume, ...], ...]
        """
        resp = self.client.get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self.client.close()

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
            # 排除稳定币作为 base 的交易对
            if base in stable_quotes:
                continue
            # 排除杠杆代币关键字
            if any(kw in base for kw in exclude_kw):
                continue
            symbols.append(s["symbol"])
        return symbols
