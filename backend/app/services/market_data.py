"""市场环境数据源（docs/04 第一梯队①②）：资金费率/持仓量、恐惧贪婪指数、大盘广度

- 全部静默降级：任何失败返回 None，prompt 中显示"不可用"，绝不阻塞 AI 分析
- 进程内 TTL 缓存：funding 10min、F&G 30min、大盘 5min
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.ops import TaskRecord

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


# ── 扫描尾窗（2026-10-11-4）：近收盘未收盘根视作已收盘 ──
# 定时扫描 beat 定在每小时第 SCAN_BEAT_MINUTE 分（celery_app.hourly-scan），此时
# 本周期K线已走完 (60-宽度)/60 分钟、形态基本定型——把它当作已收盘根参与筛选，
# 信号提前数分钟捕捉。
# 尾窗宽度与该分钟数单一来源派生（2026-10-12）：二者仅靠注释耦合时，改 beat
# 忘改此处会让 in_forming_tail 静默返回 False（筛选退回「看上一根已收盘根」，
# 不报错）——派生 + 启动校验让配置漂移在结构上不可能发生。
# 宽度 0 = 尾窗关闭（2026-10-12 拍板：beat 改整点 :00，新周期刚开，无「近收盘
# 未收盘根」可言）：in_forming_tail 恒 False，screen_klines 的尾窗分支不再触发。
# 整点取数竞态（新周期根尚未出现在响应里、末根实为刚收盘根）由「末根已收盘
# 即视作信号根」兜底（2026-10-13，screen_klines 第二触发条件）：该情形末根
# 正是应评的信号根，不再被当未收盘剔除、错位到前一根且该根被漏评（STXUSDT
# 09:00 吞没漏评个案）。机制与派生校验保留，改回分钟数即自动恢复
# 宽度 = beat 时刻距收盘的剩余分钟数 = 60 - SCAN_BEAT_MINUTE；beat 整点（0）
# 是唯一例外：新周期刚开，「近收盘」无从定义，而派生式会误得 60（=恒在尾窗），
# 故显式归零 = 尾窗关闭
FORMING_TAIL_MINUTES = (
    0 if settings.SCAN_BEAT_MINUTE == 0 else 60 - settings.SCAN_BEAT_MINUTE
)
if not 0 <= FORMING_TAIL_MINUTES < 60:
    raise ValueError(
        f"SCAN_BEAT_MINUTE={settings.SCAN_BEAT_MINUTE} 非法："
        f"尾窗宽度须落在 [0,60) 分钟（当前 {FORMING_TAIL_MINUTES} 分钟）"
    )


def in_forming_tail(interval: str, now: Optional[datetime] = None) -> bool:
    """当前时刻是否处于未收盘K线的尾部窗口（距收盘 ≤ FORMING_TAIL_MINUTES 分钟）

    interval 周期内剩余时间 ≤ 阈值即 True：1h 在每小时的 :55~:00、4h 在每个
    4h 周期的最后 5 分钟。now 可注入（测试/回放）。
    """
    from app.services.binance_client import _INTERVAL_MINUTES
    minutes = _INTERVAL_MINUTES.get(interval, 60)
    now = now or datetime.now(timezone.utc)
    total = now.hour * 60 + now.minute + now.second / 60.0
    into = total % minutes
    return (minutes - into) <= FORMING_TAIL_MINUTES


def _last_candle_closed(klines: list, interval: str, now: datetime) -> bool:
    """末根是否已走完（开盘时刻 + 周期 ≤ now）

    正常取数末根 = 当前周期未走完根（False）；整点取数竞态下新周期根尚未
    出现在响应里、末根实为上一根已收盘根（True——须视作信号根）。now 无
    时区信息按 UTC 理解（生产恒传 aware 的 datetime.now(timezone.utc)；
    统一口径避免裸 datetime 落到本机时区、判定随部署机漂移）。
    """
    from app.services.binance_client import _INTERVAL_MINUTES
    minutes = _INTERVAL_MINUTES.get(interval, 60)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return float(klines[-1][0]) + minutes * 60_000 <= now.timestamp() * 1000


def screen_klines(klines: list, interval: str, now: Optional[datetime] = None) -> list:
    """扫描筛选口径视图：末根已是已收盘根（或尾窗内近收盘根）时视作信号根。

    全管线既有约定为「[-1] 未收盘剔除、[-2] 信号已收盘根」（technical/
    ema/trigger 各模块统一 [:-1] 剔尾）。为不改任何下游逻辑，此处复制一
    份幽灵尾根追加到末尾：[-1]=幽灵（被管线剔除），[-2]=原末根（视作
    信号根，OHLCV 为扫描时刻快照）。两者皆不满足则原样返回。

    触发幽灵尾根的情形（或）：
    1. 尾窗内（in_forming_tail，2026-10-11-4）：近收盘未收盘根提前视作
       信号根（beat 回退整点后派生宽度 0，此路恒不触发，机制保留）；
    2. 末根已收盘（2026-10-13 整点竞态修复）：整点扫描取数竞态下新周期
       根尚未出现在响应里、末根实为刚收盘根——不补幽灵会把它当未收盘
       剔除，信号根错位到前一根且该根被漏评（STXUSDT 09:00 吞没漏评个
       案）；已收盘末根视作信号根与新鲜取数口径一致（新鲜数据末根=新周
       期根，剔尾后信号根是同一根）。
    """
    if not klines:
        return klines
    now = now or datetime.now(timezone.utc)
    if in_forming_tail(interval, now) or _last_candle_closed(klines, interval, now):
        return klines + [list(klines[-1])]
    return klines


# 4h 状态快照时效契约（2026-10-21 第二阶段 T1）：分析落库时记录状态对应的
# 最后一根已收盘 4h 的收盘时刻；开仓前复核陈旧度，跨周期禁开。
HTF_INTERVAL_MS = 4 * 3600 * 1000
HTF_MAX_STALE_SECONDS = 4 * 3600 + 900   # 4h 周期 + 15min 数据落地容差


def htf_snapshot_time(htf_klines: list, now: Optional[datetime] = None) -> Optional[datetime]:
    """4h 状态快照时刻 = 参与状态计算的最后一根 K 线收盘时间（UTC naive）。

    与 htf_core 同口径用 [:-1] 剔除尾根（screen_klines 可能在末尾追加幽灵根）。
    now 传 aware/naive 均可，用于封顶未收盘/尾窗场景；None=不封顶。
    返回 None=数据不足，调用方按缺数处理。
    """
    if not htf_klines or len(htf_klines) < 2:
        return None
    try:
        close_ms = float(htf_klines[-2][0]) + HTF_INTERVAL_MS
    except (IndexError, TypeError, ValueError):
        return None
    try:
        as_of = datetime.fromtimestamp(
            close_ms / 1000, tz=timezone.utc
        ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None
    if now is not None:
        now_utc = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
        as_of = min(as_of, now_utc)
    return as_of


def htf_open_block_reason(htf_degraded: bool, htf_as_of: Optional[datetime],
                          now: Optional[datetime] = None) -> Optional[str]:
    """开仓前 4h 资格复核：返回拦截原因，None=通过。

    缺数（分析时降级）一律禁开；状态时刻距今超过 4h15m 禁开（信号已跨 4h
    周期，方向上可能已反转）。htf_as_of 为 None 的历史行（本列上线前）保持
    兼容放行——新分析的缺数已由 htf_degraded 覆盖。
    """
    if htf_degraded:
        return "4h 数据缺失/降级，禁止新开仓（结论缺少大周期背景）"
    if htf_as_of is None:
        return None
    now_utc = now or datetime.utcnow()
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        age = (now_utc - htf_as_of).total_seconds()
    except TypeError:
        return "4h 状态时间戳异常，禁止新开仓"
    if age > HTF_MAX_STALE_SECONDS:
        return (f"4h 状态已陈旧（{age / 3600:.1f}h > "
                f"{HTF_MAX_STALE_SECONDS / 3600:.1f}h），禁止新开仓")
    return None


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
    """大盘广度：BTC/ETH 的 EMA 形态 + 24h 涨跌幅 + 现价。

    K 线走 ExchangePool（同周期内命中缓存，零交易所 IO）。
    返回 {"BTCUSDT": {"ema_state", "ema_label", "change_24h_pct", "price"}, ...} 或 None
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
            # 现价（2026-10-13 顶部条展示）：末根未收盘K线收盘≈现价，
            # 与 24h 口径共用同一批 K 线，零额外交易所请求
            "price": float(klines[-1][4]) if klines else None,
        }
    if not out:
        return None
    _cache_set("breadth", out, 300)
    return out


# ── 技术结构快照（Header 大盘条，2026-10-13）────────────────────────────

# 结构检测任务写库任务的展示名（与 app.tasks.structure_watch._TASK_NAME 对齐；
# 两模块各自持有字面量，防跨模块 import 私有常量造成耦合）
_STRUCTURE_TASK_NAME = "技术结构检测"

_TREND_LABELS = {"up": "上涨结构", "down": "下降结构", "range": "区间震荡", "unknown": "结构未定"}

# 异常码 → 中文短标签（仅展示用；未知码原样透出便于排查）
_ANOMALY_LABELS = {
    "break_down": "结构破位", "break_up": "结构破位",
    "conflict": "周期冲突", "fetch": "取数失败",
    "data_1h_short": "1h数据不足", "data_4h_short": "4h数据不足",
    "stale_1h": "1h数据陈旧", "stale_4h": "4h数据陈旧",
    "gap_1h": "1h缺口", "gap_4h": "4h缺口",
}


def get_structure_snapshot() -> Optional[dict]:
    """最新一轮「技术结构检测」的结构快照（Header 大盘条结构 token 数据源）。

    读 structure_watch 任务写入的 TaskRecord.detail.symbols，映射为前端展示字段；
    无记录 / 解析失败 / 查询异常 → None（接口静默降级，不阻塞 Header）。
    返回 {"BTCUSDT": {"trend","trend_label","valid","broken","support",
    "support_dist_pct","resistance","resistance_dist_pct","htf_label",
    "anomaly_labels","updated_at"}, ...}
    """
    try:
        db = SessionLocal()
        try:
            rec = db.execute(
                select(TaskRecord)
                .where(TaskRecord.task_name == _STRUCTURE_TASK_NAME,
                       TaskRecord.status == "completed")
                .order_by(TaskRecord.started_at.desc())
                .limit(1)
            ).scalars().first()
        finally:
            db.close()
        if rec is None:
            return None
        syms = (rec.detail or {}).get("symbols") or {}
        ts = rec.finished_at or rec.started_at
        updated_at = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None

        out: dict = {}
        for sym, d in syms.items():
            st = d.get("structure") or {}
            kl = d.get("key_levels") or {}
            htf = d.get("htf") or {}
            trend = st.get("trend") or "unknown"
            valid = st.get("valid")
            out[sym] = {
                "trend": trend,
                "trend_label": _TREND_LABELS.get(trend, "结构未定"),
                "valid": valid,
                "broken": valid is False,
                "support": kl.get("support"),
                "support_dist_pct": kl.get("support_dist_pct"),
                "resistance": kl.get("resistance"),
                "resistance_dist_pct": kl.get("resistance_dist_pct"),
                "htf_label": htf.get("state_label"),
                "anomaly_labels": [_ANOMALY_LABELS.get(c, c) for c in (d.get("anomalies") or [])],
                "updated_at": updated_at,
            }
        return out or None
    except Exception as e:
        logger.debug("get_structure_snapshot 失败: %s", e)
        return None


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

    与 technical.detect_tangle 的纠缠阈值同口径（间距 <TANGLE_GAP_PCT 视为纠缠），
    但不带 ADX 门控与强弱分级——环境摘要只给粗粒度状态。
    """
    from app.services.strategy.ema import calc_ema
    from app.services.strategy.technical import TANGLE_GAP_PCT

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
    if abs(fn - sn) / sn < TANGLE_GAP_PCT:
        return "纠缠"
    return "多头" if fn > sn else "空头"


def _btc_market_line(client, crash_pct: Optional[float] = None) -> Optional[str]:
    """BTC 状态行：4h EMA21/55 排列 + 1h 近3根已收盘累计涨跌幅。

    4h 取 60 根（EMA55 需 ≥55 根已收盘数据，50 根不够成形）；
    1h 近3根累计跌幅超过急跌阈值时追加急跌警示（0=关闭）。
    crash_pct（2026-10-02 上页面）：阈值 %，system_config.btc_crash_pause_pct
    覆盖时由调用方传入；None 回落 env 默认。
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
    _crash_pct = float(settings.BTC_CRASH_PAUSE_PCT) if crash_pct is None else float(crash_pct)
    if _crash_pct > 0 and chg is not None and chg < -_crash_pct:
        line += "（BTC 短线急跌，山寨开单需谨慎）"
    return line


# ── BTC-beta（docs/13 #1：组合热度 β 加权）────────────────────────────────

_BETA_BARS = 24 * 30 + 25   # 1h K：30 个日采样点 + 回看锚点 + 缓冲
_BETA_TTL = 24 * 3600.0     # β 是慢变量：24h 进程内缓存


def get_beta(client, symbol: str) -> Optional[float]:
    """币种对 BTC 的日收益回归 β（docs/13 #1 组合热度加权用）。

    口径：1h K 最近 30 个日采样收盘（每 24 根取一）算日收益序列，
    β = cov(币, BTC) / var(BTC)；样本不足 / BTC 收益零方差 / 任一查询失败
    → None（调用方按 1.0 保守处理）。BTCUSDT 恒 1.0（自身）。
    24h 进程内缓存（β 慢变量，开仓闸门逐笔调用不重拉 K 线）。
    """
    if symbol == "BTCUSDT":
        return 1.0
    key = f"beta:{symbol}"
    cached = _cache_get(key)
    if cached is not None:
        return cached if cached != 0 else None  # 0 哨兵 = 缓存的 None
    try:
        sym_k = client.get_futures_klines(symbol, "1h", _BETA_BARS)
        btc_k = client.get_futures_klines("BTCUSDT", "1h", _BETA_BARS)
    except Exception as e:
        logger.debug("get_beta %s K线获取失败: %s", symbol, e)
        _cache_set(key, 0, _BETA_TTL)
        return None
    beta: Optional[float] = None
    try:
        # 已收盘口径，每 24 根采一个收盘价（对齐 UTC 日界附近，取样误差对回归斜率影响可忽略）
        sym_c = np.array([float(k[4]) for k in sym_k[:-1][::24]], dtype=float)
        btc_c = np.array([float(k[4]) for k in btc_k[:-1][::24]], dtype=float)
        n = min(len(sym_c), len(btc_c))
        if n >= 15:  # 至少 15 个日收益样本（14 个收益差）
            sym_c, btc_c = sym_c[-n:], btc_c[-n:]
            sym_r = np.diff(sym_c) / sym_c[:-1]
            btc_r = np.diff(btc_c) / btc_c[:-1]
            # β = cov(币,BTC)/var(BTC)：cov 与 var 必须同 ddof（混用会系统放大 n/(n-1)）
            cov_m = np.cov(sym_r, btc_r)
            var = float(cov_m[1, 1])
            if var > 1e-12:
                beta = float(cov_m[0, 1] / var)
                if not np.isfinite(beta) or beta <= 0:
                    beta = None  # 负 β/非有限值：回归不可信，按缺失处理
    except Exception as e:
        logger.debug("get_beta %s 回归计算失败: %s", symbol, e)
        beta = None
    _cache_set(key, beta if beta is not None else 0, _BETA_TTL)
    return beta


def build_market_context(
    client, symbol: str, crash_pct: Optional[float] = None,
) -> Optional[str]:
    """AI 事实包市场环境文本块（docs/24 A4①）：本币资金费率 + BTC 状态摘要。

    返回两行左右中文文本（不带标题，调用方拼「【市场环境】」标题注入提示词）；
    费率/BTC 任一环节失败静默降级为跳过对应行，全失败返回 None（不注入）。
    client: BinanceClient 实例（拉 BTC K 线；测试传 fake）；资金费率走
    get_funding（/fapi/v1/premiumIndex，带进程内 TTL 缓存）。
    crash_pct（2026-10-02 上页面）：BTC 急跌阈值 %，透传 _btc_market_line
    （None=env 默认）。
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
        btc = _btc_market_line(client, crash_pct=crash_pct)
        if btc:
            lines.append(btc)
    except Exception as e:
        logger.debug("市场环境-BTC状态构建失败: %s", e)

    if not lines:
        return None
    return "\n".join(lines)
