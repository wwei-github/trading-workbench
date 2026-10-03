"""币安 USDT-M 合约交易客户端（docs/06）

- 独立于行情 ExchangePool：凭据走 .env 账号注册表（docs/17），测试网/正式网
  双网并存（testnet.binancefuture.com / 生产基地址，路径一致），账号属性
  testnet 决定基地址（2026-10-03 删环境切换，无全局「当前环境」）
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


# 逐 symbol 的交易规则缓存（进程内，按环境分桶）：stepSize/tickSize/minNotional/minQty
_filters_cache: dict[tuple[bool, str], dict] = {}
# 逐 symbol 的杠杆分档缓存（进程内，按环境分桶）：leverageBracket 响应（MMR 按名义档位取）
_bracket_cache: dict[tuple[bool, str], list] = {}
# 全市场第 1 档风险档位缓存（扫描候选池过滤，24h 刷新）
_risk_tier_cache: dict[str, tuple[float, int]] = {}
_risk_tier_cached_at: float = 0.0
# 下单环境可交易合约集合缓存（扫描宇宙过滤，24h 刷新，按环境分桶）：
# ARX 案例——数据源是正式网（ARXUSDT 存在、AI 分析照常），测试网未上架
# → 下单 -1121 Invalid symbol
_listed_cache: dict[bool, tuple[set, float]] = {}


class BinanceTrader:
    def __init__(self, account: Optional[Union[bool, str]] = None):
        """多账号构造（docs/17 P0；2026-10-03 删环境切换后无「当前环境」概念）：
        - str（账号 key，如 'live-2'/'testnet-1'）：用该账号凭据；注册表里不存在
          （.env 改配置后旧记录引用）→ 回落测试网优先兜底并告警
        - bool：兼容旧调用——True=testnet-1（未配置回落 live-1）；False=live-1
          （未配置回落 testnet-1）
        - None：测试网优先兜底（实盘安全基线：无明确指向的调用绝不默认落正式网）
        self.testnet 保留为账号属性，既有按环境分桶的缓存与调用方判断不受影响。"""
        acc = self._resolve_account(account)
        if acc:
            self.account_key, self.testnet = acc.key, acc.testnet
            self.key, self.secret = acc.api_key, acc.api_secret
            self.base = acc.base_url
        else:
            # 注册表为空（.env 完全没配 key）：沿用旧路径，configured=False 兜底
            # （None=无明确指向 → 测试网优先；显式 False=正式网）
            self.testnet = True if account is None else bool(account)
            self.account_key = "testnet-1" if self.testnet else "live-1"
            if self.testnet:
                # 测试网 key 未单独配置时回落正式网 key（兼容单组 key 部署）
                self.key = settings.BINANCE_TESTNET_API_KEY or settings.BINANCE_API_KEY
                self.secret = settings.BINANCE_TESTNET_SECRET_KEY or settings.BINANCE_SECRET_KEY
                self.base = _TEST_BASE
            else:
                self.key = settings.BINANCE_API_KEY
                self.secret = settings.BINANCE_SECRET_KEY
                self.base = _PROD_BASE
        self._client = httpx.Client(timeout=15)
        logger.info(
            "BinanceTrader: %s/%s (%s)", self.account_key, "testnet" if self.testnet else "正式网", self.base
        )

    def _resolve_account(self, account) -> Optional["Account"]:
        from app.services import account_registry as reg

        if isinstance(account, str):
            acc = reg.get_account(account)
            if acc is not None:
                return acc
            logger.warning("账号 %s 不在注册表中（.env 已移除？），回落测试网优先兜底", account)
            account = None  # 落到下方兜底链
        if account is True:
            # 明确要测试网（未配置测试网 key 时回落 live-1，历史行为）
            pool = reg.testnet_accounts() or reg.live_accounts()
            return pool[0] if pool else None
        if account is False:
            # 明确要正式网：live 账号第一个（current_account_key 主账号语义
            # 仅前端展示用，后端一律显式传账号 key）
            pool = reg.live_accounts() or reg.testnet_accounts()
            return pool[0] if pool else None
        # None（无参兜底）：测试网优先（实盘安全基线——无明确指向的调用
        # 绝不默认落正式网）
        pool = reg.testnet_accounts() or reg.live_accounts()
        return pool[0] if pool else None

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
        """非零持仓：{symbol: {amt, initial_margin, unrealized_pnl}}"""
        out: dict[str, dict] = {}
        for p in self._req("GET", "/fapi/v2/positionRisk"):
            amt = float(p.get("positionAmt") or 0)
            if amt != 0:
                out[p["symbol"]] = {
                    "amt": amt,
                    "initial_margin": float(p.get("initialMargin") or 0),
                    "entry_price": float(p.get("entryPrice") or 0),
                    "unrealized_pnl": float(p.get("unRealizedProfit") or 0),
                }
        return out

    # ── 交易规则与精度 ────────────────────────────────────────

    def filters(self, symbol: str) -> dict:
        """交易规则（进程内缓存，按环境分桶）：{step, tick, min_qty, min_notional}"""
        ck = (self.testnet, symbol)
        if ck in _filters_cache:
            return _filters_cache[ck]
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
            _filters_cache[ck] = f
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
            # testnet 对未上架合约返回空体 {}（主网才返回 -1121），错误信息带上可能性提示
            raise BinanceTradeError(
                f"ticker/price 无数据（可能未上架该合约，如 testnet）: {str(r)[:100]}"
            )
        return float(r["price"])

    def book_ticker(self, symbol: str) -> tuple[float, float]:
        """最优买卖价（GET /fapi/v1/bookTicker）：返回 (bid, ask)。
        开仓前盘口价差预检用（docs/24 A4'），查询异常由调用方兜底放行"""
        r = self._req("GET", "/fapi/v1/bookTicker", {"symbol": symbol})
        return float(r["bidPrice"]), float(r["askPrice"])

    def bracket_info(self, symbol: str, notional: float) -> Optional[tuple]:
        """名义价值所在档位的 (MMR, 档位最大杠杆 initialLeverage, 档位名义上限 notionalCap)。

        /fapi/v1/leverageBracket。失败返回 None（调用方用保守值兜底）。
        -2027 修复：下单名义落在某档位时，设置的杠杆超过该档 initialLeverage
        会被拒单（Exceeded the maximum allowable position at current leverage）；
        名义超最大档 notionalCap 同样拒。
        """
        try:
            ck = (self.testnet, symbol)
            rows = _bracket_cache.get(ck)
            if rows is None:
                rows = self._req("GET", "/fapi/v1/leverageBracket", {"symbol": symbol})
                _bracket_cache[ck] = rows
            brackets = rows[0]["brackets"]  # 按档位升序
            chosen = brackets[-1]  # notional 超所有档时落最大档（调用方按 cap 拦截）
            for b in brackets:
                if notional <= float(b["notionalCap"]):
                    chosen = b
                    break
            return (
                float(chosen["maintMarginRatio"]),
                int(chosen["initialLeverage"]),
                float(chosen["notionalCap"]),
            )
        except Exception as e:
            logger.warning("bracket_info(%s) 获取失败: %s", symbol, e)
            return None

    def bracket_mmr(self, symbol: str, notional: Optional[float] = None) -> Optional[float]:
        """该币种的维持保证金率（MMR），按名义价值取对应档位；notional=None 取第 1 档。

        各币第 1 档 MMR 差异极大（BTC 0.4%、主流币 0.5%、小币 1.5%-2.5%），
        写死 0.5% 会放行「强平先于止损」的单子（4USDT 案例：止损 4.2% 通过
        4.5% 闸门，但实际 MMR=1.5% → 跌 3.5% 即被强平，SL 形同虚设）。
        """
        info = self.bracket_info(symbol, notional if notional is not None else 0.0)
        return info[0] if info else None

    def market_slippage_estimate(self, symbol: str, side: str, qty: float) -> Optional[float]:
        """市价单滑点实测（GRIFFAIN 案例闸门，2026-09-18）：按当前订单簿把本单
        数量逐层吃掉，加权均价对最优价的偏离即为预期滑点（比例，0.003=0.3%）。

        订单簿薄的小币（吃单滑点 0.637%）会撑宽实际止损距离、放大风险超预算；
        成交额门槛拦不住（拉盘小币 24h 成交额可过 300 万，簿上照样空）。开仓前
        用本单真实数量实测——不是泛泛的盘口深度，是这单要付出的真实成本。
        返回 None = 簿数据异常（调用方决定放行与否，不阻断）。
        """
        try:
            book = self._req("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": 50})
            # BUY 吃 asks（卖单簿），SELL 吃 bids（买单簿）；[[price, qty], ...] 字符串对
            levels = book.get("asks" if side.upper() == "BUY" else "bids") or []
            if not levels:
                return None
            remaining, cost, best = float(qty), 0.0, None
            for p_str, q_str in levels[:50]:
                p, q = float(p_str), float(q_str)
                if best is None:
                    best = p
                take = min(remaining, q)
                cost += take * p
                remaining -= take
                if remaining <= 0:
                    break
            if best is None or best <= 0:
                return None
            if remaining > 0:  # 簿深 50 档吃不完：按全部可见深度算均价（必然高滑点，如实上报）
                cost += remaining * float(levels[-1][0])
            avg_price = cost / float(qty)
            return avg_price / best - 1
        except Exception as e:
            logger.warning("market_slippage_estimate(%s) 失败: %s", symbol, e)
            return None

    def risk_tier_all(self) -> dict[str, tuple[float, int]]:
        """全市场第 1 档风险档位（扫描候选池过滤用，scanner 调用）：{symbol: (首档MMR, 首档最大杠杆)}。

        /fapi/v1/leverageBracket 不带 symbol 一次拉全市场，进程内缓存 24h——
        交易所对各币的风险标注（4USDT 首档 MMR 1.5%/杠杆 15×，主流币 0.4%~0.5%）。
        失败返回空 dict（调用方放行，不阻断扫描）
        """
        global _risk_tier_cache, _risk_tier_cached_at
        now = time.time()
        if _risk_tier_cache and now - _risk_tier_cached_at < 86400:
            return _risk_tier_cache
        try:
            rows = self._req("GET", "/fapi/v1/leverageBracket", {})
            out: dict[str, tuple[float, int]] = {}
            for r in rows:
                brackets = r.get("brackets") or []
                if not brackets:
                    continue
                first = brackets[0]  # 按档位升序，[0] 即第 1 档
                try:
                    out[str(r["symbol"])] = (
                        float(first["maintMarginRatio"]),
                        int(first["initialLeverage"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            _risk_tier_cache = out
            _risk_tier_cached_at = now
            return out
        except Exception as e:
            logger.warning("risk_tier_all 拉取失败（扫描侧放行）: %s", e)
            return _risk_tier_cache or {}

    def listed_symbols(self) -> set[str]:
        """当前实例环境可交易的合约集合（扫描宇宙过滤）。

        exchangeInfo 一次拉全市场，进程内缓存 24h（按环境分桶：运行时切环境
        后新实例走新 base，缓存互不污染）。失败返回空 set（调用方放行，不阻断扫描）。
        """
        now = time.time()
        cached = _listed_cache.get(self.testnet)
        if cached and now - cached[1] < 86400:
            return cached[0]
        try:
            r = self._req("GET", "/fapi/v1/exchangeInfo", {})
            out = {
                s["symbol"]
                for s in r.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            }
            if out:  # 拉到空列表视为异常，保留旧缓存
                _listed_cache[self.testnet] = (out, now)
            return _listed_cache.get(self.testnet, (out, 0))[0]
        except Exception as e:
            logger.warning("listed_symbols 拉取失败（扫描侧放行）: %s", e)
            return (cached or (set(), 0))[0]

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
        """止损单（algo）：触发后市价全平剩余仓位（closePosition），返回含 algoId

        workingType=MARK_PRICE：用标记价格触发。标记价格跟随多所指数价格加权利率，
        不受单一订单簿打飞影响（测试网薄订单簿曾把 last price 打飞 30%误触发止损，
        成交于 100 vs 真实市价 77）。
        """
        return self._req("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "STOP_MARKET", "triggerPrice": self._dec_str(stop_price),
            "closePosition": "true", "workingType": "MARK_PRICE",
        })

    def algo_order_detail(self, symbol: str, algo_id: int) -> dict:
        """查询单个 algo 条件单详情（Algo Order API 无历史列表端点，只能按单号逐个查）。

        关键字段：algoStatus（NEW/FINISHED/EXPIRED/TRIGGERED...）与 actualOrderId——
        条件单触发后生成的市价子单号精确回填在此（未触发为空串）。子单在
        /fapi/v1/allOrders 里不带归属字段，本字段是"成交是否来自系统挂单链"
        的唯一可靠判据（2026-10-02 测试网实测：TP1/SL 两条链均验证）。"""
        return self._req("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": int(algo_id)})

    def take_profit_reduce(self, symbol: str, side: str, qty: float, stop_price: float) -> dict:
        """止盈单（algo）：触发后市价减仓指定数量（reduceOnly），返回含 algoId

        止盈保留默认 CONTRACT_PRICE（最新成交价）：到止盈位说明行情真到了，
        用 last price 触发更贴近实际可成交价格，避免标记价格滞后于行情尖峰。
        """
        return self._req("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "TAKE_PROFIT_MARKET", "triggerPrice": self._dec_str(stop_price),
            "quantity": self._dec_str(qty), "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
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

    def transfer_rows(self, since_ms: int) -> list:
        """合约账户级资金划转明细（不带 symbol：income TRANSFER 是账户级流水）。
        income 正=转入（现货→合约），负=转出（合约→现货）。"""
        return self._req("GET", "/fapi/v1/income", {
            "incomeType": "TRANSFER", "startTime": since_ms, "limit": 1000,
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
        两档合计成交量对不上（碎片化/残缺）时抛 ValueError，由调用方回退合计口径；
        qty_tp2=0（无 TP2 单）跳过总量校验，仅按分界取 TP1 档盈亏。
        """
        rows = self._req("GET", "/fapi/v1/userTrades", {
            "symbol": symbol, "startTime": since_ms, "limit": 1000,
        })
        closes = sorted(
            (r for r in rows if float(r.get("realizedPnl") or 0) != 0),
            key=lambda r: r.get("time") or 0,
        )
        # 无 TP2（qty_tp2=0）的单：CLOSE 侧 = TP1 档 + SL/追踪档，总量必然
        # 大于 qty_tp1，不能做两档总量校验（否则必抛 ValueError）——只按
        # 时间序累计到 qty_tp1 分界取 TP1 档盈亏，pnl2 无意义返回 0
        if qty_tp2 <= 0:
            pnl1 = 0.0
            acc = 0.0
            for r in closes:
                q, p = float(r.get("qty") or 0), float(r.get("realizedPnl") or 0)
                if acc < qty_tp1:
                    pnl1 += p
                acc += q
            if acc <= 0:
                raise ValueError("CLOSE 侧无成交记录，无法拆分 TP1 档盈亏")
            return round(pnl1, 6), 0.0
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
