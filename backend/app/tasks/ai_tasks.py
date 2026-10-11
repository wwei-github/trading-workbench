"""AI 分析 Celery 任务（P0 管线，docs/04）

- 批量：Celery group 按币种并行分发（原串行 + sleep(1) 已废弃）
- 并发：Redis zset 信号量限制 LLM 全局并发（LLM_CONCURRENCY，默认 3）
- Stage 1 闸门：
    1) 1h 内同指纹 → 沿用旧结论（价格区仍有效时）
- 程序侧 skip 闸门已全部移除（2026-09-15 拍板）：选中的币种必须经过真实 AI 分析，
  跳过与否由 AI 自己决定；重复命中由扫描侧抑制（scanner，同币同信号窗口内不落库）
- Stage 2：analyze_with_guard（校验回炉）→ 落库
"""
import hashlib
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from celery import group
from redis import Redis
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanRecord, ScanResult, AIAnalysis, TradeReview
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services import ai_progress
from app.services.ai_agent import analyze_coin_agent
from app.services.ai_analyzer import analyze_with_guard, calc_stop_reference
from app.services.scan_filters import resolve_scan_filters
from app.services.dual_judge import run_dual_judge
from app.services.strategy.technical import enforce_htf_constraint, render_indicator_block
from app.services.exchange_pool import ExchangePool
from app.services.shadow import tag_oi_conf
from app.services.risk_guard import is_pullback_wait
from app.services.strategy import recent_swings
from app.services.account_registry import auto_trade_accounts
from app.services.trade_engine import _consecutive_losses_today

logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None
_LLM_SEM_KEY = "sem:llm"
# 持有者过期清理阈值（ms）：大于单次分析最长耗时；崩溃泄漏最多 10 分钟
_SEM_STALE_MS = 600_000


def _get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _sem_acquire(timeout_s: int = 900) -> str:
    """获取 LLM 并发信号量，返回持有者 id（空串表示超时降级放行）"""
    r = _get_redis()
    ident = uuid4().hex
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        now_ms = int(time.time() * 1000)
        r.zremrangebyscore(_LLM_SEM_KEY, 0, now_ms - _SEM_STALE_MS)
        r.zadd(_LLM_SEM_KEY, {ident: now_ms})
        rank = r.zrank(_LLM_SEM_KEY, ident)
        if rank is not None and rank < settings.LLM_CONCURRENCY:
            return ident
        r.zrem(_LLM_SEM_KEY, ident)
        time.sleep(1.0)
    logger.warning("LLM 信号量等待超时（%ds），降级放行", timeout_s)
    return ""


def _sem_release(ident: str) -> None:
    if ident:
        try:
            _get_redis().zrem(_LLM_SEM_KEY, ident)
        except Exception:
            pass


def compute_fingerprint(signal: dict) -> str:
    """信号指纹：价格 0.5% 对数分桶 + 信号要素。同指纹在 TTL 内可复用结论"""
    close = float(signal.get("current_price") or 0)
    bucket = round(math.log(close) / 0.005) if close > 0 else 0
    raw = "|".join([
        signal.get("symbol", ""),
        signal.get("signal_type", ""),
        signal.get("position") or "",
        signal.get("pattern") or "",
        signal.get("touch_ref") or "",  # 触点进指纹（docs/22）：同价位桶不同触点不复用结论
        str(bucket),
    ])
    return hashlib.sha1(raw.encode()).hexdigest()[:40]


def _build_judge_signal(signal: dict, klines: list, htf_klines: Optional[list]) -> dict:
    """双评委事实包增厚（docs/24 D3）：把调用现场已有的技术指标块与摆动结构摘要
    注入 signal 副本传给 run_dual_judge——评委用数据核对三价与结构位矛盾
    （止损落在摆动区间内部、止盈近于争夺位）。零额外计算成本：
    klines / htf_klines / swings 均为分析现场已有变量；原 signal dict 不被污染。
    """
    judge_signal = dict(signal)
    judge_signal["indicator_block"] = render_indicator_block(klines, htf_klines)
    sw = signal.get("recent_swings") or {}
    if sw.get("highs") or sw.get("lows"):
        hi = f"{sw['highs'][0]['price']:.6g}" if sw.get("highs") else "—"
        lo = f"{sw['lows'][0]['price']:.6g}" if sw.get("lows") else "—"
        judge_signal["structure_summary"] = f"最近摆动高点 {hi} / 低点 {lo}"
    return judge_signal


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_task", bind=True, max_retries=3)
def run_ai_analysis_task(
    self, scan_record_id: str, only_scan_result_id: Optional[str] = None,
    user_input: Optional[str] = None,
):
    """对指定扫描记录的命中币种执行 AI 分析（并行分发）。

    scan_record_id: 扫描记录 ID
    only_scan_result_id: 若提供，只分析该单个 ScanResult（用于手动重新分析，强制跳过缓存短路）
    user_input: 用户补充说明（单币分析时随请求传入），附加到 AI 提示词
    """
    db = SessionLocal()
    # 任务记录只在分发层落（2026-10-05 补缺口 3）：单币子任务若逐个落条目，
    # 每轮扫描最多配额个 × 每小时一轮会刷爆任务列表——子任务失败已有系统日志
    # + 进度事件（ai_progress）双通道可见
    from app.services.task_log import close_task, open_task
    task_id = open_task(
        "ai", "AI 分析分发",
        trigger="manual" if only_scan_result_id else "system",
    )
    try:
        cfg = db.get(SystemConfig, 1)
        if not cfg or not settings.AI_API_KEY:
            logger.warning("AI 分析未启用（未配置 API Key），跳过")
            close_task(task_id, "skipped", summary="AI 未启用（未配置 API Key）")
            return
        if cfg and not cfg.ai_analysis_enabled and only_scan_result_id is None:
            # 「筛选后自动 AI 分析」开关（2026-09-28 与交易开关解耦）：仅拦截
            # 扫描链路的批量自动分析；手动单币重分析（only_scan_result_id 非空）
            # 不受限。交易开关（trading_enabled）只管自动成交，不再拦分析
            logger.info("筛选后自动 AI 分析开关关闭，批量分析跳过 #%s", scan_record_id)
            close_task(task_id, "skipped", summary="「筛选后自动 AI 分析」开关关闭")
            return
        q = select(ScanResult).where(ScanResult.scan_record_id == UUID(scan_record_id))
        if only_scan_result_id:
            q = q.where(ScanResult.id == UUID(only_scan_result_id))
        else:
            # 批量：按质量分降序（指标驱动筛选的排序键，旧数据无分排后）、
            # 同分按 24h 成交额——控制 LLM 成本；单币重分析不受限。
            # 配额走筛选参数组（scan_filters > env > 默认，2026-09-28）
            quota = resolve_scan_filters(cfg).ai_max_per_scan
            # 连损降速（docs/24 A3 软熔断）：启用自动交易的账号当日连损任一 ≥2 →
            # 批量分析配额减半——连损趋势日少开新分析、同步降 LLM 成本与开单频率；
            # 手动单币分析（only_scan_result_id 非空）不受影响。账号/连损解析
            # 失败不阻断扫描（按无连损处理）
            try:
                losses = 0
                for acc in auto_trade_accounts(db):
                    losses = max(losses, _consecutive_losses_today(db, acc.key, acc.testnet))
            except Exception as e:
                logger.warning("连损查询失败，批量分析配额不降速: %s", e)
                losses = 0
            if losses >= 2:
                derated = max(1, quota // 2)
                logger.info(
                    "连损 %d 笔降速：批量分析配额 %d → %d（扫描 #%s）",
                    losses, quota, derated, scan_record_id,
                )
                quota = derated
            q = q.order_by(
                ScanResult.quality_score.desc().nulls_last(),
                ScanResult.volume_24h.desc(),
            ).limit(quota)
        results = db.execute(q).scalars().all()
        if not results:
            logger.info("无可分析的扫描结果 #%s", scan_record_id)
            close_task(task_id, "skipped", summary="无可分析的扫描结果")
            return

        # 影子 oi_conf 打标（docs/14 CARD-02，AI 侧）：仅定时扫描批量路径——
        # 手动扫描/手动单币重分析用户在场，不进观察总体（与扫描侧仅 scheduled
        # 口径一致）。候选逐币拉 OI 快照 → 有 24h 前值才分桶落标；影子异常
        # 全吞（docs/14 铁律），不影响分析分发
        if only_scan_result_id is None:
            try:
                rec = db.get(ScanRecord, UUID(scan_record_id))
                if rec is not None and rec.scan_type == "scheduled":
                    tag_oi_conf(list(results))
            except Exception as e:
                logger.debug("oi_conf 影子打标失败（忽略）: %s", e)

        force = only_scan_result_id is not None
        jobs = group(
            run_ai_analysis_single.s(str(r.id), scan_record_id, user_input, force)
            for r in results
        )
        jobs.apply_async()
        logger.info("已分发 %d 个 AI 分析子任务 #%s", len(results), scan_record_id)
        close_task(
            task_id, "completed",
            summary=f"已分发 {len(results)} 个分析子任务"
                    + ("（手动单币重分析）" if force else f"（扫描 #{scan_record_id[:8]}）"),
            detail={"scan_record_id": scan_record_id, "dispatched": len(results), "force": force},
        )
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("AI 分析任务分发失败: %s", e)
        # 瞬时故障（DB 断连等）自动重试：子任务幂等（闸门/指纹/沿用短路），重发无副作用
        raise self.retry(countdown=30)
    finally:
        db.close()


@celery_app.task(name="app.tasks.ai_tasks.run_ai_analysis_single", bind=True, max_retries=2)
def run_ai_analysis_single(
    self, scan_result_id: str, scan_record_id: str,
    user_input: Optional[str] = None, force: bool = False,
):
    """单币种 AI 分析子任务：闸门 → 缓存 → LLM(带校验回炉) → 落库"""
    sem = _sem_acquire()
    db = SessionLocal()
    try:
        r = db.get(ScanResult, UUID(scan_result_id))
        if r is None:
            return
        cfg = db.get(SystemConfig, 1)
        if not cfg or not settings.AI_API_KEY:
            return
        if not cfg.ai_analysis_enabled and not force:
            # 「筛选后自动 AI 分析」开关只拦批量自动分析（与父任务闸门口径一致）；
            # 手动单币重分析（force = only_scan_result_id 非空）不受限。
            # 此前这里静默 return 且不写进度事件 → 前端「等待分析任务开始」卡死
            logger.info("筛选后自动 AI 分析开关关闭，批量分析子任务跳过 %s", r.symbol)
            return

        strategy_prompt = (
            cfg.strategy_prompt
            if cfg.strategy_prompt_enabled and cfg.strategy_prompt
            else None
        )
        # TP2 分批止盈开关（2026-10-08）：关闭时两管线提示词剔除止盈二 + 输出硬清零；
        # 定义在指纹缓存沿用（_copy）之前——沿用路径同样需要清零 TP2
        _tp2_on = bool(cfg.tp2_enabled)
        signal = {
            "symbol": r.symbol,
            "signal_type": r.signal_type,
            "current_price": float(r.current_price),
            "breakout_pct": float(r.breakout_pct),
            "pattern": r.pattern,
            "signal_reason": r.signal_reason,
            "touch_ref": r.touch_ref,  # 回踩触点（docs/22），无触点信号为 None
            "position": r.position,
            "volume_type": r.volume_type,
            "volume": float(r.volume),
            "volume_24h": float(r.volume_24h),
        }
        # 战法库（2026-10-02）：装载活动战法 → 渲染判定块注入用户消息 + 白名单
        # 随 signal 进 risk_guard 闸门（防 LLM 编造战法名）。2026-10-03 删环境
        # 切换：传 None = 装载全部活动战法（同一分析双网各开一单，scope 取并集）；
        # 装载失败降级不阻断
        from app.services import tactics_match
        _tactics = tactics_match.load_active_tactics(None)
        tactics_block = tactics_match.render_tactics_block(_tactics)
        signal["tactic_names"] = tactics_match.allowed_names(_tactics)
        fp = compute_fingerprint(signal)
        logger.info(
            "AI 分析开始: %s strength=%s fp=%s", r.symbol, r.strength, fp[:8]
        )
        # 进度事件：start（前端流式展示分析过程）
        ai_progress.push_start(r.id, r.symbol, "agent")

        # ── 程序侧 skip 闸门已全部移除（2026-09-15 拍板）：强度/陈旧/振幅熔断不再拦截，
        #    选中的币种一律交真实 AI 分析，跳过与否由 AI 自己决定 ──
        pool = ExchangePool()
        klines = pool.get_klines(r.symbol, cfg.kline_interval, 500)
        if len(klines) < 500:
            # K线不足500根（止盈锚定/回退的前提是查满500根窗口）：强刷重取一次；
            # 仍不足则为新上市合约的全部历史，用可用K线继续
            klines = pool.get_klines(r.symbol, cfg.kline_interval, 500, force_refresh=True)
            logger.info("AI 分析 %s K线不足500根，强刷重取后 %d 根", r.symbol, len(klines))

        # 分析时刻最新价（未收盘K线的现价）：信号里的 current_price 是扫描时价格，
        # 扫描到分析之间可能已明显移动——所有锚点换算、入场价校验都以最新价为基准
        if klines:
            signal["current_price"] = float(klines[-1][4])
        # 4) 指纹缓存：TTL 内同指纹沿用旧结论（手动重分析不短路）；
        #    价格区已失效（现价越过原止盈一/止损）的旧结论不沿用，重新分析
        if not force and settings.FINGERPRINT_TTL_MIN > 0:
            cached = _find_recent(db, r.symbol, fp, settings.FINGERPRINT_TTL_MIN)
            if cached:
                if _conclusion_still_valid(cached, signal["current_price"]):
                    note = "⏱️ 沿用近期同信号结论"
                    ai_progress.push(r.id, "gate", note=note)
                    _copy(db, r, r.symbol, cached, fp, note, tp2_enabled=_tp2_on)
                    ai_progress.push_done(r.id, cached.trade_decision, note)
                    return
                note = "⚠️ 同信号旧结论的价格区已失效（现价越过原止盈/止损），重新分析"
                ai_progress.push(r.id, "gate", note=note)

        # ── Stage 2：LLM 分析（P1 Agent 工具循环 / P0 单次调用）+ Risk Guard 校验 ──
        # LLM 长调用（数分钟）期间不持有 DB 连接——任务开头取出的连接可能中途被
        # 网络层掐断（pool_pre_ping 只在取出时探测），落库前换新会话并重取行
        db.close()
        db = SessionLocal()
        r = db.get(ScanResult, r.id)
        # 近期摆动结构（HH/LH/HL/LL，高低点各 20 个）随事实包给 AI，与图表标注同源；
        # 分析时刻现算（现价已更新），锚点解析（ai_agent）与风控锚定共用
        signal["recent_swings"] = recent_swings(klines, order=cfg.swing_order, n=20)
        # 4h 高周期 K 线（大方向背景注入，300 根）；失败只降级为无 4h 行，不中断分析
        try:
            htf_klines = pool.get_klines(r.symbol, "4h", 300)  # 300 根：EMA144 收敛残留 ~12%（docs/17 §6）
        except Exception as e:
            htf_klines = None
            logger.warning("AI 分析 %s 获取4h K线失败，跳过4h背景: %s", r.symbol, e)
        # 市场环境（资金费率/大盘/恐贪）不再预取注入——保持数据契约干净，
        # Agent 管线由模型按需调用工具自行获取
        # 止损金额口径（2026-09-27）：与开仓引擎同源的预算模式/金额/文案，
        # 传给两管线统一提示词与仓位复算（risk_budget.resolve）
        from app.services.risk_budget import resolve_risk_budget
        _risk_mode, _risk_amount, _risk_label = resolve_risk_budget(cfg)
        # 止损宽度硬上限透传（2026-09-29）：max_stop_pct 补丁参数注入门槛块，
        # AI 定止损前自验不超限，避免"AI 建议开单→程序宽度闸弃单"的无效调用
        _stop_cap = float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None
        # 风控参数上页面（2026-10-02）：RR 铁律 / BTC 急跌阈值按 cfg 覆盖优先、
        # NULL 回落 env，透传进两管线（提示词 + Risk Guard 同源）；
        # crash_pct 仅 P0 单次调用管线消费（Agent 管线不预注入市场环境，
        # 由模型按需调工具，急跌硬闸在开仓引擎侧按 cfg 生效）
        # 风险比例单一来源（2026-10-02 复查）：pct 模式下 resolver 的 amount 即
        # 生效比例（cfg.risk_pct ?? RISK_BUDGET_PCT），直接派生保证提示词 label
        # 与 Risk Guard 仓位复算同口径（TRADING_RISK_PCT 仅开仓引擎 est_risk 用，
        # 两 env 默认同 3.0）；fixed 模式 amount 是 USDT 实额，不传 risk_pct
        _rr_min = (
            float(cfg.ai_rr_min) if cfg.ai_rr_min is not None
            else float(settings.AI_RR_MIN)
        )
        _risk_pct = _risk_amount if _risk_mode == "pct" else None
        _crash_pct = (
            float(cfg.btc_crash_pause_pct) if cfg.btc_crash_pause_pct is not None
            else None
        )
        # 管线选择：批量（每小时扫描自动触发/手动全量）固定走 P0 单次调用管线，
        # 不受「AI 管线」开关影响；仅手动单币重分析（force）跟随开关走 Agent 管线
        if cfg.ai_pipeline_enabled and force:
            def _cb(ev: dict) -> None:
                ai_progress.push(r.id, ev.pop("t"), **ev)

            ai_result = analyze_coin_agent(
                signal, klines,
                strategy_prompt=strategy_prompt,
                user_input=user_input,
                pool=pool,
                progress_cb=_cb,
                htf_klines=htf_klines,
                risk_mode=_risk_mode,
                risk_amount=_risk_amount,
                risk_label=_risk_label,
                stop_cap_pct=_stop_cap,
                rr_min=_rr_min,
                risk_pct=_risk_pct,
                tactics_block=tactics_block,
                tp2_enabled=_tp2_on,
            )
        else:
            ai_progress.push(r.id, "round", round=0, tools=[], note="单次调用管线思考中…")
            ai_result = analyze_with_guard(
                signal, klines,
                strategy_prompt=strategy_prompt,
                user_input=user_input,
                htf_klines=htf_klines,
                risk_mode=_risk_mode,
                risk_amount=_risk_amount,
                risk_label=_risk_label,
                stop_cap_pct=_stop_cap,
                rr_min=_rr_min,
                risk_pct=_risk_pct,
                crash_pct=_crash_pct,
                interval=cfg.kline_interval,
                progress_id=r.id,  # 方案 B 轻量进度：流式增量计数推 "llm" 事件（≥2s 节流）
                tactics_block=tactics_block,
                tp2_enabled=_tp2_on,
            )
        # 双评委辩论复核（P2，默认关）：仅对 suggest 决策，只能 keep / veto；
        # 事实包增厚（docs/24 D3）——指标块 + 摆动结构摘要随 signal 传入（_build_judge_signal）
        if cfg.dual_judge_enabled:
            if ai_result.get("trade_decision") == "suggest":
                ai_progress.push(r.id, "gate", note="⚖️ 双评委辩论复核中…")
            ai_result = run_dual_judge(
                ai_result, _build_judge_signal(signal, klines, htf_klines),
            )
        # 4h 状态机硬约束兜底（2026-10-11 再重构 EMA 状态机 11 态：缠绕/拐头双向禁开，宏观交叉/恢复/排列锁逆势，过渡放行）：LLM 无视 prompt 时程序强制 skip
        ai_result = enforce_htf_constraint(ai_result, htf_klines)
        # 程序止损参考价（2026-10-11）：两管线（P0 单次/Agent）编排层统一注入，
        # 仅 suggest 落值（veto/强制 skip 的 direction=None 自然 NULL）——前端与
        # AI 报价对比展示（0.5×ATR 外扩锚），不改写 stop_loss 不参与下单
        if ai_result.get("trade_decision") == "suggest":
            ai_result["calc_stop_loss"] = calc_stop_reference(
                signal, klines, ai_result.get("direction")
            )
        # Narrator 已下线（提速）：submit_decision 的 reason 直接承载完整分点分析
        # （工具 schema 强约束"1. 2. 3."每条一行），落库即 analysis，省一次 LLM 往返
        # 待回踩判定（2026-09-15）：入场在现价回踩侧的建议仅展示标识、不下单
        ai_result["pullback_wait"] = is_pullback_wait(ai_result, signal.get("current_price"))
        _finish(db, r, r.symbol, ai_result, fp)
        ai_progress.push_done(
            r.id, ai_result.get("trade_decision"),
            str(ai_result.get("analysis") or "")[:80],
        )
    except Exception as e:
        # 网关超时（延迟波动剧烈，实测 130s~>240s 随机）：延迟自动重试，不推 error 事件。
        # 重试额度用尽（self.retry 抛 MaxRetriesExceeded）必须推 error 收尾，
        # 否则前端进度流永远停在「重试中」（2026-09-28 排查网关超时卡死）
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            if self.request.retries >= self.max_retries:
                logger.error("AI 分析 %s 网关超时且重试额度用尽，标记失败", scan_result_id)
                ai_progress.push(scan_result_id, "error", note="⏳ 网关超时，重试 2 次后仍失败")
            else:
                logger.warning(
                    "AI 分析 %s 网关超时，%ds 后自动重试（第 %d/2 次）",
                    scan_result_id, 90, self.request.retries + 1,
                )
                ai_progress.push(scan_result_id, "gate", note="⏳ 网关超时，90s 后自动重试")
                raise self.retry(countdown=90)
        logger.warning("AI 分析 %s 失败: %s", scan_result_id, e)
        ai_progress.push(scan_result_id, "error", note=str(e)[:200])
        try:
            db.rollback()
        except Exception:
            pass  # 连接已死时 rollback 也会抛，吞掉以保住原始错误信息
    finally:
        db.close()
        _sem_release(sem)


def _conclusion_still_valid(a: AIAnalysis, current_price: float) -> bool:
    """沿用旧结论前校验价格区仍有效：现价须仍在该方向的 止损~止盈一 区间内。

    价格已越过原 TP1/SL 的旧结论没有参考意义（如价格已到止盈位还提示做多）——
    不再沿用，落入下方正常 LLM 重分析（以当前价重新锚定）。skip 结论或价格
    数据不全时不拦（无失效可言）。
    """
    if a.trade_decision != "suggest" or not a.direction:
        return True
    try:
        price, sl, tp1 = (
            float(current_price), float(a.stop_loss or 0), float(a.take_profit_1 or 0),
        )
    except (TypeError, ValueError):
        return True
    if price <= 0 or sl <= 0 or tp1 <= 0:
        return True
    if a.direction == "long":
        return sl < price < tp1
    return sl > price > tp1


def _find_recent(db, symbol: str, fp: str, ttl_min: int) -> Optional[AIAnalysis]:
    """TTL 内同 symbol 同指纹的最近结论"""
    cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
    return db.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.symbol == symbol,
            AIAnalysis.fingerprint == fp,
            AIAnalysis.created_at >= cutoff,
            AIAnalysis.trade_decision.isnot(None),
        )
        .order_by(AIAnalysis.created_at.desc())
    ).scalars().first()


def _copy(db, r: ScanResult, symbol: str, src: AIAnalysis, fp: str, note: str,
          tp2_enabled: bool = True) -> None:
    """沿用旧结论：复制到新 scan_result_id（analysis 加沿用说明）。

    必须提交：前端行级轮询以"分析记录出现"为完成标志，不提交会导致轮询挂满超时。
    tp2_enabled（2026-10-08）：TP2 分批关闭时沿用记录同样清零 take_profit_2
    （旧结论可能带 TP2 价，落库口径须与当前出场规则一致）。
    """
    result = {
        "trade_decision": src.trade_decision,
        "skip_reason": src.skip_reason,
        "direction": src.direction,
        "trade_type": src.trade_type,
        "analysis": (note + "\n" + (src.analysis or "")).strip(),
        "entry_price": float(src.entry_price or 0),
        "stop_loss": float(src.stop_loss or 0),
        # 程序止损参考价（2026-10-11）：沿用结论同步带出（源行 skip/旧行 NULL 照抄）
        "calc_stop_loss": (
            float(src.calc_stop_loss) if src.calc_stop_loss is not None else None
        ),
        "take_profit_1": float(src.take_profit_1 or 0),
        "take_profit_2": float(src.take_profit_2 or 0) if tp2_enabled else 0,
        "risk_reward_ratio": float(src.risk_reward_ratio or 0),
        "position_pct": float(src.position_pct or 0),
        "recommendation": float(src.recommendation or 0),
        # 双入场策略（2026-10-10-5）：沿用结论保留胜出方与策略二审计字段
        "entry_strategy": src.entry_strategy,
        "strategy2_decision": src.strategy2_decision,
        "strategy2_reason": src.strategy2_reason,
        "pullback_wait": bool(src.pullback_wait),
    }
    _finish(db, r, symbol, result, fp)
    logger.info("AI 结论沿用: %s ← %s", symbol, src.id)


def _finish(db, r: ScanResult, symbol: str, ai_result: dict, fp: str, commit: bool = True):
    row = _upsert_ai_analysis(db, r.id, symbol, ai_result, fingerprint=fp)
    if commit:
        db.commit()
        # P-07 虚拟账本 AI 标注回填（N-02 校准数据）：提交后回填账本行，
        # 失败吞掉（attach_ai 自吞异常）——不影响 AI 主链路
        try:
            from app.services.ledger import attach_ai
            attach_ai(r.id, ai_result.get("trade_decision"),
                      ai_result.get("recommendation"), db=db)
        except Exception as e:
            logger.debug("账本 AI 标注回填失败（忽略）%s: %s", r.id, e)
        logger.info("AI 分析完成: %s (%s)", symbol, ai_result.get("trade_decision"))
        _dispatch_immediate_open(row)


def _dispatch_immediate_open(a: AIAnalysis) -> None:
    """唯一开仓通道：suggest 且过评分门槛的分析一落库即分发开仓任务（2026-09-14 起
    取消 :42 批量开仓，本分发失败即无兜底）。

    只做分发不做开仓（闸门全在 open_trade_for_analysis → try_open_for_analysis 里）；
    静默失败不重试——等该币下次信号重新分析再触发。
    """
    if a.trade_decision != "suggest":
        return
    # 待回踩单照常分发（2026-09-18）：回踩距离 ≤ pullback_market_pct 容差时
    # 市价入场、超容差在 _attempt_open 偏离闸门拦下——不再一刀切不下单
    # 开关以 DB 为运行时权威（2026-09-20 由 env 迁移进 system_config）
    # 评分门槛（2026-10-02 上页面）：cfg.min_recommendation 覆盖时优先，
    # NULL 回落 env——门槛检查需 cfg 行，故移入 db 块内
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, 1)
        # 双网独立开关（2026-10-03）：任一网开启即分发，具体扇出哪些账号
        # 由 open_trade_for_analysis 按 auto_trade_accounts 逐网判定
        if cfg is None or not (cfg.trading_enabled_testnet or cfg.trading_enabled_live):
            return
        min_rec = (
            float(cfg.min_recommendation) if cfg.min_recommendation is not None
            else float(settings.TRADING_MIN_RECOMMENDATION)
        )
        if float(a.recommendation or 0) < min_rec:
            return
    finally:
        db.close()
    celery_app.send_task(
        "app.tasks.trade_tasks.open_trade_for_analysis", args=[str(a.id)]
    )
    logger.info("已分发即时开仓任务: %s（评分 %s）", a.symbol, a.recommendation)


def _upsert_ai_analysis(
    db, scan_result_id: UUID, symbol: str, ai_result: dict,
    fingerprint: Optional[str] = None,
):
    """latest-wins：有则更新，无则新建；返回落库行（供即时开仓分发取 id）。

    保护性摘钩（2026-10-11，ALTUSDT 案例）：命中已存在且被 trade_records/
    trade_reviews 引用的行时不再原地覆盖（会把「已开单」的结论静默改写——
    交易记录 ai_snapshot 冻住开单时三价，AI 卡片却显示新结论，看卡片的人
    被误导），旧行摘钩（scan_result_id=None，与手动单币重分析同语义），
    新结论另起新行照常走开仓闸门。"""
    fields = {
        "trade_decision": ai_result.get("trade_decision"),
        "skip_reason": ai_result.get("skip_reason"),
        "direction": ai_result.get("direction"),
        "trade_type": ai_result.get("trade_type"),
        "analysis": ai_result.get("analysis"),
        "entry_price": ai_result.get("entry_price"),
        "stop_loss": ai_result.get("stop_loss"),
        # 程序止损参考价（2026-10-11）：0.5×ATR 外扩，仅前端对比展示不下单
        "calc_stop_loss": ai_result.get("calc_stop_loss"),
        "take_profit_1": ai_result.get("take_profit_1"),
        "take_profit_2": ai_result.get("take_profit_2"),
        "risk_reward_ratio": ai_result.get("risk_reward_ratio"),
        "position_pct": ai_result.get("position_pct"),
        "recommendation": ai_result.get("recommendation"),
        # 止损锚类型（docs/23）：suggest 时由 risk_guard fixed dict 保证有值
        # （None→structure），skip 行为 None（旧行同样按 structure 理解）
        "stop_anchor": ai_result.get("stop_anchor"),
        # 规则版本快照（docs/24 A0）：复盘统计按版本分组，跨版本不混谈
        "rule_version": settings.RULE_VERSION,
        # 双评委 veto 原始建议快照（docs/24 A5a）：仅 veto 强制 skip 携带；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "veto_original": ai_result.get("veto_original"),
        # 战法命中标注（2026-10-02）：白名单闸门后的值（清单外已清空）；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "tactic_name": ai_result.get("tactic_name"),
        "tactic_reason": ai_result.get("tactic_reason"),
        # 4h 排列强度（2026-10-10 五次拍板）：enforce 阶段 stash，复盘维度分组用；
        # latest-wins 重分析后 None 覆盖旧值，不残留
        "htf_phase": ai_result.get("htf_phase"),
        # 止盈模式（2026-10-10，docs/19 §3.4）：risk_guard fixed dict 保证 suggest
        # 有值（None→ladder）；latest-wins 重分析后覆盖旧值
        "tp_mode": ai_result.get("tp_mode"),
        # 双入场策略（2026-10-10-5，docs/20）：胜出方与策略二审计字段落库
        # （2026-10-11 建列——前端分区展示结论徽标取结构化字段，不依赖文本解析）
        "entry_strategy": ai_result.get("entry_strategy"),
        "strategy2_decision": ai_result.get("strategy2_decision"),
        "strategy2_reason": ai_result.get("strategy2_reason"),
        "pullback_wait": bool(ai_result.get("pullback_wait")),
        # 待回踩结论在分发前就被跳过（_dispatch_immediate_open），不会进入开仓闸门，
        # 未开单原因在此预置；其余结论的原因由开仓闸门拦截时写入
        "open_block_reason": (
            "AI 建议等回踩至入场价，仅展示待回踩标识、不下单"
            if bool(ai_result.get("pullback_wait")) else None
        ),
        "fingerprint": fingerprint,
        "stage_trace": ai_result.get("stage_trace"),
    }
    existing = db.execute(
        select(AIAnalysis).where(AIAnalysis.scan_result_id == scan_result_id)
    ).scalars().first()

    if existing:
        # 引用检查（轻查询各 limit 1）：开单/复盘引用任一存在即视为已消费
        referenced = (
            db.execute(
                select(TradeRecord.id)
                .where(TradeRecord.ai_analysis_id == existing.id).limit(1)
            ).scalar() is not None
            or db.execute(
                select(TradeReview.id)
                .where(TradeReview.ai_analysis_id == existing.id).limit(1)
            ).scalar() is not None
        )
        if referenced:
            # 摘钩保留（先 flush 保证 UPDATE 先于 INSERT 发出），新结论另起新行
            existing.scan_result_id = None
            db.flush()
            row = AIAnalysis(scan_result_id=scan_result_id, symbol=symbol, **fields)
            db.add(row)
            return row
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing
    row = AIAnalysis(scan_result_id=scan_result_id, symbol=symbol, **fields)
    db.add(row)
    return row
