"""AI 建议复盘任务（docs/04 §4 Stage 6 / P2）

对 suggest 决策在 24h 观察期后做一次 1h K 线逐根回放：
- 先触止损 → loss；先触 tp2 → win_tp2；触 tp1（后又触止损）→ win_tp1
- 同一根 K 线同时触碰止损与止盈 → 保守计 loss
- 观察期内未触任何价位 → expired
统计维度快照（signal_type/position/ema_state）随复盘落库，供分组胜率统计。
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import AIAnalysis, ScanResult, TradeReview
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.exchange_pool import ExchangePool
from app.services.llm_client import configure_from
from app.services.shadow import tag_trade_close
from app.services.task_log import close_task, open_task

logger = logging.getLogger(__name__)

OBSERVATION_HOURS = 24
REVIEW_BATCH_SIZE = 20
REVIEW_LOOKBACK_DAYS = 7
# 到期时刻按 UTC 半小时分片轮转：每轮只取 REVIEW_SHARDS_PER_RUN 个分片，
# 约 24 小时轮完一圈（48 分片 / 每轮 6 个 × 每 30 分钟一轮）。这样某个分片里
# 长期取不到行情的坏样本不会把后面的新样本永久挤出队列。
REVIEW_SHARD_SECONDS = 1800
REVIEW_SHARD_COUNT = 48
REVIEW_SHARDS_PER_RUN = 6


def _as_utc(value: datetime) -> datetime:
    """DB timestamps are naive UTC; normalize aware values without local-time conversion."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _due_shard(due: datetime) -> int:
    """Shard index of a due time: UTC half-hour bucket modulo the shard count."""
    return int(_as_utc(due).timestamp() // REVIEW_SHARD_SECONDS) % REVIEW_SHARD_COUNT


def _review_shard_hours(now: datetime, round_no: int = 0) -> list[int]:
    """Return the due-time shards covered by one run, newest shard first.

    Shard of a due time is its UTC half-hour bucket modulo REVIEW_SHARD_COUNT.
    Advancing the run slot by one moves the whole window back by one shard, so
    REVIEW_SHARD_COUNT consecutive slots visit every shard exactly once.
    """
    base = int(_as_utc(now).timestamp() // REVIEW_SHARD_SECONDS) % REVIEW_SHARD_COUNT
    width = min(REVIEW_SHARDS_PER_RUN, REVIEW_SHARD_COUNT)
    return [(base - round_no * width - i) % REVIEW_SHARD_COUNT for i in range(width)]


def _shard_ranges(shard: int, oldest: datetime, due_before: datetime) -> list[tuple]:
    """Half-open naive-UTC ranges whose due time falls in ``shard``."""
    step = REVIEW_SHARD_SECONDS
    lo = int(_as_utc(oldest).timestamp())
    hi = int(_as_utc(due_before).timestamp()) + 1
    ranges = []
    bucket = lo // step
    while bucket * step < hi:
        if bucket % REVIEW_SHARD_COUNT == shard:
            start = max(bucket * step, lo)
            end = min((bucket + 1) * step, hi)
            if start < end:
                ranges.append((
                    datetime.utcfromtimestamp(start),
                    datetime.utcfromtimestamp(end),
                ))
        bucket += 1
    return ranges


def _review_window(signal_time: datetime) -> tuple[int, int]:
    start = _as_utc(signal_time)
    start_ms = int(start.timestamp() * 1000)
    return start_ms, start_ms + OBSERVATION_HOURS * 3_600_000


def _interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    try:
        return int(interval[:-1]) * units[interval[-1]]
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"unsupported kline interval: {interval}")


def _expected_window_bars(start_ms: int, end_ms: int, interval: str) -> range:
    step = _interval_ms(interval)
    first = start_ms - start_ms % step
    if first < start_ms:
        first += step
    return range(first, end_ms - step + 1, step)


def _has_complete_window(bars: list, start_ms: int, end_ms: int, interval: str) -> bool:
    """Require complete bars wholly inside the 24h window; partial edge bars are excluded."""
    expected = _expected_window_bars(start_ms, end_ms, interval)
    by_time = {int(float(k[0])): k for k in bars}
    return all(ts in by_time for ts in expected)


def replay_suggestion(
    direction: str, entry: float, stop: float, tp1: float, tp2: float,
    bars: list,
) -> tuple[str, bool, int]:
    """Replay a counterfactual price path, not an actual exchange execution.

    Only complete bars are supplied. Entry is considered filled if its price is
    inside a bar's OHLC range. On the entry bar, a touched stop is a conservative
    loss; targets are ignored because OHLC cannot prove entry preceded target.
    For later active bars, stop wins same-bar ambiguity; prior TP1 remains banked.
    """
    tp1_hit = False
    active = False
    for i, k in enumerate(bars):
        high, low = float(k[2]), float(k[3])
        if not active:
            if not low <= entry <= high:
                continue
            active = True
            hit_stop = low <= stop if direction == "long" else high >= stop
            if hit_stop:
                return "loss", False, i + 1
            continue

        if direction == "long":
            hit_stop = low <= stop
            hit_tp1 = high >= tp1
            hit_tp2 = tp2 > 0 and high >= tp2
        else:
            hit_stop = high >= stop
            hit_tp1 = low <= tp1
            hit_tp2 = tp2 > 0 and low <= tp2

        if hit_stop:
            return ("loss" if not tp1_hit else "win_tp1"), tp1_hit, i + 1
        if hit_tp2:
            return "win_tp2", True, i + 1
        if hit_tp1:
            tp1_hit = True

    return ("win_tp1" if tp1_hit else "open"), tp1_hit, len(bars)


@celery_app.task(name="app.tasks.review_tasks.run_trade_review_task", max_retries=0)
def run_trade_review_task():
    """Replay due suggestions/vetoes as a counterfactual price path, not actual fills.

    Only complete candles wholly inside the fixed 24h window are replayed. The
    partial first candle is intentionally excluded, so this is not full coverage
    of all prices from the exact signal instant. Missing bars remain pending.
    """
    db = SessionLocal()
    pool = ExchangePool()
    task_id = ""
    try:
        now = datetime.now(timezone.utc)
        now_naive = now.replace(tzinfo=None)
        due_before = now_naive - timedelta(hours=OBSERVATION_HOURS)
        oldest = due_before - timedelta(days=REVIEW_LOOKBACK_DAYS)

        # 超过补偿窗口仍未落盘的行不再请求行情，避免无界补历史；保留计数告警，
        # 人工可从任务记录中看到积压而不是静默丢弃。
        aged_pending = db.execute(
            select(func.count()).select_from(AIAnalysis).outerjoin(
                TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id
            ).where(
                or_(
                    AIAnalysis.trade_decision == "suggest",
                    and_(AIAnalysis.trade_decision == "skip",
                         cast(AIAnalysis.veto_original, String) != "null"),
                ),
                func.coalesce(AIAnalysis.ai_finished_at, AIAnalysis.created_at) < oldest,
                TradeReview.id.is_(None),
            )
        ).scalar_one()
        if aged_pending:
            logger.warning("有 %d 条待复盘超出 %d 天自动补偿范围，未自动处理",
                           aged_pending, REVIEW_LOOKBACK_DAYS)

        # 回放对象（docs/24 A5a 扩容）：suggest 单 + veto 否决单（trade_decision=skip
        # 但带 veto_original 原始三价快照）——否决单回放"假想表现"，与通过单对比
        # 验证双评委 veto 是否有正贡献（此前每笔 suggest 花 3 次 LLM 调用无效果度量）
        stmt = (
            select(AIAnalysis, ScanResult)
            .join(ScanResult, AIAnalysis.scan_result_id == ScanResult.id)
            .outerjoin(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
            .where(
                or_(
                    AIAnalysis.trade_decision == "suggest",
                    and_(
                        AIAnalysis.trade_decision == "skip",
                        # 有 veto 快照（非 SQL NULL、亦非 JSON `null`）——用内容判据而非
                        # IS NOT NULL，见 models/scan.py veto_original 列注（2026-10-20）
                        cast(AIAnalysis.veto_original, String) != "null",
                    ),
                ),
                func.coalesce(AIAnalysis.ai_finished_at, AIAnalysis.created_at) <= due_before,
                func.coalesce(AIAnalysis.ai_finished_at, AIAnalysis.created_at) >= oldest,
                TradeReview.id.is_(None),
            )
        )
        count = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        if not count:
            return
        due_expr = func.coalesce(AIAnalysis.ai_finished_at, AIAnalysis.created_at)

        def shard_stmt(shard: int):
            ranges = _shard_ranges(shard, oldest, due_before)
            if not ranges:
                return None
            return stmt.where(or_(*[
                and_(due_expr >= a, due_expr < b) for a, b in ranges
            ])).order_by(due_expr, AIAnalysis.id)

        # 分片内再按游标轮转：先保证每个分片本轮至少拿到一页，预算有剩再回头
        # 取同分片的下一行——单个分片的坏样本不会挤占其它分片的配额。
        offsets = {shard: 0 for shard in _review_shard_hours(now)}
        rows = []
        remaining = REVIEW_BATCH_SIZE
        while remaining > 0:
            progressed = False
            for shard in list(offsets):
                if remaining <= 0:
                    break
                query = shard_stmt(shard)
                if query is None:
                    continue
                batch = db.execute(
                    query.offset(offsets[shard]).limit(1)
                ).all()
                if not batch:
                    continue
                progressed = True
                rows.extend(batch)
                offsets[shard] += 1
                remaining -= 1
            if not progressed:
                break
        if not rows:
            return  # 无待复盘不记任务记录（每 30 分钟一次，避免刷屏）
        logger.info("复盘任务：%d 条建议待复盘", len(rows))
        task_id = open_task(task_type="review", task_name="建议复盘", trigger="scheduled")
        reviewed = 0
        # 明细随任务记录落库（前端任务记录页可展开查看）：逐条结论 + 分组计数
        items = []
        counts = {"win_tp1": 0, "win_tp2": 0, "loss": 0, "expired": 0}

        for a, sr in rows:
            try:
                # veto 否决单（docs/24 A5a）：三价/方向/评分取 veto_original 快照
                # （强制 skip 已把分析行自身三价清空），is_veto=True 落库供对比统计
                veto = (
                    a.veto_original
                    if a.trade_decision != "suggest" and a.veto_original
                    else None
                )
                if veto:
                    r_direction = str(veto.get("direction") or "")
                    entry = float(veto.get("entry_price") or 0)
                    stop = float(veto.get("stop_loss") or 0)
                    tp1 = float(veto.get("take_profit_1") or 0)
                    tp2 = float(veto.get("take_profit_2") or 0)
                    r_rating = veto.get("recommendation")
                else:
                    r_direction = a.direction
                    entry = float(a.entry_price or 0)
                    stop = float(a.stop_loss or 0)
                    tp1 = float(a.take_profit_1 or 0)
                    tp2 = float(a.take_profit_2 or 0)
                    r_rating = a.recommendation

                signal_time = a.ai_finished_at or a.created_at
                start_ms, end_ms = _review_window(signal_time)
                interval_ms = _interval_ms(settings.KLINE_INTERVAL)
                now_ms = int(now.timestamp() * 1000)
                end_closed_ms = min(end_ms - 1, now_ms - interval_ms)
                expected = len(_expected_window_bars(start_ms, end_ms, settings.KLINE_INTERVAL))
                klines = pool.get_klines_range(
                    a.symbol, settings.KLINE_INTERVAL, expected + 2, end_closed_ms
                ) or []
                bars = [k for k in klines
                        if start_ms <= int(float(k[0])) < end_ms
                        and int(float(k[0])) + interval_ms <= now_ms]

                if entry <= 0 or stop <= 0 or tp1 <= 0 or not r_direction:
                    outcome, tp1_hit, nbars = "expired", False, 0
                elif not _has_complete_window(bars, start_ms, end_ms, settings.KLINE_INTERVAL):
                    logger.warning("建议复盘数据不完整，保留待补偿: %s window=%s..%s bars=%d/%d",
                                   a.symbol, start_ms, end_ms, len(bars), expected)
                    continue
                else:
                    outcome, tp1_hit, nbars = replay_suggestion(
                        r_direction, entry, stop, tp1, tp2, bars
                    )
                    if outcome == "open":
                        outcome = "expired"

                db.add(TradeReview(
                    ai_analysis_id=UUID(str(a.id)),
                    symbol=a.symbol,
                    direction=r_direction,
                    outcome=outcome,
                    tp1_hit=tp1_hit,
                    bars_to_exit=nbars or None,
                    signal_type=sr.signal_type,
                    touch_ref=sr.touch_ref,  # 回踩触点快照（docs/22），复盘按触点分组
                    position=sr.position,
                    ema_state=sr.ema_state,
                    # 4h 背景状态快照（2026-10-20）：影子 A/B 与「4h趋势期」维度用；
                    # 与 ab_bucket 同在复盘行上，/review/stats 可直接分组无需回查分析行
                    htf_phase=getattr(a, "htf_phase", None),
                    # 4h 数据降级快照（2026-10-21 观测 B3）：分析时无 4h 背景的
                    # 建议单列统计，与正常组对比判断降级是否伤质量
                    htf_degraded=bool(getattr(a, "htf_degraded", False)),
                    ab_bucket=(veto.get("ab_bucket") if veto else None),
                    recommendation=float(r_rating) if r_rating is not None else None,
                    is_veto=bool(veto),
                ))
                db.commit()
                reviewed += 1
                counts[outcome] = counts.get(outcome, 0) + 1
                items.append({
                    "symbol": a.symbol,
                    "direction": r_direction,
                    "outcome": outcome,
                    "bars_to_exit": nbars or None,
                    "recommendation": float(r_rating) if r_rating is not None else None,
                    "is_veto": bool(veto),
                })
                logger.info(
                    "复盘完成: %s %s -> %s%s",
                    a.symbol, r_direction, outcome, "（veto 假想回放）" if veto else "",
                )
            except Exception as e:
                logger.warning("复盘 %s 失败: %s", a.symbol, e)
                db.rollback()
                continue
        close_task(task_id, "completed", summary=f"复盘 {reviewed} 条建议", detail={
            "counts": counts,
            "items": items,
        })
    except Exception as e:
        logger.exception("复盘任务失败: %s", e)
        close_task(task_id, "failed", error=e)
    finally:
        db.close()


# ===== AI 错误归因（复盘重设计 2026-09-18） =====

# 单轮最多归因笔数（每笔一次 LLM 调用，控制开销与单轮时长）
ATTRIBUTION_BATCH = 5

# 镜像跳过打标文案（常量）：already 查询以它排除「跳过打标」的兄弟单——
# 否则 picked 单归因失败重进队列时，会被已打标的跳过单误判为
# 「兄弟已归因」而永久跳过（跨轮误伤：该单从未真正归因却再无机会）
MIRROR_SKIP_LESSON = (
    "同源镜像单已归因（双网同开的同一 AI 结论只复盘一次，正式网优先），本单跳过"
)


def _dedup_sibling_trades(db, trades: list) -> tuple[list, list]:
    """同源镜像单去重（2026-10-03 双网同开）：同一 AI 分析（ai_analysis_id）
    在测试网/正式网各落一单，行情同源、结论同源——复盘只做一次。

    规则：无 analysis 关联的（导入持仓/手动单）不受影响；同 analysis 的
    兄弟单组内正式网优先（实盘归因更有价值），且兄弟单已在历史轮归因过的
    （attributed_at 非空，含跨轮：两网平仓时间不同批）直接跳过。

    返回 (待归因清单, 跳过清单)。调用方负责给跳过单打标（不调 LLM）。"""
    sibling_ids = {t.ai_analysis_id for t in trades if t.ai_analysis_id}
    already = set()
    if sibling_ids:
        # 已归因判定排除「镜像跳过打标」行（error_lesson == MIRROR_SKIP_LESSON）：
        # 跳过打标虽写了 attributed_at，但代表的是兄弟单占位而非本组已完成归因
        already = {
            str(aid) for aid in db.execute(
                select(TradeRecord.ai_analysis_id).where(
                    TradeRecord.ai_analysis_id.in_(sibling_ids),
                    TradeRecord.attributed_at.isnot(None),
                    or_(
                        TradeRecord.error_lesson.is_(None),
                        TradeRecord.error_lesson != MIRROR_SKIP_LESSON,
                    ),
                )
            ).scalars().all()
        }
    groups: dict = {}
    picked: list = []
    skipped: list = []
    for t in trades:
        if t.ai_analysis_id is None or str(t.ai_analysis_id) in already:
            if t.ai_analysis_id is not None:
                skipped.append(t)  # 兄弟单已归因过 → 本单跳过
            else:
                picked.append(t)   # 无 analysis 关联：独立归因
            continue
        groups.setdefault(t.ai_analysis_id, []).append(t)
    for group in groups.values():
        # 正式网优先（testnet=False 排前）；同网多账号取闭合最晚的（信息最全）
        ordered = sorted(
            group,
            key=lambda t: (bool(t.testnet), t.closed_at or datetime.utcnow()),
        )
        picked.append(ordered[0])
        skipped.extend(ordered[1:])
    return picked, skipped


def run_ai_attribution(db, pool, trader, trades: list) -> list[dict]:
    """对给定已平仓交易逐笔 AI 归因并落库（定时任务与手动触发共用）。

    同源镜像单只归因一笔（_dedup_sibling_trades，正式网优先）——双网同开的
    重复单不重复花 LLM 调用、不污染标签统计；被跳过的镜像单打标
    attributed_at + error_lesson 说明（复盘面板可辨）。

    返回逐笔结果 [{symbol, ok, tags/lesson/error}]。
    """
    from app.services.ai_attribution import _interval_ms, attribute_trade

    cfg = db.get(SystemConfig, 1)
    configure_from(cfg)  # 生效 AI 运行参数（模型/端点/超时/并发）每轮重读 DB
    interval = cfg.kline_interval if cfg else "1h"

    trades, mirror_skipped = _dedup_sibling_trades(db, trades)
    results: list = []
    for rec in mirror_skipped:
        try:
            rec.attributed_at = datetime.utcnow()
            rec.error_lesson = MIRROR_SKIP_LESSON
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("镜像单跳过打标失败 %s", rec.symbol)
        logger.info(
            "归因跳过 %s %s（%s）：同源镜像单只复盘一次",
            rec.symbol, rec.direction, "正式网" if rec.testnet else "测试网",
        )
        # 跳过项也进结果（任务记录明细可见，summary 计数含跳过）
        results.append({
            "symbol": rec.symbol,
            "opened_at": rec.opened_at.isoformat() if rec.opened_at else None,
            "ok": True,
            "skipped": True,
            "lesson": MIRROR_SKIP_LESSON,
        })

    for rec in trades:
        item = {"symbol": rec.symbol, "opened_at": rec.opened_at.isoformat() if rec.opened_at else None}
        try:
            # K 线区间：开仓前 ~100 根 + 持仓全程（与交易复盘K线同源口径）
            opened = rec.opened_at
            closed = rec.closed_at or datetime.utcnow()
            held_bars = 0
            if opened:
                held_bars = int((closed - opened).total_seconds() * 1000 / _interval_ms(interval)) + 1
            bars = min(100 + max(held_bars, 1), 480)
            end_ms = int(closed.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1
            klines = pool.get_klines_range(rec.symbol, interval, bars, end_ms) or []

            verdict = attribute_trade(rec, klines)
            rec.error_tags = verdict["tags"]
            rec.error_lesson = verdict["lesson"]
            rec.attributed_at = datetime.utcnow()
            db.commit()
            item.update(ok=True, tags=verdict["tags"], lesson=verdict["lesson"])
            logger.info("AI 归因 %s %s: %s", rec.symbol, rec.direction, verdict["tags"])
        except Exception as e:
            db.rollback()
            item.update(ok=False, error=str(e)[:200])
            logger.warning("AI 归因失败 %s: %s", rec.symbol, e)
        results.append(item)
    return results


@celery_app.task(name="app.tasks.review_tasks.run_ai_attribution_task", max_retries=0)
def run_ai_attribution_task():
    """定时归因：找已平仓未归因的交易，逐笔 AI 复盘出错误标签与教训。

    开头先跑 trade_close 影子打标（docs/14 §七 CARD-09 评分门槛前向对照，
    AI-Key 门之前执行——打标只依赖交易记录不依赖 LLM，AI 未配置时也要积累）；
    打标与归因共用同一批未归因交易查询，但打标幂等、不受归因结果影响。"""
    db = SessionLocal()
    try:
        # ── 影子打标先行（异常全吞，绝不影响归因）────────────────────────
        try:
            pending_for_shadow = db.execute(
                select(TradeRecord)
                .where(
                    TradeRecord.status == "CLOSED",
                    TradeRecord.excluded.is_(False),  # 异常单剔除（2026-10-18）
                    TradeRecord.exit_reason.isnot(None),
                    TradeRecord.attributed_at.is_(None),
                )
                .order_by(TradeRecord.closed_at.desc())
                .limit(ATTRIBUTION_BATCH)
            ).scalars().all()
            if pending_for_shadow:
                tagged = tag_trade_close(pending_for_shadow)
                if tagged:
                    logger.info("trade_close 影子打标 %d 行（%d 笔交易）",
                                tagged, len(pending_for_shadow))
        except Exception as e:
            logger.debug("trade_close 影子打标批量异常（忽略）: %s", e)
    finally:
        db.close()

    if not settings.AI_API_KEY:
        return  # AI 未配置，静默跳过
    db = SessionLocal()
    pool = ExchangePool()
    trader = None
    task_id = ""
    try:
        # 费用回补先行（归因事实包需要 fee/funding）；双网各补各的
        # （2026-10-03 双网同开：归因队列不再按当前环境过滤——否则另一网的
        # 平仓单会一直积压，要等手动切环境才能归因）
        from app.services.binance_trader import BinanceTrader
        from app.services.actual_review import backfill_costs
        for testnet in (True, False):
            bt = BinanceTrader(testnet)
            if bt.configured:
                backfill_costs(db, bt, limit=ATTRIBUTION_BATCH)

        trades = db.execute(
            select(TradeRecord)
            .where(
                TradeRecord.status == "CLOSED",
                TradeRecord.excluded.is_(False),  # 异常单剔除（2026-10-18）
                TradeRecord.exit_reason.isnot(None),
                TradeRecord.attributed_at.is_(None),
            )
            .order_by(TradeRecord.closed_at.desc())
            .limit(ATTRIBUTION_BATCH)
        ).scalars().all()
        if not trades:
            return  # 无待归因不记任务记录（避免刷屏）

        task_id = open_task(task_type="review", task_name="AI错误归因", trigger="scheduled")
        results = run_ai_attribution(db, pool, None, trades)
        ok_n = sum(1 for r in results if r.get("ok"))
        close_task(task_id, "completed", summary=f"归因 {ok_n}/{len(results)} 笔", detail={
            "items": results,
        })
    except Exception as e:
        logger.exception("AI 归因任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()


# ===== 规则补丁闭环（docs/12 §10，2026-09-18） =====

@celery_app.task(name="app.tasks.review_tasks.run_patch_trigger_task", max_retries=0)
def run_patch_trigger_task():
    """每日触发：标签热度检查 → AI 生成补丁草稿（待人工确认合入）"""
    if not settings.AI_API_KEY:
        return
    db = SessionLocal()
    task_id = ""
    try:
        from app.services.patch_service import run_trigger
        task_id = open_task(task_type="review", task_name="补丁触发", trigger="scheduled")
        result = run_trigger(db)
        drafts = result["drafts"]
        summary = f"生成 {len(drafts)} 条补丁草稿"
        if not drafts and result["skips"]:
            summary = f"未生成补丁：{result['skips'][0]}"
        close_task(task_id, "completed",
                    summary=summary,
                    detail={
                        # 未生成时 skips 直接给出卡在哪关（样本/热度/已有草稿/冷却）
                        "skips": result["skips"],
                        "attributed_total_7d": result["attributed_total"],
                        "drafts": [
                            {"tag": d.target_tag, "kind": d.kind, "id": d.id}
                            for d in drafts
                        ],
                    })
    except Exception as e:
        logger.exception("补丁触发任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()


@celery_app.task(name="app.tasks.review_tasks.run_patch_evaluate_task", max_retries=0)
def run_patch_evaluate_task():
    """每日评估：active 补丁的三态判定（有效/无效/恶化告警）"""
    db = SessionLocal()
    task_id = ""
    try:
        from app.services.patch_service import run_evaluate
        task_id = open_task(task_type="review", task_name="补丁评估", trigger="scheduled")
        updated = run_evaluate(db)
        alerts = [p for p in updated if p.verdict == "worse"]
        close_task(task_id, "completed",
                   summary=f"评估 {len(updated)} 条补丁，告警 {len(alerts)} 条",
                   detail={"alerts": [
                       {"tag": p.target_tag, "id": p.id,
                        "checks": p.metrics_after_json.get("checks")}
                       for p in alerts
                   ]})
    except Exception as e:
        logger.exception("补丁评估任务失败: %s", e)
        if task_id:
            close_task(task_id, "failed", error=e)
    finally:
        db.close()
