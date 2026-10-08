"""合约回测任务（docs/16 P-06）：异步执行采样→变体重放→聚合→落库。

镜像 spot_tasks.run_backtest_task 模式（open_task → 读参数 → 拉K线 → 计算
→ 落 summary/daily_series/orders_detail → DONE；异常写 error → FAILED）。

K 线获取：优先 BacktestKlineCache（覆盖区间命中即复用），未命中用
pool.get_klines_range 分页回拉（每页 1000 根、0.15s 限速），整段回写缓存。
回测只用 1h/4h/1d 三种周期——与生产扫描/复盘同源口径。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_, and_

from app.celery_app import celery_app
from app.config import settings
from app.services.exchange_pool import ExchangePool
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)

# 前向窗口默认根数（与生产复盘 OBSERVATION_HOURS=24 同口径；变体可用
# overrides.fwd_bars 覆盖，切片取全局最大值）
DEFAULT_FWD_BARS = 24
_PAGE_LIMIT = 1000          # get_klines_range 单页根数（币安上限 1500 内）
_PAGE_SLEEP = 0.15          # 分页限速（秒）
_D1_BUFFER_DAYS = 45        # D1 ATR 序列预热（atr_period=22 + 缓冲）
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "1h": 3_600_000, "4h": 4 * 3_600_000, "1d": 86_400_000}


def _interval_ms(interval: str) -> int:
    return _INTERVAL_MS.get(interval, 3_600_000)


def _fetch_range(pool: ExchangePool, symbol: str, interval: str,
                 start_ms: int, end_ms: int) -> list:
    """拉 [start_ms, end_ms] 区间 K 线（时间升序，含未收盘末根由调用方剔除）。

    从 end 往前翻页直至覆盖 start；单所失败向上抛（AllExchangesFailed），
    由任务层按币容错。
    """
    step = _interval_ms(interval)
    pages: list = []
    cursor = end_ms
    for _ in range(20):  # 页数硬上限（90 天 1h ≈ 3 页，20 页足够 1m 级误配防御）
        page = pool.get_klines_range(symbol, interval, _PAGE_LIMIT, cursor) or []
        if not page:
            break
        pages.insert(0, page)
        first = int(page[0][0])
        if first <= start_ms or len(page) < _PAGE_LIMIT:
            break
        cursor = first - 1
        time.sleep(_PAGE_SLEEP)
    out = [k for k in (k for p in pages for k in p) if int(k[0]) >= start_ms - step]
    return out


def _load_klines(db, pool: ExchangePool, symbol: str, interval: str,
                 start_ms: int, end_ms: int) -> list:
    """缓存优先的历史区间 K 线：BacktestKlineCache 覆盖请求区间即复用，
    否则分页回拉并整段回写（回写失败不阻断——缓存只是加速）。"""
    from app.models.backtest import BacktestKlineCache

    row = (
        db.query(BacktestKlineCache)
        .filter(BacktestKlineCache.symbol == symbol,
                BacktestKlineCache.interval == interval)
        .first()
    )
    step = _interval_ms(interval)
    if row and row.klines:
        ks = sorted(row.klines, key=lambda k: int(k[0]))
        if ks and int(ks[0][0]) <= start_ms and int(ks[-1][0]) + step >= end_ms:
            return [k for k in ks if int(k[0]) >= start_ms - step]
    data = _fetch_range(pool, symbol, interval, start_ms, end_ms)
    if data:
        payload = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                    float(k[4]), float(k[5])] for k in data]
        if row is None:
            row = BacktestKlineCache(symbol=symbol, interval=interval, klines=payload)
            db.add(row)
        else:
            row.klines = payload
        db.commit()
    return data


def _slice_fwd(kl_1h: list, entry_ms: int, n: int) -> list:
    """自入场时刻起的前向已收盘根（复盘同口径：open_time ≥ entry_ms 取前 n 根）。"""
    return [k for k in kl_1h if int(k[0]) >= entry_ms][:n]


def _parse_window(p: dict) -> tuple[int, int] | None:
    """params 的 start/end（YYYY-MM-DD，UTC 语义）→ [start_ms, end_ms)。

    end 含当日（取次日 0 点为开区间上界）；缺一或都缺省返回 None（走 days）。
    """
    s, e = p.get("start"), p.get("end")
    if not s or not e:
        return None
    start_ms = int(datetime.strptime(s, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((datetime.strptime(e, "%Y-%m-%d") + timedelta(days=1))
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
    return start_ms, end_ms


def _sample_signals(db, pool: ExchangePool, source: str, symbols: list,
                    days: int, fwd_slice: int,
                    window: tuple[int, int] | None = None) -> tuple[list, dict]:
    """信号采样公共分支（P-06 回测与 P-12 稳健性扫描共用；等价重构自原任务体）。

    window=(start_ms, end_ms) 提供时按任意起止窗口采样（优先于 days）：
    信号按 entry_time_ms 落在 [start, end) 内过滤，K 线按窗口 + 预热/前向
    缓冲装载。缺省走「最近 days 天」（原行为，P-06 不受影响）。
    返回 (signals, need)——need 为每币 1h K 线需求区间 [start_ms, end_ms]
    （D1 预热由装载层统一加 _D1_BUFFER_DAYS）。
    """
    from app.models.scan import AIAnalysis, ScanResult
    from app.models.trade import TradeRecord
    from app.services.backtest_engine import (
        build_signals_by_scan_replay,
        build_signals_from_history,
    )

    if window is not None:
        lo_dt = datetime.utcfromtimestamp(window[0] / 1000)
        hi_dt = datetime.utcfromtimestamp(window[1] / 1000)
    else:
        cutoff = datetime.utcnow() - timedelta(days=days)

    if source == "history":
        ai_conds = [
            or_(
                AIAnalysis.trade_decision == "suggest",
                and_(AIAnalysis.trade_decision == "skip",
                     AIAnalysis.veto_original.isnot(None)),
            ),
            AIAnalysis.created_at >= (lo_dt if window is not None else cutoff),
        ]
        if window is not None:
            ai_conds.append(AIAnalysis.created_at < hi_dt)
        if symbols:
            ai_conds.append(AIAnalysis.symbol.in_(symbols))
        ai_rows = db.execute(
            select(AIAnalysis, ScanResult)
            .outerjoin(ScanResult, AIAnalysis.scan_result_id == ScanResult.id)
            .where(*ai_conds)
        ).all()
        opened_lo = lo_dt if window is not None else cutoff
        tr_conds = [TradeRecord.opened_at >= opened_lo]
        if window is not None:
            tr_conds.append(TradeRecord.opened_at < hi_dt)
        if symbols:
            tr_conds.append(TradeRecord.symbol.in_(symbols))
        trade_rows = db.execute(
            select(TradeRecord).where(*tr_conds)
        ).scalars().all()
        # 初始三价快照（OPEN 事件）：rec.stop_loss 是现值（TP1 后移保本），
        # 回放必须用开仓时的初始 SL/TP
        trade_ids = [r.id for r in trade_rows]
        open_events: dict[str, dict] = {}
        if trade_ids:
            from app.models.trade import TradeEvent
            for te in db.execute(
                select(TradeEvent).where(
                    TradeEvent.trade_record_id.in_(trade_ids),
                    TradeEvent.event_type == "OPEN",
                )
            ).scalars().all():
                open_events[str(te.trade_record_id)] = te.detail or {}
        signals = build_signals_from_history(ai_rows, list(trade_rows),
                                             open_events)
        # 源A样本 K 线需求：每币 [最早入场 − 1h, 最晚入场 + fwd+1 根] + D1
        need: dict[str, tuple[int, int]] = {}
        for s in signals:
            lo, hi = need.get(s["symbol"], (10**18, 0))
            need[s["symbol"]] = (
                min(lo, s["entry_time_ms"] - 3_600_000),
                max(hi, s["entry_time_ms"] + (fwd_slice + 1) * 3_600_000),
            )
    else:
        # 源B：滚动扫描重放。预热 warmup=240 根 + 窗口根数；4h 背景 200 根
        from app.models.system_config import SystemConfig
        from app.services.scan_filters import resolve_scan_filters

        flt = resolve_scan_filters(db.get(SystemConfig, 1))
        warmup = 240
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        if window is not None:
            start_1h = window[0] - (warmup + fwd_slice + 1) * 3_600_000
            # 窗口尾信号的前向 K 线（越过大窗尾但有前向缓冲；不超当下）
            end_1h = min(now_ms, window[1] + (fwd_slice + 1) * 3_600_000)
        else:
            n_1h = days * 24 + warmup + fwd_slice + 1
            end_1h = now_ms
            start_1h = end_1h - n_1h * 3_600_000
        signals = []
        for sym in symbols:
            try:
                kl = _load_klines(db, pool, sym, "1h", start_1h, end_1h)
                htf = _load_klines(
                    db, pool, sym, "4h",
                    end_1h - 200 * 4 * 3_600_000, end_1h)
                if len(kl) < window + 5:
                    logger.warning("采样 %s: %s 1h 仅 %d 根（预热不足）跳过",
                                   sym, source, len(kl))
                    continue
                signals.extend(build_signals_by_scan_replay(
                    sym, kl, htf or [], flt))
            except Exception as e:
                logger.warning("采样 %s: %s 源B采样失败 %s", sym, source, e)
                continue
        need = {
            sym: (start_1h - _D1_BUFFER_DAYS * 86_400_000, end_1h)
            for sym in symbols
        }
    if window is not None:
        # 窗口外信号剔除（含源B 预热区/前向缓冲区产生的信号）
        signals = [s for s in signals
                   if window[0] <= int(s["entry_time_ms"]) < window[1]]
    return signals, need


@celery_app.task(name="app.tasks.backtest_tasks.run_futures_backtest_task",
                 soft_time_limit=1800, max_retries=0)
def run_futures_backtest_task(bt_id: str) -> dict:
    """合约回测主任务：采样（源A历史/源B扫描重放）→ 基线+N变体重放 →
    聚合统计 + 组合模拟 → 落库。"""
    from app.database import SessionLocal
    from app.models.backtest import FuturesBacktest
    from app.services.backtest_engine import (
        BACKTEST_ENGINE_VERSION,
        aggregate,
        replay_exit,
    )

    task_id = open_task("backtest", f"合约回测 {bt_id[:8]}")
    db = SessionLocal()
    pool = ExchangePool()
    row = None
    try:
        row = db.get(FuturesBacktest, bt_id)
        if row is None:
            raise ValueError(f"回测 {bt_id} 不存在")
        p = row.params
        source = p.get("source", "scan")
        symbols = list(p.get("symbols") or [])
        days = int(p.get("days") or 30)
        variants = p.get("variants") or [{"name": "baseline", "overrides": {}}]
        pf = p.get("portfolio") or {}
        risk_usdt = float(pf.get("risk_usdt", 100.0))
        max_open = int(pf.get("max_open_trades", 5))
        same_cap = int(pf.get("same_direction_cap", 0))
        fwd_default = DEFAULT_FWD_BARS
        # 切片长度取全部变体 fwd_bars 最大值（引擎内再按各自 fwd_bars 截断）
        fwd_slice = max(
            [fwd_default]
            + [int((v.get("overrides") or {}).get("fwd_bars") or 0)
               for v in variants]
        )
        logger.info(
            "回测 %s 开始: source=%s symbols=%s days=%d variants=%d",
            bt_id[:8], source, symbols or "(全部)", days, len(variants),
        )

        now = datetime.utcnow()
        cutoff = now - timedelta(days=days)
        cutoff_ms = int(cutoff.timestamp() * 1000)

        # ── 采样（公共分支，P-12 稳健性扫描同款）──────────────────────
        signals, need = _sample_signals(db, pool, source, symbols, days,
                                        fwd_slice)

        n_signals = len(signals)
        if n_signals == 0:
            raise ValueError("采样结果为空：区间内无可用信号（放宽 days/币种后重试）")

        # ── K 线装载（1h 按需区间 + 每币 D1 供 ATR 腿）─────────────────
        kl_1h: dict[str, list] = {}
        kl_d1: dict[str, list] = {}
        syms_needed = sorted({s["symbol"] for s in signals} | set(need))
        for sym in syms_needed:
            lo, hi = need.get(sym, (cutoff_ms, int(now.timestamp() * 1000)))
            try:
                kl_1h[sym] = _load_klines(db, pool, sym, "1h", lo, hi)
                kl_d1[sym] = _load_klines(
                    db, pool, sym, "1d",
                    lo - _D1_BUFFER_DAYS * 86_400_000, hi)
            except Exception as e:
                logger.warning("回测 %s: %s K线拉取失败 %s", bt_id[:8], sym, e)

        # ── 变体重放（首个恒 baseline）────────────────────────────────
        def _replay(overrides: dict) -> list[dict]:
            out = []
            for s in signals:
                bars = _slice_fwd(kl_1h.get(s["symbol"], []),
                                  s["entry_time_ms"], fwd_slice)
                out.append(replay_exit(
                    s["direction"], s["entry"], s["stop"], s["tp1"],
                    s["tp2"], bars, overrides or None,
                    kl_d1.get(s["symbol"])))
            return out

        base_results = _replay(variants[0].get("overrides") or {})
        var_tuples = []
        for v in variants[1:]:
            var_tuples.append((v["name"], v.get("overrides") or {},
                               _replay(v.get("overrides") or {})))

        summary = aggregate(base_results, var_tuples, signals,
                            risk_usdt=risk_usdt, max_open_trades=max_open,
                            same_direction_cap=same_cap)
        summary["engine_version"] = BACKTEST_ENGINE_VERSION
        summary["data_range"] = {
            "days": days,
            "cutoff": cutoff.isoformat(),
            "symbols": sorted({s["symbol"] for s in signals}),
        }
        summary["signals"] = {"n": n_signals}

        # daily_series：基线组合日曲线（前端默认画基线；各变体曲线在
        # summary.portfolio[i].daily_series 内——aggregate 平铺结构）
        base_pf = summary["portfolio"][0] if summary.get("portfolio") else {}
        row.daily_series = {"items": base_pf.get("daily_series", [])}

        # orders_detail：基线逐笔明细（32KB 截断惯例）
        items = []
        for s, r in zip(signals, base_results):
            items.append({
                "symbol": s["symbol"], "direction": s["direction"],
                "entry": s["entry"], "stop": s["stop"], "tp1": s["tp1"],
                "tp2": s["tp2"], "entry_time_ms": s["entry_time_ms"],
                "src": (s.get("meta") or {}).get("src"),
                "outcome": r["outcome"], "r": r["r"], "legs": r["legs"],
                "bars": r["bars"],
            })
        truncated = False
        import json as _json
        while len(items) > 1 and len(_json.dumps({"items": items}).encode()) > 32_000:
            items.pop()
            truncated = True
        row.orders_detail = {"items": items, "truncated": truncated}

        row.summary = summary
        row.status = "DONE"
        row.finished_at = datetime.utcnow()
        db.commit()
        base_v = summary["variants"][0] if summary.get("variants") else {}
        logger.info(
            "回测 %s 完成: %d 信号 基线 decided=%s exp_r=%s",
            bt_id[:8], n_signals, base_v.get("decided"), base_v.get("exp_r"),
        )
        close_task(task_id, "completed",
                   summary=f"{source} 源 {n_signals} 信号 × {len(variants)} 变体",
                   detail={"backtest_id": str(row.id)})
        return {"ok": True, "signals": n_signals}
    except Exception as e:
        db.rollback()
        err = f"{type(e).__name__}: {e}"[:2000]
        try:
            row = db.get(FuturesBacktest, bt_id)
            if row is not None:
                row.status = "FAILED"
                row.error = err
                row.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("回测失败状态写入失败")
        close_task(task_id, "failed", error=err)
        logger.exception("合约回测失败 id=%s", bt_id[:8])
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.backtest_tasks.run_robustness_scan_task",
                 soft_time_limit=1800, max_retries=0)
def run_robustness_scan_task(scan_id: str) -> dict:
    """P-12 参数稳健性扫描：网格变体 × 同一信号集重放 → PBO/DSR 折扣判据。

    采样复用 _sample_signals（与 P-06 同源）；逐变体 replay_exit 后：
    - 每变体用自己的 decided R 序列出统计（decided/exp_r/win_rate/SR/偏度/峰度）
      + deflated_sharpe（n_trials=网格变体数，SR 方差=跨变体样本方差）；
    - R 矩阵只留「全部变体均 decided」的行（保守配对，P-04 口径）→ CSCV PBO。
    纯离线报告：不改任何生产参数，拍板走 docs/16 §0 流程。
    """
    from uuid import UUID as _UUID

    from app.database import SessionLocal
    from app.models.backtest import RobustnessScan
    from app.services import robustness as rb
    from app.services.backtest_engine import BACKTEST_ENGINE_VERSION, replay_exit

    task_id = open_task("robustness", f"稳健性扫描 {scan_id[:8]}")
    db = SessionLocal()
    pool = ExchangePool()
    row = None
    try:
        row = db.get(RobustnessScan, _UUID(str(scan_id)))
        if row is None:
            raise ValueError(f"稳健性扫描 {scan_id} 不存在")
        p = row.params
        source = p.get("source", "scan")
        symbols = list(p.get("symbols") or [])
        days = int(p.get("days") or 30)
        n_blocks = int(p.get("n_blocks") or rb.DEFAULT_N_BLOCKS)
        variants = rb.expand_grid(p.get("grid"))
        n_variants = len(variants)
        window = _parse_window(p)
        fwd_slice = max(
            [DEFAULT_FWD_BARS]
            + [int(v["overrides"].get("fwd_bars") or 0) for v in variants]
        )
        logger.info(
            "稳健性 %s 开始: source=%s symbols=%s window=%s days=%d 网格=%d 变体",
            scan_id[:8], source, symbols or "(全部)",
            f"{p.get('start')}~{p.get('end')}" if window else f"近{days}天",
            days, n_variants,
        )

        signals, need = _sample_signals(db, pool, source, symbols, days,
                                        fwd_slice, window=window)
        n_signals = len(signals)
        if n_signals == 0:
            raise ValueError("采样结果为空：区间内无可用信号（扩窗口/放宽币种后重试）")

        # ── K 线装载（P-06 主任务同款）────────────────────────────────
        now = datetime.utcnow()
        cutoff_ms = int(now.timestamp() * 1000) - days * 86_400_000
        kl_1h: dict[str, list] = {}
        kl_d1: dict[str, list] = {}
        syms_needed = sorted({s["symbol"] for s in signals} | set(need))
        for sym in syms_needed:
            lo, hi = need.get(sym, (cutoff_ms, int(now.timestamp() * 1000)))
            try:
                kl_1h[sym] = _load_klines(db, pool, sym, "1h", lo, hi)
                kl_d1[sym] = _load_klines(
                    db, pool, sym, "1d", lo - _D1_BUFFER_DAYS * 86_400_000, hi)
            except Exception as e:
                logger.warning("稳健性 %s: %s K线拉取失败 %s", scan_id[:8], sym, e)

        # ── 逐变体重放 ────────────────────────────────────────────────
        def _replay(overrides: dict) -> list[dict]:
            out = []
            for s in signals:
                bars = _slice_fwd(kl_1h.get(s["symbol"], []),
                                  s["entry_time_ms"], fwd_slice)
                out.append(replay_exit(
                    s["direction"], s["entry"], s["stop"], s["tp1"],
                    s["tp2"], bars, overrides or None,
                    kl_d1.get(s["symbol"])))
            return out

        all_results = [_replay(v["overrides"]) for v in variants]

        def _decided(r: dict) -> bool:
            return r["outcome"] not in ("open", "invalid")

        # 每变体自身 decided R 序列（统计与 DSR 用）
        per_series: list[list[float]] = []
        for j in range(n_variants):
            per_series.append(
                [all_results[j][i]["r"] for i in range(n_signals)
                 if _decided(all_results[j][i])])
        sr_list = [rb.probabilistic_sharpe(s) for s in per_series]
        mean_sr = sum(sr_list) / n_variants
        sr_var = (sum((x - mean_sr) ** 2 for x in sr_list) / (n_variants - 1)
                  if n_variants > 1 else 0.0)

        # R 矩阵（全变体均 decided 的行）→ CSCV PBO
        matrix: list[list[float]] = []
        excluded = 0
        for i in range(n_signals):
            if all(_decided(all_results[j][i]) for j in range(n_variants)):
                matrix.append([all_results[j][i]["r"] for j in range(n_variants)])
            else:
                excluded += 1

        pbo_out = None
        if len(matrix) >= n_blocks * 5 and n_variants >= 2:
            pbo_out = rb.pbo_cscv(matrix, n_blocks=n_blocks)
            oos_median = pbo_out["oos_sr_median"]
        else:
            oos_median = None
            logger.warning(
                "稳健性 %s: PBO 样本不足（矩阵 %d 行 < %d×5），跳过 CSCV",
                scan_id[:8], len(matrix), n_blocks)

        # 逐变体统计 + DSR；best_is=全样本 SR 最优，best_oos=OOS 中位 SR 最优
        # （PBO 未实算时 OOS 无读数——回退成 best_is 会被前端误标「=OOS最优」）
        best_is_i = max(range(n_variants), key=lambda c: (sr_list[c], -c))
        best_oos_i = (max(range(n_variants), key=lambda c: oos_median[c])
                      if oos_median else None)
        variant_rows = []
        for j, v in enumerate(variants):
            st = rb.per_variant_stats(per_series[j], sr_var, n_variants)
            variant_rows.append({"name": v["name"], "overrides": v["overrides"],
                                 **st})
        best_is_name = variants[best_is_i]["name"]
        best_oos_name = variants[best_oos_i]["name"] if best_oos_i is not None else None
        dsr_best_is = variant_rows[best_is_i]["dsr"]

        conclusion = (
            rb.conclude(pbo_out["pbo"], dsr_best_is, best_is_name,
                        best_oos_name)
            if pbo_out
            else {"verdict": "insufficient",
                  "text": f"decided 共同样本不足（矩阵 {len(matrix)} 行），"
                          "扩窗口/放宽币种后重扫；本轮仅出逐变体统计。",
                  "best_is": best_is_name, "best_oos": None,
                  "best_is_is_best_oos": False}
        )

        summary = {
            "n_trials": n_variants,
            "n_signals": n_signals,
            "matrix_rows": len(matrix),
            "excluded_n": excluded,
            "pbo": pbo_out,
            "variants": variant_rows,
            "best_is": best_is_name,
            "best_oos": best_oos_name,
            "conclusion": conclusion,
            "engine_version": BACKTEST_ENGINE_VERSION,
            "data_range": {"days": days,
                           "symbols": sorted({s["symbol"] for s in signals}),
                           **({"start": p.get("start"), "end": p.get("end")}
                              if window else {})},
        }
        row.summary = summary
        row.status = "DONE"
        row.finished_at = datetime.utcnow()
        db.commit()
        logger.info(
            "稳健性 %s 完成: %d 信号 矩阵 %d 行 PBO=%s verdict=%s",
            scan_id[:8], n_signals, len(matrix),
            round(pbo_out["pbo"], 3) if pbo_out else "—", conclusion["verdict"],
        )
        close_task(task_id, "completed",
                   summary=f"{source} 源 {n_signals} 信号 × {n_variants} 变体 "
                           f"→ {conclusion['verdict']}",
                   detail={"scan_id": str(row.id)})
        return {"ok": True, "signals": n_signals}
    except Exception as e:
        db.rollback()
        err = f"{type(e).__name__}: {e}"[:2000]
        try:
            row = db.get(RobustnessScan, _UUID(str(scan_id)))
            if row is not None:
                row.status = "FAILED"
                row.error = err
                row.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("稳健性扫描失败状态写入失败")
        close_task(task_id, "failed", error=err)
        logger.exception("稳健性扫描失败 id=%s", scan_id[:8])
        raise
    finally:
        db.close()
