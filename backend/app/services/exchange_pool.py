"""多交易所 K 线获取（故障转移）

主链路币安，失败（限流 429 / IP 封禁 418 / 网络错误）时自动切换
欧易(OKX) → Bitget，各交易所 K 线统一归一化为币安行格式：

    [open_time_ms, open, high, low, close, base_volume, close_time, quote_volume]

- 同一 symbol+interval 在同一 K 线周期内命中本地缓存，不请求交易所
- 单次请求失败即切换下一交易所（不做退避重试，故障转移本身就是重试）
- 418/429 的 Retry-After 会记录为该交易所的"冷却期"，期间优先尝试其他交易所
- 所有交易所均失败时：allow_stale=True 则返回最近一份旧缓存（供前端展示），
  否则抛出 AllExchangesFailed（summary 含各交易所失败原因与解禁时长）
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.scan import KlineCache
from app.services.binance_client import _INTERVAL_MINUTES

logger = logging.getLogger(__name__)

_OKX_BASE_URL = "https://www.okx.com"
_BITGET_BASE_URL = "https://api.bitget.com"

# 网络错误等无 Retry-After 时的默认冷却秒数
_DEFAULT_COOLDOWN = 30

_LABELS = {"binance": "币安", "okx": "欧易OKX", "bitget": "Bitget"}

# interval → 各交易所周期字符串（未收录的周期该交易所不支持，自动跳过）
_OKX_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "1w": "1W", "1M": "1M",
}
_BITGET_GRANULARITY = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "3d": "3D", "1w": "1W", "1M": "1M",
}

# ── 运行时状态（进程内共享，线程安全依赖 GIL 原子赋值）──
_ban_until: dict[str, float] = {}   # exchange → 冷却截止时间戳
_last_error: dict[str, str] = {}    # exchange → 最近一次失败原因

_clients: dict[str, httpx.Client] = {}


class ExchangeError(Exception):
    """单交易所请求失败"""


class AllExchangesFailed(Exception):
    """所有交易所均失败，summary 为可读的失败汇总"""

    def __init__(self, summary: str):
        self.summary = summary
        super().__init__(summary)


def _get_client(name: str) -> httpx.Client:
    if name not in _clients:
        base = {
            "binance": settings.BINANCE_FUTURES_URL,
            "okx": _OKX_BASE_URL,
            "bitget": _BITGET_BASE_URL,
        }[name]
        _clients[name] = httpx.Client(
            base_url=base,
            timeout=settings.BINANCE_TIMEOUT,
            headers={"User-Agent": "trading-workbench/1.0"},
        )
    return _clients[name]


def _parse_retry_after(resp: httpx.Response) -> float:
    try:
        return max(float(resp.headers.get("retry-after", 60)), 1.0)
    except (TypeError, ValueError):
        return 60.0


def _cooldown(name: str, seconds: float, reason: str) -> None:
    _ban_until[name] = time.time() + seconds
    _last_error[name] = reason


# ── 各交易所 K 线拉取（统一返回 [ts, o, h, l, c, base_vol, close_time, quote_vol]）──


def _raise_if_rate_limited(resp: httpx.Response, name: str = "binance") -> None:
    """418/429 统一处理：记录冷却期并抛出 ExchangeError"""
    if resp.status_code == 418:
        wait = _parse_retry_after(resp)
        _cooldown(name, wait, f"IP 封禁，约 {wait:.0f}s 后解禁")
        raise ExchangeError(f"IP 封禁，约 {wait:.0f}s 后解禁")
    if resp.status_code == 429:
        wait = _parse_retry_after(resp)
        _cooldown(name, wait, f"限流，约 {wait:.0f}s 后恢复")
        raise ExchangeError(f"限流，约 {wait:.0f}s 后恢复")


def _fetch_binance(symbol: str, interval: str, limit: int) -> list[list]:
    resp = _get_client("binance").get(
        "/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
    )
    _raise_if_rate_limited(resp)
    if resp.status_code >= 400:
        raise ExchangeError(f"HTTP {resp.status_code}")
    rows = resp.json()
    return [
        [float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
         float(r[5]), float(r[6]), float(r[7])]
        for r in rows
    ]


def _okx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP"""
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote):
            return f"{symbol[: -len(quote)]}-{quote}-SWAP"
    raise ExchangeError(f"无法转换合约代码 {symbol}")


def _fetch_okx(symbol: str, interval: str, limit: int) -> list[list]:
    bar = _OKX_BAR.get(interval)
    if bar is None:
        raise ExchangeError(f"不支持周期 {interval}")
    resp = _get_client("okx").get(
        "/api/v5/market/candles",
        params={"instId": _okx_symbol(symbol), "bar": bar, "limit": min(limit, 300)},
    )
    if resp.status_code != 200:
        raise ExchangeError(f"HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "0":
        raise ExchangeError(f"code={body.get('code')} {str(body.get('msg'))[:80]}")
    # 行格式 [ts, o, h, l, c, vol(张), volCcy(币), volCcyQuote(计价), confirm]，倒序返回
    rows = [
        [float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
         float(r[6]), 0.0, float(r[7])]
        for r in body.get("data", [])
    ]
    rows.sort(key=lambda r: r[0])
    return rows


def _fetch_bitget(symbol: str, interval: str, limit: int) -> list[list]:
    granularity = _BITGET_GRANULARITY.get(interval)
    if granularity is None:
        raise ExchangeError(f"不支持周期 {interval}")
    resp = _get_client("bitget").get(
        "/api/v2/mix/market/candles",
        params={
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "granularity": granularity,
            "limit": limit,
        },
    )
    if resp.status_code != 200:
        raise ExchangeError(f"HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "00000":
        raise ExchangeError(f"code={body.get('code')} {str(body.get('msg'))[:80]}")
    # 行格式 [ts, o, h, l, c, baseVol, quoteVol, usdtVol]，倒序返回
    rows = [
        [float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
         float(r[5]), 0.0, float(r[6])]
        for r in body.get("data", [])
    ]
    rows.sort(key=lambda r: r[0])
    return rows


_FETCHERS = {
    "binance": _fetch_binance,
    "okx": _fetch_okx,
    "bitget": _fetch_bitget,
}


# ── USDT 永续合约列表 + 24h 成交额（多链路，供扫描器取币种清单）──

_STABLE_BASES = {"USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP", "PAX"}


def _base_ok(base: str) -> bool:
    """排除稳定币和关键词命中的基础资产"""
    return base not in _STABLE_BASES and not any(
        kw in base for kw in settings.EXCLUDE_KEYWORDS
    )


def _fetch_symbols_binance() -> list[dict]:
    c = _get_client("binance")
    resp = c.get("/fapi/v1/exchangeInfo")
    _raise_if_rate_limited(resp)
    if resp.status_code >= 400:
        raise ExchangeError(f"HTTP {resp.status_code}")

    valid = set()
    for s in resp.json().get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != settings.QUOTE_ASSET:
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if not _base_ok(s.get("baseAsset", "")):
            continue
        valid.add(s["symbol"])

    resp = c.get("/fapi/v1/ticker/24hr")
    _raise_if_rate_limited(resp)
    if resp.status_code >= 400:
        raise ExchangeError(f"ticker HTTP {resp.status_code}")

    out = []
    for t in resp.json():
        sym = t.get("symbol", "")
        if sym not in valid:
            continue
        try:
            vol = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue
        if vol <= 0:
            continue
        out.append({"symbol": sym, "volume_24h": vol, "last_price": price})
    out.sort(key=lambda x: x["volume_24h"], reverse=True)
    return out


def _fetch_symbols_okx() -> list[dict]:
    c = _get_client("okx")
    quote = settings.QUOTE_ASSET
    suffix = f"-{quote}-SWAP"

    resp = c.get("/api/v5/public/instruments", params={"instType": "SWAP"})
    if resp.status_code != 200:
        raise ExchangeError(f"HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "0":
        raise ExchangeError(f"instruments code={body.get('code')}")

    valid = set()
    for s in body.get("data", []):
        inst = s.get("instId", "")
        if s.get("state") != "live" or not inst.endswith(suffix):
            continue
        base = inst[: -len(suffix)]
        if not _base_ok(base):
            continue
        valid.add(inst)

    resp = c.get("/api/v5/market/tickers", params={"instType": "SWAP"})
    if resp.status_code != 200:
        raise ExchangeError(f"tickers HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "0":
        raise ExchangeError(f"tickers code={body.get('code')}")

    out = []
    for t in body.get("data", []):
        inst = t.get("instId", "")
        if inst not in valid:
            continue
        try:
            price = float(t.get("last", 0))
            base_vol = float(t.get("volCcy24h", 0))
        except (TypeError, ValueError):
            continue
        vol = base_vol * price  # OKX 合约 24h 量为基础币数量，换算成计价额
        if vol <= 0:
            continue
        out.append({
            "symbol": inst[: -len(suffix)] + quote,
            "volume_24h": vol,
            "last_price": price,
        })
    out.sort(key=lambda x: x["volume_24h"], reverse=True)
    return out


def _fetch_symbols_bitget() -> list[dict]:
    c = _get_client("bitget")
    quote = settings.QUOTE_ASSET

    resp = c.get("/api/v2/mix/market/tickers", params={"productType": "USDT-FUTURES"})
    if resp.status_code != 200:
        raise ExchangeError(f"HTTP {resp.status_code}")
    body = resp.json()
    if body.get("code") != "00000":
        raise ExchangeError(f"code={body.get('code')} {str(body.get('msg'))[:80]}")

    out = []
    for t in body.get("data", []):
        sym = t.get("symbol", "")
        if not sym.endswith(quote):
            continue
        if not _base_ok(sym[: -len(quote)]):
            continue
        try:
            vol = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPr", 0))
        except (TypeError, ValueError):
            continue
        if vol <= 0:
            continue
        out.append({"symbol": sym, "volume_24h": vol, "last_price": price})
    out.sort(key=lambda x: x["volume_24h"], reverse=True)
    return out


_SYMBOL_FETCHERS = {
    "binance": _fetch_symbols_binance,
    "okx": _fetch_symbols_okx,
    "bitget": _fetch_symbols_bitget,
}


def _ordered_names() -> list[str]:
    """币安优先；处于冷却期的交易所排到最后（可能已提前恢复，仍作兜底尝试）"""
    now = time.time()
    return sorted(
        _FETCHERS, key=lambda n: 1 if _ban_until.get(n, 0) > now else 0
    )


# ── K 线缓存（自 binance_client 迁移至此，所有交易所共用）──


def _calc_kline_hour(interval: str) -> datetime:
    """当前 K 线周期起点（UTC 对齐整点），同一周期内缓存复用"""
    minutes = _INTERVAL_MINUTES.get(interval, 60)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    total_minutes = now.hour * 60 + now.minute
    aligned = (total_minutes // minutes) * minutes
    return now.replace(hour=aligned // 60, minute=aligned % 60, second=0, microsecond=0)


def _get_cached_klines(symbol: str, interval: str, kline_hour: datetime) -> Optional[list]:
    sdb = SessionLocal()
    try:
        row = sdb.execute(
            select(KlineCache).where(
                KlineCache.symbol == symbol,
                KlineCache.interval == interval,
                KlineCache.kline_hour == kline_hour,
            )
        ).scalars().first()
        return row.klines if row else None
    finally:
        sdb.close()


def _get_latest_cached_klines(symbol: str, interval: str) -> Optional[list]:
    """最近一份缓存（不限周期），全交易所失败时兜底展示用"""
    sdb = SessionLocal()
    try:
        row = sdb.execute(
            select(KlineCache)
            .where(KlineCache.symbol == symbol, KlineCache.interval == interval)
            .order_by(KlineCache.kline_hour.desc())
        ).scalars().first()
        return row.klines if row else None
    finally:
        sdb.close()


def _upsert_kline_cache(symbol: str, interval: str, kline_hour: datetime, klines: list) -> None:
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
            sdb.add(KlineCache(
                symbol=symbol, interval=interval,
                kline_hour=kline_hour, klines=klines,
            ))
        sdb.commit()
    except Exception:
        sdb.rollback()
        raise
    finally:
        sdb.close()


# ── 对外入口 ──


class ExchangePool:
    """多交易所 K 线获取器（无状态，可全局共享）"""

    def get_klines(
        self, symbol: str, interval: str = "1h", limit: int = 240,
        allow_stale: bool = False,
    ) -> list[list]:
        kline_hour = _calc_kline_hour(interval)

        # 1. 查缓存
        try:
            cached = _get_cached_klines(symbol, interval, kline_hour)
            if cached is not None:
                return cached
        except Exception as e:
            logger.warning("K线缓存查询失败（降级直连交易所）: %s", e)

        # 2. 依次尝试各交易所
        errors: list[str] = []
        for name in _ordered_names():
            try:
                klines = _FETCHERS[name](symbol, interval, limit)
            except ExchangeError as e:
                errors.append(f"{_LABELS[name]}: {e}")
                continue
            except Exception as e:
                _cooldown(name, _DEFAULT_COOLDOWN, str(e)[:120])
                errors.append(f"{_LABELS[name]}: {str(e)[:120]}")
                continue
            try:
                _upsert_kline_cache(symbol, interval, kline_hour, klines)
            except Exception as e:
                logger.warning("K线缓存写入失败（忽略）: %s", e)
            _last_error.pop(name, None)
            return klines

        # 3. 全部失败：按需降级返回旧缓存
        if allow_stale:
            stale = _get_latest_cached_klines(symbol, interval)
            if stale is not None:
                logger.warning(
                    "所有交易所获取 %s %s 失败，降级返回旧缓存", symbol, interval
                )
                return stale
        raise AllExchangesFailed("；".join(errors) or "未知错误")

    def get_usdt_swap_symbols_with_volume(self) -> list[dict]:
        """获取 USDT 永续合约列表（多链路故障转移）

        返回: [{symbol, volume_24h, last_price}, ...] 按 24h 成交额降序
        """
        errors: list[str] = []
        for name in _ordered_names():
            try:
                return _SYMBOL_FETCHERS[name]()
            except ExchangeError as e:
                errors.append(f"{_LABELS[name]}: {e}")
                continue
            except Exception as e:
                _cooldown(name, _DEFAULT_COOLDOWN, str(e)[:120])
                errors.append(f"{_LABELS[name]}: {str(e)[:120]}")
                continue
        raise AllExchangesFailed("；".join(errors) or "未知错误")


def exchange_status() -> str:
    """当前各交易所状态摘要（运维/报错展示用）"""
    now = time.time()
    parts = []
    for name in _FETCHERS:
        until = _ban_until.get(name, 0)
        if until > now:
            parts.append(f"{_LABELS[name]} 冷却中(约 {int(until - now)}s 后恢复)")
        else:
            parts.append(f"{_LABELS[name]} 可用")
    return "；".join(parts)
