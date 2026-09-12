"""币安 USDT-M 合约交易客户端（docs/06）

- 独立于行情 ExchangePool：凭据走 BINANCE_TRADE_KEY / BINANCE_TRADE_SECRET
- TRADING_TESTNET=true 走 testnet（https://testnet.binancefuture.com），路径与生产一致
- 同步 httpx（celery worker 内调用）；单账户专用，不做并发防护（每小时任务单线程执行）
"""
import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_PROD_BASE = "https://fapi.binance.com"
_TEST_BASE = "https://testnet.binancefuture.com"


class BinanceTradeError(Exception):
    """币安交易接口错误（含 HTTP 非 2xx 与返回码错误）"""


# 逐 symbol 的交易规则缓存（进程内）：stepSize/tickSize/minNotional/minQty
_filters_cache: dict[str, dict] = {}


class BinanceTrader:
    def __init__(self):
        self.key = settings.BINANCE_TRADE_KEY
        self.secret = settings.BINANCE_TRADE_SECRET
        self.base = _TEST_BASE if settings.TRADING_TESTNET else _PROD_BASE
        self._client = httpx.Client(timeout=15)
        if settings.TRADING_TESTNET:
            logger.info("BinanceTrader: testnet 模式 (%s)", self.base)

    # ── 基础请求 ──────────────────────────────────────────────

    def _req(self, method: str, path: str, params: Optional[dict] = None) -> dict | list:
        params = dict(params or {})
        params.setdefault("recvWindow", 10000)
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        resp = self._client.request(
            method, f"{self.base}{path}?{query}&signature={sig}",
            headers={"X-MBX-APIKEY": self.key},
        )
        if resp.status_code >= 400:
            raise BinanceTradeError(f"{path} {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    # ── 账户 / 持仓 ──────────────────────────────────────────

    def wallet_balance(self) -> dict:
        """USDT 钱包余额（balance 不含未实现盈亏）与可用余额（availableBalance）"""
        rows = self._req("GET", "/fapi/v2/balance")
        for r in rows:
            if r.get("asset") == "USDT":
                return {
                    "wallet": float(r.get("balance") or 0),
                    "available": float(r.get("availableBalance") or 0),
                }
        return {"wallet": 0.0, "available": 0.0}

    def positions(self) -> dict[str, dict]:
        """非零持仓：{symbol: {amt, initial_margin}}"""
        out: dict[str, dict] = {}
        for p in self._req("GET", "/fapi/v2/positionRisk"):
            amt = float(p.get("positionAmt") or 0)
            if amt != 0:
                out[p["symbol"]] = {
                    "amt": amt,
                    "initial_margin": float(p.get("initialMargin") or 0),
                    "entry_price": float(p.get("entryPrice") or 0),
                }
        return out

    # ── 交易规则与精度 ────────────────────────────────────────

    def filters(self, symbol: str) -> dict:
        """交易规则（进程内缓存）：{step, tick, min_qty, min_notional}"""
        if symbol in _filters_cache:
            return _filters_cache[symbol]
        info = self._req("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {"step": 0.001, "tick": 0.1, "min_qty": 0.001, "min_notional": 5.0}
            for rule in s.get("filters", []):
                ft = rule.get("filterType")
                if ft == "LOT_SIZE":
                    f["step"] = float(rule["stepSize"])
                    f["min_qty"] = float(rule["minQty"])
                elif ft == "MARKET_LOT_SIZE":
                    f["market_min_qty"] = float(rule["minQty"])
                elif ft == "MIN_NOTIONAL":
                    f["min_notional"] = float(rule["notional"])
                elif ft == "PRICE_FILTER":
                    f["tick"] = float(rule["tickSize"])
            _filters_cache[symbol] = f
            return f
        raise BinanceTradeError(f"exchangeInfo 无 {symbol}（testnet 可能未上架该合约）")

    @staticmethod
    def _floor_to(v: float, grid: float) -> float:
        if grid <= 0:
            return v
        return math.floor(v / grid + 1e-9) * grid

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self.filters(symbol)
        return self._floor_to(qty, f["step"])

    def round_price(self, symbol: str, price: float) -> float:
        f = self.filters(symbol)
        return self._floor_to(price, f["tick"])

    def price(self, symbol: str) -> float:
        """最新价（下单时点）"""
        r = self._req("GET", "/fapi/v2/ticker/price", {"symbol": symbol}, )
        return float(r["price"])

    # ── 杠杆 / 保证金模式 ─────────────────────────────────────

    def setup_leverage(self, symbol: str, leverage: int) -> None:
        self._req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        try:
            self._req("POST", "/fapi/v1/marginType",
                      {"symbol": symbol, "marginType": "ISOLATED"})
        except BinanceTradeError as e:
            if "-4046" not in str(e):  # 已是逐仓
                raise

    # ── 下单 / 撤单 / 查询 ────────────────────────────────────

    def market_order(self, symbol: str, side: str, qty: float) -> dict:
        return self._req("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
        })

    def stop_market_close(self, symbol: str, side: str, stop_price: float) -> dict:
        """止损单：触发后市价全平剩余仓位（closePosition）"""
        return self._req("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "STOP_MARKET",
            "stopPrice": stop_price, "closePosition": "true",
        })

    def take_profit_reduce(self, symbol: str, side: str, qty: float, stop_price: float) -> dict:
        """止盈单：触发后市价减仓指定数量（reduceOnly）"""
        return self._req("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price, "quantity": qty, "reduceOnly": "true",
        })

    def cancel_order(self, symbol: str, order_id: int) -> None:
        try:
            self._req("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        except BinanceTradeError as e:
            if "-2011" not in str(e):  # Unknown order sent = 已成交/已撤销
                raise

    def open_order_ids(self, symbol: str) -> set[int]:
        return {int(o["orderId"]) for o in self._req("GET", "/fapi/v1/openOrders", {"symbol": symbol})}

    def order(self, symbol: str, order_id: int) -> dict:
        return self._req("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        """本 symbol 自 since_ms 起的已实现盈亏合计（USDT，正/负）"""
        rows = self._req("GET", "/fapi/v1/income", {
            "symbol": symbol, "incomeType": "REALIZED_PNL",
            "startTime": since_ms, "limit": 1000,
        })
        return sum(float(r.get("income") or 0) for r in rows)
