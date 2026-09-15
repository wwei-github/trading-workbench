"""币安 USDT-M 合约交易客户端（docs/06）

- 独立于行情 ExchangePool：凭据走 BINANCE_API_KEY / BINANCE_SECRET_KEY
- TRADING_TESTNET=true 走 testnet（https://testnet.binancefuture.com），路径与生产一致
- 同步 httpx（celery worker 内调用）；单账户专用，不做并发防护（每小时任务单线程执行）
"""
import hashlib
import hmac
import logging
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal
from typing import Optional, Union

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
        self.key = settings.BINANCE_API_KEY
        self.secret = settings.BINANCE_SECRET_KEY
        self.base = _TEST_BASE if settings.TRADING_TESTNET else _PROD_BASE
        self._client = httpx.Client(timeout=15)
        if settings.TRADING_TESTNET:
            logger.info("BinanceTrader: testnet 模式 (%s)", self.base)

    # ── 基础请求 ──────────────────────────────────────────────

    def _req(self, method: str, path: str, params: Optional[dict] = None) -> Union[dict, list]:
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
        info = self._req("GET", "/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {"step": 0.001, "tick": 0.1, "min_qty": 0.001, "min_notional": 5.0}
            for rule in s.get("filters", []):
                ft = rule.get("filterType")
                if ft == "LOT_SIZE":
                    f["step"] = float(rule["stepSize"])
                    f["min_qty"] = float(rule["minQty"])
                    f["max_qty"] = float(rule.get("maxQty") or 1e18)
                elif ft == "MARKET_LOT_SIZE":
                    f["market_min_qty"] = float(rule["minQty"])
                    f["market_max_qty"] = float(rule.get("maxQty") or 1e18)
                elif ft == "MIN_NOTIONAL":
                    f["min_notional"] = float(rule["notional"])
                elif ft == "PRICE_FILTER":
                    f["tick"] = float(rule["tickSize"])
            _filters_cache[symbol] = f
            return f
        raise BinanceTradeError(f"exchangeInfo 无 {symbol}（testnet 可能未上架该合约）")

    @staticmethod
    def _floor_to(v: float, grid: float) -> float:
        """向下取整到 grid 网格的整数倍。Decimal 运算避免 FP 噪声（如 64*0.0001=0.006400000000000001 触发 -1111），
        且 grid=1.0 等值不受字符串尾零干扰（quantize 会被 '1.0' 带成 1 位小数）"""
        if grid <= 0:
            return v
        g = Decimal(str(grid))
        return float(Decimal(str(v)) // g * g)

    @staticmethod
    def _dec_str(v) -> str:
        """请求参数用：规范十进制字符串（去 FP 噪声/科学计数法/尾零，4269.0→'4269'，0.2540→'0.254'）"""
        return format(Decimal(str(v)).normalize(), "f")

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self.filters(symbol)
        return self._floor_to(qty, f["step"])

    def round_price(self, symbol: str, price: float) -> float:
        f = self.filters(symbol)
        return self._floor_to(price, f["tick"])

    def price(self, symbol: str) -> float:
        """最新价（下单时点）"""
        r = self._req("GET", "/fapi/v2/ticker/price", {"symbol": symbol})
        if "price" not in r:
            raise BinanceTradeError(f"ticker/price 响应异常: {str(r)[:100]}")
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
    # 2025-12-09 起 STOP_MARKET / TAKE_PROFIT_MARKET 等条件单迁移到 Algo Order API（/fapi/v1/algoOrder），
    # 经典 /fapi/v1/order 对这些类型返回 -4120。algo 单以 algoId 标识（triggerPrice 替代 stopPrice）。

    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> dict:
        params = {"symbol": symbol, "side": side, "type": "MARKET",
                  "quantity": self._dec_str(qty)}
        if reduce_only:
            # 只减仓：无持仓时会被交易所拒绝——防止"平仓"反向开成裸仓
            params["reduceOnly"] = "true"
        r = self._req("POST", "/fapi/v1/order", params)
        if "orderId" not in r:
            raise BinanceTradeError(f"MARKET 下单响应异常: {str(r)[:120]}")
        return r

    def stop_market_close(self, symbol: str, side: str, stop_price: float) -> dict:
        """止损单（algo）：触发后市价全平剩余仓位（closePosition），返回含 algoId"""
        return self._req("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "STOP_MARKET", "triggerPrice": self._dec_str(stop_price),
            "closePosition": "true",
        })

    def take_profit_reduce(self, symbol: str, side: str, qty: float, stop_price: float) -> dict:
        """止盈单（algo）：触发后市价减仓指定数量（reduceOnly），返回含 algoId"""
        return self._req("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "TAKE_PROFIT_MARKET", "triggerPrice": self._dec_str(stop_price),
            "quantity": self._dec_str(qty), "reduceOnly": "true",
        })

    def cancel_order(self, symbol: str, order_id: int) -> None:
        """撤销 algo 条件单（order_id 实为 algoId）。已成交/已撤销的忽略"""
        try:
            self._req("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": order_id})
        except BinanceTradeError as e:
            msg = str(e)
            if "-2011" in msg or "unknown" in msg.lower() or "not exist" in msg.lower():
                return
            raise

    def open_algo_orders(self, symbol: str) -> list:
        """在挂的条件单原始列表（SL/TP 均为 algo 单，含 algoId/type/side/closePosition 字段）"""
        return self._req("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})

    def all_open_algo_orders(self) -> list:
        """全账户在挂条件单（不带 symbol 一次拉全，孤儿清扫用）"""
        return self._req("GET", "/fapi/v1/openAlgoOrders", {})

    def cancel_stale_close_position_algo(self, symbol: str, side: str) -> None:
        """撤销该 symbol 指定方向全部 closePosition 条件单。

        SL 移动时旧单撤单竞态未生效的兜底（交易所 -4130：同方向已有 closePosition 单）；
        TP 单是 reduceOnly 带数量，不受影响。
        """
        for o in self.open_algo_orders(symbol):
            try:
                if (
                    str(o.get("closePosition")).lower() == "true"
                    and o.get("side") == side
                    and o.get("algoId")
                ):
                    self.cancel_order(symbol, int(o["algoId"]))
            except (TypeError, ValueError, AttributeError):
                continue

    def cancel_all_algo(self, symbol: str) -> None:
        """撤销该 symbol 全部在挂 algo 条件单（紧急平仓/结算清理用）"""
        try:
            self._req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
        except BinanceTradeError as e:
            msg = str(e)
            if "-2011" in msg or "unknown" in msg.lower() or "not exist" in msg.lower():
                return
            raise

    def open_order_ids(self, symbol: str) -> set[int]:
        """在挂的条件单 algoId 集合（SL/TP 均为 algo 单）"""
        return {int(o["algoId"]) for o in self._req("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})}

    def order(self, symbol: str, order_id: int) -> dict:
        return self._req("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def income_rows(self, symbol: str, since_ms: int) -> list:
        """本 symbol 自 since_ms 起的 income 明细（净盈亏/手续费/资金费/强平清算等）"""
        return self._req("GET", "/fapi/v1/income", {
            "symbol": symbol, "startTime": since_ms, "limit": 1000,
        })

    INCOME_KEEP = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR"}

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        """本 symbol 自 since_ms 起的净盈亏（USDT，正/负）：
        已实现盈亏 + 手续费 + 资金费 + 强平清算（INSURANCE_CLEAR，强平时真实划走的钱；
        币安 income 中费用为负值，直接求和即净额）"""
        rows = self.income_rows(symbol, since_ms)
        return self.sum_income(rows, self.INCOME_KEEP)

    def realized_price_pnl_since(self, symbol: str, since_ms: int) -> float:
        """自 since_ms 起的价差已实现盈亏（仅 REALIZED_PNL，不含费用——
        供止盈分档事件计算"本档盈亏"：各档差值即该档成交的价差收益）"""
        rows = self.income_rows(symbol, since_ms)
        return self.sum_income(rows, {"REALIZED_PNL"})

    def fill_tranche_pnls(
        self, symbol: str, since_ms: int, qty_tp1: float, qty_tp2: float,
    ) -> tuple[float, float]:
        """把两档止盈各自的价差盈亏从交易所成交明细拆出（userTrades.realizedPnl，
        与 income REALIZED_PNL 同源）。

        背景：TP 为条件单（TAKE_PROFIT_MARKET），触发后生成新 orderId 成交，
        下单时记录的 algo 单号对不上成交回报——只能按成交归集：TP1 触发价更近
        必然先成交，按时间序累计 CLOSE 侧成交量，到达 qty_tp1 即分界，其余归
        TP2。CLOSE 侧 = realizedPnl 非零的成交（开仓成交价差为 0）。
        两档合计成交量对不上（碎片化/残缺）时抛 ValueError，由调用方回退合计口径。
        """
        rows = self._req("GET", "/fapi/v1/userTrades", {
            "symbol": symbol, "startTime": since_ms, "limit": 1000,
        })
        closes = sorted(
            (r for r in rows if float(r.get("realizedPnl") or 0) != 0),
            key=lambda r: r.get("time") or 0,
        )
        total_qty = sum(float(r.get("qty") or 0) for r in closes)
        if abs(total_qty - (qty_tp1 + qty_tp2)) > max(qty_tp1 + qty_tp2, 1) * 0.01:
            raise ValueError(
                f"CLOSE 成交量 {total_qty} 与两档 {qty_tp1}+{qty_tp2} 对不上")
        pnl1 = pnl2 = 0.0
        acc = 0.0
        for r in closes:
            q, p = float(r.get("qty") or 0), float(r.get("realizedPnl") or 0)
            if acc < qty_tp1:  # TP1 分界之前的时间序成交归 TP1
                pnl1 += p
            else:
                pnl2 += p
            acc += q
        return round(pnl1, 6), round(pnl2, 6)

    @staticmethod
    def sum_income(rows: list, keep: set) -> float:
        return sum(float(r.get("income") or 0) for r in rows if r.get("incomeType") in keep)
