"""市场环境数据源（docs/04 第一梯队①②）：资金费率/持仓量、恐惧贪婪指数、大盘广度

- 全部静默降级：任何失败返回 None，prompt 中显示"不可用"，绝不阻塞 AI 分析
- 进程内 TTL 缓存：funding 10min、F&G 30min、大盘 5min
"""
import logging
import time
from typing import Optional

import httpx
import numpy as np

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


# ── AI 事实包市场环境块（docs/24 A4①）──────────────────────────────────

# 共享 BinanceClient 单例（拉 BTC 背景K线用；懒加载，避免导入期建连）
_kline_client_inst: Optional[object] = None


def get_kline_client():
    """共享 BinanceClient（get_futures_klines 薄封装宿主）；测试可整体替换"""
    global _kline_client_inst
    if _kline_client_inst is None:
        from app.services.binance_client import BinanceClient
        _kline_client_inst = BinanceClient()
    return _kline_client_inst


def _btc_trend_label(klines: list) -> Optional[str]:
    """BTC 4h 趋势标签（EMA21/55 排列）：多头/空头/纠缠；数据不足返回 None。

    与 analyze_htf_background 的纠缠阈值同口径（间距 <0.1% 视为纠缠），
    但不带 ADX 门控与强弱分级——环境摘要只给粗粒度状态。
    """
    from app.services.strategy.ema import calc_ema

    if not klines or len(klines) < 2:
        return None
    closed = klines[:-1]  # 剔除最后一根未收盘（与指标口径一致）
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    f = calc_ema(closes, 21)
    s = calc_ema(closes, 55)
    if f is None or s is None:
        return None
    fn, sn = float(f[-1]), float(s[-1])
    if sn <= 0 or not np.isfinite(fn):
        return None
    if abs(fn - sn) / sn * 100 < 0.1:
        return "纠缠"
    return "多头" if fn > sn else "空头"


def _btc_market_line(client) -> Optional[str]:
    """BTC 状态行：4h EMA21/55 排列 + 1h 近3根已收盘累计涨跌幅。

    4h 取 60 根（EMA55 需 ≥55 根已收盘数据，50 根不够成形）；
    1h 近3根累计跌幅超过 BTC_CRASH_PAUSE_PCT 时追加急跌警示（0=关闭）。
    两段数据任一失败只降级该段，全失败返回 None。
    """
    # 4h 趋势（EMA 排列）
    trend = None
    try:
        k4 = client.get_futures_klines("BTCUSDT", "4h", 60)
        trend = _btc_trend_label(k4)
    except Exception as e:
        logger.debug("市场环境-BTC 4h K线获取失败: %s", e)

    # 1h 近 3 根已收盘累计涨跌幅（c[-1]/c[-4]-1，覆盖 3 根涨跌）
    chg = None
    try:
        k1 = client.get_futures_klines("BTCUSDT", "1h", 24)
        if k1 and len(k1) >= 5:
            closed = k1[:-1]
            base = float(closed[-4][4])
            if base > 0:
                chg = (float(closed[-1][4]) / base - 1) * 100
    except Exception as e:
        logger.debug("市场环境-BTC 1h K线获取失败: %s", e)

    parts = []
    if trend:
        parts.append(f"BTC 4h {trend}")
    if chg is not None:
        parts.append(f"1h 近3根 {chg:+.2f}%")
    if not parts:
        return None
    line = "，".join(parts)
    crash_pct = float(settings.BTC_CRASH_PAUSE_PCT)
    if crash_pct > 0 and chg is not None and chg < -crash_pct:
        line += "（BTC 短线急跌，山寨开单需谨慎）"
    return line


def btc_crash_active(client) -> bool:
    """BTC 1h 近3根已收盘累计急跌判定（docs/24 A4② 程序级普跌防护闸门用）。

    跌幅阈值 = BTC_CRASH_PAUSE_PCT（0=关闭恒 False，与提示词警示共用同一开关）；
    K 线查询失败降级为 False（不因环境查询失败阻断开仓）。60s 进程内缓存，
    开仓闸门逐笔调用不重拉 K 线。
    """
    crash_pct = float(settings.BTC_CRASH_PAUSE_PCT)
    if crash_pct <= 0:
        return False
    cached = _cache_get("btc_crash_active")
    if cached is not None:
        return bool(cached)
    active = False
    try:
        k1 = client.get_futures_klines("BTCUSDT", "1h", 24)
        if k1 and len(k1) >= 5:
            closed = k1[:-1]  # 已收盘口径，与 _btc_market_line 一致
            base = float(closed[-4][4])
            if base > 0:
                chg = (float(closed[-1][4]) / base - 1) * 100
                active = chg < -crash_pct
    except Exception as e:
        logger.debug("BTC 急跌判定查询失败（按无急跌处理）: %s", e)
        return False  # 查询失败不缓存、不拦截
    _cache_set("btc_crash_active", active, ttl=60.0)
    return active


def build_market_context(client, symbol: str) -> Optional[str]:
    """AI 事实包市场环境文本块（docs/24 A4①）：本币资金费率 + BTC 状态摘要。

    返回两行左右中文文本（不带标题，调用方拼「【市场环境】」标题注入提示词）；
    费率/BTC 任一环节失败静默降级为跳过对应行，全失败返回 None（不注入）。
    client: BinanceClient 实例（拉 BTC K 线；测试传 fake）；资金费率走
    get_funding（/fapi/v1/premiumIndex，带进程内 TTL 缓存）。
    """
    lines: list[str] = []

    # 1) 本币资金费率：绝对值 >0.1%/8h 视为费率异常（多头/空头拥挤，逆向风险）
    try:
        f = get_funding(symbol)
        if f is not None:
            rate = float(f.get("funding_rate_pct") or 0.0)
            line = f"资金费率 {rate:.4f}%/8h"
            if abs(rate) > 0.1:
                side = "多头拥挤" if rate > 0 else "空头拥挤"
                line += f"（费率异常，{side}，逆向风险）"
            lines.append(line)
    except Exception as e:
        logger.debug("市场环境-资金费率 %s 失败: %s", symbol, e)

    # 2) BTC 状态摘要（4h 趋势 + 1h 近3根涨跌，急跌加警示）
    try:
        btc = _btc_market_line(client)
        if btc:
            lines.append(btc)
    except Exception as e:
        logger.debug("市场环境-BTC状态构建失败: %s", e)

    if not lines:
        return None
    return "\n".join(lines)
