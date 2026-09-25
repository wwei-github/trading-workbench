"""币安现货 REST 客户端（docs/18，独立封装）。

零侵入约束（docs/18 §1 隔离原则第 2 条）：不复用合约 fapi 客户端实例
（binance_trader），互不共享连接与缓存；仅引用 account_registry 解析 key。

P0 能力（只算不发单）：
- 公开端点：klines（1d/1w/1M，月K 用 '1M'）、ticker 价格
- 签名端点：account（现货持仓只读查询，overview 页展示用）
- 下单能力 P1 再加（spot_order 方法留待 P1，避免 P0 出现可下单面）

环境语义（docs/18，2026-09-23 拍板）：现货环境与合约完全独立——
spot_trading_testnet=true（默认）→ testnet-1（testnet.binance.vision），
false → spot_live_trading_keys 启用账号（第一个，P0 只读）→ 回落 live-1。
账号注册表与合约共用（.env BINANCE_LIVE_{N}_*），环境开关互不影响。
注意：现货测试网与合约测试网是两套独立系统，key 不互通（2026-09-24 实证）——
现货测试网专用凭据走 .env BINANCE_SPOT_TESTNET_*（可选，未配置回落 BINANCE_TESTNET_*）。
"""
import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger("app.spot")

# 现货测试网独立域名（与合约 testnet fapi 不同）
SPOT_TESTNET_BASE = "https://testnet.binance.vision"

# 现货 K 线 interval 白名单（docs/18：最小决策周期日K，无日内）
SPOT_KLINE_INTERVALS = ("1d", "1w", "1M")


class SpotClient:
    """币安现货客户端：每次构造独立 httpx.Client（实例不跨账号缓存）。"""

    def __init__(self, account_key: Optional[str] = None):
        # 延迟导入避免 registry 循环依赖
        from app.services.account_registry import get_account, resolve_spot_account

        self.account = None
        # 账号解析失败原因（overview 降级提示用；None=解析成功）
        self.resolve_error: Optional[str] = None
        self.testnet = True
        try:
            from app.database import SessionLocal

            db = SessionLocal()
            try:
                if account_key:
                    acc = get_account(account_key)
                    if acc is None:
                        # 未注册 key：回落现货环境默认账号
                        acc = resolve_spot_account(db)
                else:
                    acc = resolve_spot_account(db)
            finally:
                db.close()
            # 注册表构建时已校验 key/secret 成对（半配置启动即抛错），此处 acc 非 None 即可用
            if acc is not None:
                # 现货测试网 key 覆盖（_with_spot_testnet_keys）：显式传 testnet 账号 key
                # 时 get_account 直查注册表拿的是合约测试网凭据，必须同样应用覆盖，
                # 否则现货测试网 401（2026-09-24 实证修复）
                if acc.testnet:
                    from app.services.account_registry import _with_spot_testnet_keys

                    acc = _with_spot_testnet_keys([acc])[0]
                self.account = acc
                self.testnet = acc.testnet
            else:
                self.resolve_error = "现货账号未配置（.env 缺 BINANCE_TESTNET_API_KEY/SECRET_KEY）"
        except Exception as e:  # 表不存在/DB 异常等：降级为仅公开端点可用
            self.resolve_error = f"现货账号解析失败: {e}"
            logger.warning("spot 账号解析失败，公开端点降级可用: %s", e)

        self.base_url = SPOT_TESTNET_BASE if self.testnet else settings.BINANCE_BASE_URL
        self._client = httpx.Client(timeout=15.0)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ---------- 公开端点 ----------

    def klines(self, symbol: str, interval: str = "1d", limit: int = 300) -> List[List[Any]]:
        """现货 K 线。返回币安原始数组（[open_time, o, h, l, c, v, ...]）。

        interval 白名单：1d / 1w / 1M（docs/18 §7.2 现货必须走 /api/v3 端点，
        合约 fapi 存在资金费率/溢价基差，屯币执行在现货须用现货价格）。
        """
        if interval not in SPOT_KLINE_INTERVALS:
            raise ValueError(f"不支持的现货 K 线周期: {interval}")
        try:
            r = self._client.get(
                f"{self.base_url}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            # WARNING 落库（系统日志面板可见）：所有现货K线拉取失败统一留痕
            logger.warning("spot klines %s %s 失败: %s", symbol, interval, e)
            raise

    def ticker_price(self, symbol: str) -> Optional[float]:
        """现价（公开端点；失败返回 None 由调用方降级）。"""
        try:
            r = self._client.get(
                f"{self.base_url}/api/v3/ticker/price", params={"symbol": symbol}
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            logger.warning("spot ticker_price %s 失败: %s", symbol, e)
            return None

    # ---------- 签名端点（只读） ----------

    def _signed_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.account is None:
            raise RuntimeError("spot 账号未配置，签名端点不可用")
        params = dict(extra or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        query = urllib.parse.urlencode(params)
        sig = hmac.new(
            self.account.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        return params

    def fetch_account(self) -> Dict[str, Any]:
        """现货账户（只读）：返回 balances 中非零资产列表。

        注意：不能命名为 account——__init__ 会写实例属性 self.account（当前账号），
        同名方法会被属性遮蔽导致 NoneType not callable（2026-09-23 生产事故）。
        """
        if self.account is None:
            raise RuntimeError(self.resolve_error or "现货账号未配置")
        params = self._signed_params()
        r = self._client.get(
            f"{self.base_url}/api/v3/account",
            params=params,
            headers={"X-MBX-APIKEY": self.account.api_key},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "can_trade": data.get("canTrade", False),
            "balances": [
                {"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])}
                for b in data.get("balances", [])
                if float(b["free"]) + float(b["locked"]) > 0
            ],
        }

    def usdt_balance(self) -> Optional[float]:
        """USDT 可用余额（overview 现金池展示；失败 None）。"""
        try:
            for b in self.fetch_account()["balances"]:
                if b["asset"] == "USDT":
                    return b["free"] + b["locked"]
        except Exception as e:
            logger.warning("spot usdt_balance 查询失败: %s", e)
        return None

    def asset_balance(self, asset: str) -> Optional[float]:
        """单资产可用余额（free；SELL 波段仓上限计算用；失败 None）。"""
        try:
            for b in self.fetch_account()["balances"]:
                if b["asset"] == asset:
                    return b["free"]
        except Exception as e:
            logger.warning("spot asset_balance %s 查询失败: %s", asset, e)
        return None

    # ---------- 下单（P1：半自动确认后执行） ----------

    # exchangeInfo 类级缓存：{base_url: {symbol: {step, tick, min_notional, ts}}}
    # 现货过滤器极少变更，1h TTL 足够；类级共享避免每单重复拉全量规则
    _EXCHANGE_INFO_TTL_MS = 3_600_000
    _exchange_info_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _symbol_filters(self, symbol: str) -> Dict[str, Any]:
        """LOT_SIZE.stepSize / PRICE.tickSize / NOTIONAL.minNotional（缓存 1h）。"""
        cache = SpotClient._exchange_info_cache.setdefault(self.base_url, {})
        hit = cache.get(symbol)
        now_ms = int(time.time() * 1000)
        if hit and now_ms - hit["ts"] < SpotClient._EXCHANGE_INFO_TTL_MS:
            return hit
        r = self._client.get(f"{self.base_url}/api/v3/exchangeInfo", params={"symbol": symbol})
        r.raise_for_status()
        data = r.json()
        # ?symbol= 形式返回仍包在 symbols 数组里（2026-09-24 实证）；
        # 旧实现误读顶层 filters → 取不到 → 静默用默认值，step 默认 1e-05
        # 对 BTCUSDT 碰巧成立、对 BNB/ETH 全错，市价单直接 -1013 LOT_SIZE。
        # 过滤器拿不到必须报错：错误精度会导致超卖，静默兜底比失败更危险
        syms = data.get("symbols") or []
        filters = (syms[0].get("filters") if syms else None) or data.get("filters") or []
        if not filters:
            raise RuntimeError(f"exchangeInfo 未返回 {symbol} 过滤器，拒绝按猜测精度下单")
        step, tick, min_notional = 0.00001, 0.01, 5.0
        for f in filters:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
            elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional", 5.0))
        info = {"step": step, "tick": tick, "min_notional": min_notional, "ts": now_ms}
        cache[symbol] = info
        return info

    def round_qty(self, symbol: str, qty: float) -> float:
        """数量按 LOT_SIZE.stepSize 向下取整（卖出可执行量守恒，绝不超卖）。"""
        step = self._symbol_filters(symbol)["step"]
        if step <= 0:
            return qty
        return math.floor(qty / step) * step

    def round_quote(self, symbol: str, quote: float) -> float:
        """金额按 PRICE.tickSize 向下取整（quoteOrderQty 也受 tick 约束）。"""
        tick = self._symbol_filters(symbol)["tick"]
        if tick <= 0:
            return quote
        return math.floor(quote / tick) * tick

    def market_order(
        self,
        symbol: str,
        side: str,
        quantity: Optional[float] = None,
        quote_order_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        """市价单（P1 半自动确认后调用）。

        - BUY 用 quoteOrderQty（按金额买，天然规避数量精度）
        - SELL 用 quantity（卖出必须给数量，调用方先 round_qty）
        返回 {order_id, executed_qty, quote_spent, avg_price, status}。
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"非法方向: {side}")
        if self.account is None:
            raise RuntimeError(self.resolve_error or "现货账号未配置，无法下单")
        params: Dict[str, Any] = {
            "symbol": symbol, "side": side, "type": "MARKET",
            "newOrderRespType": "FULL",  # 回带 fills 明细，均价/成交量直接用
        }
        if side == "BUY":
            if not quote_order_qty or quote_order_qty <= 0:
                raise ValueError("BUY 必须给 quote_order_qty")
            params["quoteOrderQty"] = self.round_quote(symbol, quote_order_qty)
        else:
            if not quantity or quantity <= 0:
                raise ValueError("SELL 必须给 quantity")
            params["quantity"] = self.round_qty(symbol, quantity)
        r = self._client.post(
            f"{self.base_url}/api/v3/order",
            params=self._signed_params(params),
            headers={"X-MBX-APIKEY": self.account.api_key},
        )
        r.raise_for_status()
        d = r.json()
        executed = float(d.get("executedQty", 0) or 0)
        spent = float(d.get("cummulativeQuoteQty", 0) or 0)
        return {
            "order_id": str(d.get("orderId", "")),
            "status": d.get("status", ""),
            "executed_qty": executed,
            "quote_spent": spent,
            "avg_price": (spent / executed) if executed > 0 else None,
        }
