"""N-16 入场抑制·度量先行（docs/16 §0.2 #3，2026-10-08）。

只产读数备料，不动交易链路：用虚拟账本 decided 行回答两个命题——
① 追高度：breakout_pct 越大（追价越远）反事实 R 是否越差（阈值拍板依据）；
② 反抽深度（2026-10-08 CCUSDT 墓碑个案新增）：回踩/反抽确认前，逆行段
   回吐了前段趋势的多少比例——极浅（近追价）组期望 R 是否显著更差。

口径：
- 分母 = 账本 decided 行（status=DONE，P-04：OPEN/EXPIRED 不进任何分母）；
- 追高度取关联 ScanResult.breakout_pct（仅 live 行有 scan_result_id，
  backfill 重放行无 → 「无记录」桶，覆盖率随 live 积累上升）；
- 反抽深度仅对回踩类信号计算（trigger_pullback / trigger_pullback_confirm）：
  前段腿 = 确认K前 24 根已收盘 1h K线（不含触碰/确认两根）的极值区间，
  逆行锚 = 触碰+确认两根的高低点极值；ratio = 逆行深度 ÷ 前段振幅（可 >1）。
  fib 分桶 ≤23.6% / 23.6~38.2% / 38.2~61.8% / ≥61.8%——极浅=没调够/没弹够
  就入场（追价），深=给了更好的价格。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1)
LEG_BARS = 24                    # 前段腿回看根数（1h）
PULLBACK_SIGNALS = ("trigger_pullback", "trigger_pullback_confirm")

RETRACE_BUCKETS = (
    (0.236, "≤23.6%（极浅·近追价）"),
    (0.382, "23.6~38.2%（浅）"),
    (0.618, "38.2~61.8%（标准）"),
    (float("inf"), "≥61.8%（深）"),
)


def _ms(dt: datetime) -> int:
    """naive UTC datetime → epoch ms（纪元差值法）。"""
    return int((dt - _EPOCH).total_seconds() * 1000)


def retracement_ratio(klines: list[list], direction: str,
                      signal_idx: int = -1) -> float | None:
    """反抽/回踩深度比（纯函数）：逆行段深度 ÷ 前段腿振幅。

    klines：已收盘 1h K线，signal_idx 指向信号（确认）K线。
    需要其前 ≥LEG_BARS+1 根（触碰K + 24 根腿）；不足或腿振幅为 0 → None。
    long：腿高点→回踩低点（触碰/确认两根最低）÷ 腿振幅；
    short：反弹高点（触碰/确认两根最高）→腿低点 ÷ 腿振幅。
    """
    idx = signal_idx if signal_idx >= 0 else len(klines) + signal_idx
    if idx < LEG_BARS + 1:
        return None
    leg = klines[idx - 1 - LEG_BARS:idx - 1]      # 触碰K之前的 LEG_BARS 根
    counter = klines[idx - 1:idx + 1]             # 触碰K + 确认K
    highs = [float(k[2]) for k in leg]
    lows = [float(k[3]) for k in leg]
    leg_high, leg_low = max(highs), min(lows)
    rng = leg_high - leg_low
    if rng <= 0:
        return None
    if direction == "long":
        anchor = min(float(k[3]) for k in counter)   # 回踩低点
        return (leg_high - anchor) / rng
    anchor = max(float(k[2]) for k in counter)       # 反弹高点
    return (anchor - leg_low) / rng


def retrace_bucket(ratio: float | None) -> str:
    if ratio is None:
        return "无法计算"
    for hi, label in RETRACE_BUCKETS:
        if ratio < hi:
            return label
    return RETRACE_BUCKETS[-1][1]


def breakout_bucket(pct: float | None) -> str:
    """追高度分桶（docs/16 §5：触发带 0.2~1.5%；>1.5% 即显著追价）。"""
    if pct is None:
        return "无记录"
    if pct < 0.5:
        return "≤0.5%"
    if pct < 1.0:
        return "0.5~1%"
    if pct <= 1.5:
        return "1~1.5%"
    return ">1.5%"


def _stat(rows: list) -> dict:
    """分桶读数（口径对齐 api/ledger._bucket：win_rate/exp_r 含费）。"""
    wins = [r for r in rows if r.outcome in ("win_tp1", "win_tp2")]
    sum_r = sum(float(r.r or 0) for r in rows)
    return {
        "decided": len(rows),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "exp_r": round(sum_r / len(rows), 4) if rows else None,
        "sum_r": round(sum_r, 2),
    }


def _aggregate(pairs: list[tuple[str, object]]) -> list[dict]:
    """pairs=[(桶名, ledger行)] → 按预注册桶序输出。"""
    order: list[str] = []
    grouped: dict[str, list] = {}
    for name, row in pairs:
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)
    return [{"bucket": name, **_stat(grouped[name])} for name in order]


def run_n16_research(db: Session, days: int = 14, fetcher=None) -> dict:
    """N-16 度量主入口：账本 decided 行 × 追高度 / 反抽深度两维度。

    fetcher：可注入 K线拉取 (db, pool, symbol, interval, start_ms, end_ms) →
    list[klines]；默认懒加载 backtest_tasks._load_klines（缓存优先+限速，
    与账本推进同款）。拉取失败的币整组落「无法计算」，不影响其余维度。
    """
    from app.models.ledger import VirtualLedger
    from app.models.scan import ScanResult

    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(VirtualLedger)
        .filter(VirtualLedger.status == "DONE", VirtualLedger.entry_bar_open >= cutoff)
        .all()
    )

    # ── 维度① 追高度：join ScanResult.breakout_pct（live 行限定）──
    sr_ids = {r.scan_result_id for r in rows if r.scan_result_id}
    pct_map: dict = {}
    if sr_ids:
        for sr in db.query(ScanResult.id, ScanResult.breakout_pct).filter(
                ScanResult.id.in_(sr_ids)).all():
            pct_map[sr.id] = sr.breakout_pct
    breakout_pairs = [
        (breakout_bucket(pct_map.get(r.scan_result_id)), r) for r in rows
    ]

    # ── 维度② 反抽深度：仅回踩类信号，按币拉一次 K线区间 ──
    pb_rows = [r for r in rows if r.signal_type in PULLBACK_SIGNALS]
    retrace_pairs: list[tuple[str, object]] = []
    kl_cache: dict[str, dict[int, list]] = {}
    by_symbol: dict[str, list] = {}
    for r in pb_rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    if fetcher is None:
        from app.services.exchange_pool import ExchangePool
        from app.tasks.backtest_tasks import _load_klines

        pool = ExchangePool()

        def fetcher(d, _pool, symbol, interval, start_ms, end_ms):
            return _load_klines(d, pool, symbol, interval, start_ms, end_ms)

    for sym, sym_rows in by_symbol.items():
        try:
            start = min(r.entry_bar_open for r in sym_rows) - timedelta(hours=LEG_BARS + 3)
            end = max(r.entry_bar_open for r in sym_rows) + timedelta(hours=2)
            kls = fetcher(db, None, sym, "1h", _ms(start), _ms(end)) or []
            kl_cache[sym] = {int(k[0]): k for k in kls}
        except Exception:
            logger.exception("N-16 度量拉 K线失败 %s，该币落「无法计算」", sym)
            kl_cache[sym] = {}

    for r in pb_rows:
        idx_map = kl_cache.get(r.symbol, {})
        sig_ms = _ms(r.entry_bar_open)
        ratio = None
        if sig_ms in idx_map:
            kls_sorted = [idx_map[t] for t in sorted(idx_map)]
            idx = [int(k[0]) for k in kls_sorted].index(sig_ms)
            ratio = retracement_ratio(kls_sorted, r.direction, idx)
        retrace_pairs.append((retrace_bucket(ratio), r))

    non_pullback_n = len(rows) - len(pb_rows)
    return {
        "window_days": days,
        "decided_total": len(rows),
        "breakout": {
            "note": "追高度＝ScanResult.breakout_pct（仅 live 行有关联，backfill 重放行落「无记录」）",
            "buckets": _aggregate(breakout_pairs),
        },
        "retracement": {
            "note": (f"反抽深度＝逆行段÷前段{LEG_BARS}根振幅（fib 分桶）；仅回踩类信号，"
                     f"其余 {non_pullback_n} 笔不适用"),
            "buckets": _aggregate(retrace_pairs),
        },
    }
