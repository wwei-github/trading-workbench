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
"""
import hashlib
import hmac
import logging
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
            if acc is not None and acc.configured:
                self.account = acc
                self.testnet = acc.testnet
        except Exception as e:  # 表不存在/DB 异常等：降级为仅公开端点可用
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
        r = self._client.get(
            f"{self.base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
        )
        r.raise_for_status()
        return r.json()

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

    def account(self) -> Dict[str, Any]:
        """现货账户（只读）：返回 balances 中非零资产列表。"""
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
            for b in self.account()["balances"]:
                if b["asset"] == "USDT":
                    return b["free"] + b["locked"]
        except Exception as e:
            logger.warning("spot usdt_balance 查询失败: %s", e)
        return None
