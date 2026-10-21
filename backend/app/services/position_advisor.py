"""在跑单 AI 持仓建议（docs/16）

两类自动触发（2026-09-24 起）：
- 浮亏触发：首次浮亏 / 3h 冷却到期 / 较上次诊断恶化 ≥0.3R
- 滞留触发：持仓 ≥24h 且从未触发 TP1（status 仍为 OPENED）——AI 评估继续
  持有的必要性（资金效率：占用一天未达第一目标），首诊后每 24h 复诊

持仓诊断：开单逻辑是否仍成立、走势是否走坏、建议动作
（hold 继续持有 / tighten 收紧止损 / reduce 减仓 / exit 离场）。
模式复用 ai_attribution（事实包 JSON + 单次 LLM + 结构化输出 + 词表校验），
结论落库 trade_records.ai_advice 并写 AI_ADVICE 事件；滞留诊断每次推送飞书
卡片（浮盈亏 + AI 结果，24h 一次不刷屏），浮亏诊断仅 action=exit 推送
（同单连续 exit 不重复刷屏）。

N-17（2026-10-08，批次②）：tighten 执行闭环——AI 判收紧时程序按规则预计算
候选止损价（tighten_candidate，AI 不报价），守卫全过即自动撤旧挂新
（_auto_tighten，降风险单向：新 SL 恒优于旧），结果卡片飞书知会；
exit/reduce 仍为人工动作（卡片建议 + 按钮），自动平仓红线不动。

双网分析所有权（2026-10-08 用户拍板）：正式网主导诊断，测试网跟随——
- 触发侧（Rule A）：测试网单存在在跑正式网孪生（同 ai_analysis_id）→
  不独立派发诊断（has_running_live_twin，巡检与事件流两处派发点共用）；
- 执行侧（Rule B）：正式网建议落库后镜像到在跑测试网孪生
  （_mirror_advice_to_testnet）：建议落库带 mirror="live" 标记，tighten 由
  孪生按自身数据复算候选价再走守卫执行，exit/reduce 孪生同推建议卡
  （各网平各网仓），hold/stale 只落库。单向 live→testnet，不回镜像。

调度约束：LLM 调用（单笔 10~30s）只在 Redis 开仓锁外执行——结算巡检
每小时整点派发 celery 子任务、手动走 API 同步调用，绝不进巡检锁内。

AI 只做判断不越权：程序算事实（MFE/MAE/入场位置/指标状态），建议仅
结构性描述、禁止输出具体委托价，且永不自动执行平仓（沿用项目原则）。
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.system_config import SystemConfig
from app.models.trade import TradeEvent, TradeRecord
from app.services.event_executor import remaining_qty
from app.services.llm_client import current_model, get_client, parse_llm_json

logger = logging.getLogger(__name__)

ADVICE_COOLDOWN_HOURS = 3   # 浮亏诊断两次间隔
ADVICE_WORSEN_R = 0.3       # 冷却期内全程盈亏较上次诊断恶化 ≥0.3R → 提前刷新
                            # （N-17 2026-10-08：0.5→0.3，JST 案例实证 0.5 线
                            # 允许先吃掉一半风险预算才重评，阴跌单恰好滑过）
PEAK_RETRACE_R = 0.6        # 浮盈回撤触发：峰值回撤 ≥0.6R → 诊断（2026-09-26）
STALE_HOLD_HOURS = 24       # 滞留触发：持仓 ≥24h 且从未触发 TP1
STALE_RECHECK_HOURS = 24    # 滞留复诊间隔（首诊后每 24h 再评估，直到 TP1/结算）

# ── N-17 自动收紧守卫参数（2026-10-08）────────────────────────────
TIGHTEN_CAND_LOW_N = 3      # 候选价参照：近 3 根已收盘 K 线极值
TIGHTEN_CAND_ATR_K = 0.25   # 极值外推缓冲（×ATR）：极值再让一根噪音的余量
TIGHTEN_NOISE_ATR = 0.3     # 噪音距离：新 SL 距现价 <0.3ATR 不收紧（必被扫）
TIGHTEN_MIN_IMPROVE_R = 0.1  # 改善 <0.1R 不动挂单（频繁小改单徒增摩擦）

ACTIONS = ("hold", "tighten", "reduce", "exit")

ADVICE_SYSTEM = """你是期货交易系统的持仓风控顾问。一笔在跑交易需要持仓诊断，\
触发原因见事实包的 diagnosis_trigger 字段：
- floating_loss：当前处于浮亏，判断浮亏是否在计划波动范围内；
- profit_retrace：全程盈亏曾达峰值后大幅回吐（峰值见事实包 peak_total_pnl），\
重点评估是否落袋/收紧保护，而非等回吐演变为浮亏；
- stale_no_tp1：持仓已超过 24 小时仍未触发第一档止盈，需重点评估继续持有的必要性——\
资金已被占用一天而未兑现第一目标，结合走势是否停滞（MFE 距离 TP1 的差距、动能、结构）\
判断是"接近突破前的蓄势"还是"逻辑失效的空耗"，不要仅因浮盈为正就默认继续持有。

判断与输出要求：
1. thesis_valid：开仓逻辑是否仍成立（方向依据、结构位、形态是否被破坏）
2. trend_broken：当前走势是否已走坏（结构破坏/趋势反转/动能衰竭迹象）
3. action 从以下枚举选择一个：
   - hold：开仓逻辑未破坏，浮亏在计划波动范围内 / 走势仍在向 TP1 推进，继续持有
   - tighten：逻辑动摇但未失效，建议收紧止损（只描述结构位，不给具体价格）
   - reduce：走势明显不利或长期停滞，建议先减仓降低风险
   - exit：开仓逻辑已破坏（或滞留且走势确认走坏），建议离场
4. 事实优先于推测：给定的 MFE/MAE/入场位置/指标状态是硬数据；浮亏本身不等于逻辑破坏。
5. 禁止输出具体委托价格，建议只做结构性描述（如"跌破开仓依据的摆动低点"）。
6. reason ≤120 字；detail ≤500 字（可选，展开分析）。
只输出 JSON：{"thesis_valid": bool, "trend_broken": bool, "action": "...", "reason": "...", "detail": "..."}\
不要输出其他内容。"""


def _risk_unit(rec) -> float:
    """1R 口径（T3，2026-10-21）：优先 TradeRecord.risk_unit（成交后初始风险），
    旧行/测试替身无该属性时回落 risk_amount。"""
    value = getattr(rec, "risk_unit", None)
    if value is None:
        value = getattr(rec, "risk_amount", None)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def advice_due(rec, total: float, now: datetime) -> bool:
    """自动诊断触发判定（docs/16 §3.1 + 2026-09-26 扩展）。

    rec 为 TradeRecord；total = 已实现 + 浮动（巡检刚写入的最新值）。
    浮亏：首次浮亏 / 3h 冷却到期 / 较上次诊断恶化 ≥0.3R（N-17：0.5→0.3）。
    浮盈回撤：峰值（peak_total_pnl）回撤 ≥0.6R（2026-09-26，修复浮盈回吐盲区；
    复用 3h 冷却防回撤平台期反复触发，诊断后基线切换为当前值由恶化规则接管）。
    滞留：持仓 ≥24h 且从未触发 TP1（status 仍 OPENED），首诊后每 24h 复诊。
    """
    if not settings.AI_API_KEY:
        return False
    last = rec.ai_advice_at
    risk = _risk_unit(rec)
    # ── 浮亏触发 ──
    if total < 0:
        if last is None:
            return True  # 首次浮亏
        if now - last >= _timedelta_hours(ADVICE_COOLDOWN_HOURS):
            return True  # 冷却到期
        base = float(rec.ai_advice_pnl or 0)
        if risk > 0 and base - total >= risk * ADVICE_WORSEN_R:  # 恶化提前刷新
            return True
    # ── 浮盈回撤触发（盈亏皆可，浮亏场景由上方分支覆盖）──
    if risk > 0:
        peak = float(rec.peak_total_pnl or 0)
        if peak - total >= risk * PEAK_RETRACE_R:
            if last is None or now - last >= _timedelta_hours(ADVICE_COOLDOWN_HOURS):
                return True
    # ── 滞留触发（盈亏皆可）：24h 未触发 TP1，每 24h 复诊 ──
    if rec.status == "OPENED" and rec.opened_at is not None:
        if now - rec.opened_at >= _timedelta_hours(STALE_HOLD_HOURS):
            if last is None or now - last >= _timedelta_hours(STALE_RECHECK_HOURS):
                return True
    return False


def is_stale_hold(rec, now=None) -> bool:
    """滞留判定：持仓 ≥24h 且从未触发 TP1（触发原因标注/推送分路由用）"""
    if now is None:
        now = datetime.utcnow()
    return (
        rec.status == "OPENED"
        and rec.opened_at is not None
        and (now - rec.opened_at) >= _timedelta_hours(STALE_HOLD_HOURS)
    )


_RUNNING_STATUSES = ("OPENED", "TP1_HIT", "TP2_HIT")


def has_running_live_twin(db: Session, rec) -> bool:
    """测试网单是否存在在跑正式网孪生（双网分析所有权守卫 Rule A，2026-10-08）。

    有 → 测试网不独立派发诊断，建议由正式网诊断镜像同步
    （_mirror_advice_to_testnet）；无（正式网未开/已平/熔断未跟）→ 测试网单
    走自己的触发管线。ai_analysis_id 为空（手动接管/兜底单）永不配对。
    派发点调用频率低（事件流 60s 节流 + 巡检每小时），单条 exists 查询可忽略。"""
    if rec.testnet is not True or not rec.ai_analysis_id:
        return False
    twin_id = db.execute(
        select(TradeRecord.id).where(
            TradeRecord.ai_analysis_id == rec.ai_analysis_id,
            TradeRecord.testnet == False,  # noqa: E712
            TradeRecord.status.in_(_RUNNING_STATUSES),
        ).limit(1)
    ).scalar()
    return twin_id is not None


def _timedelta_hours(h: float):
    from datetime import timedelta
    return timedelta(hours=h)


def build_facts(db: Session, rec) -> dict:
    """持仓诊断事实包：开单逻辑 + 当前持仓 + 程序计算的 K 线特征 + 近期K线。

    K 线经 ExchangePool 历史区间拉取（与交易复盘K线同源，开仓前 24 根 + 持仓
    至今），行情源不受交易环境影响。特征计算口径与 ai_attribution 一致。
    """
    from app.services.exchange_pool import ExchangePool
    from app.services.strategy.ema import analyze_ema
    from app.services.strategy.indicators import calc_atr

    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    snap = rec.ai_snapshot or {}
    opened = rec.opened_at
    now = datetime.utcnow()
    opened_ms = int(opened.replace(tzinfo=timezone.utc).timestamp() * 1000) if opened else 0

    entry = float(rec.entry_price or snap.get("entry_price") or 0)
    direction = rec.direction or ""
    held_hours = round((now - opened).total_seconds() / 3600, 1) if opened else None

    # 总根数 = 开仓前 24 + 持仓根数 + 1（当前未收盘根），上限 480
    held_bars = 0
    if opened:
        unit = interval[-1]
        n = int(interval[:-1] or 1)
        interval_ms = n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit]
        held_bars = int((now - opened).total_seconds() * 1000 / interval_ms) + 1
    bars = min(24 + max(held_bars, 1), 480)
    end_ms = int(now.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1
    klines = ExchangePool().get_klines_range(rec.symbol, interval, bars, end_ms) or []

    pre = [k for k in klines if int(k[0]) < opened_ms][-20:]
    held = [k for k in klines if int(k[0]) >= opened_ms]
    price = float(klines[-1][4]) if klines else entry

    mfe = mae = None
    if held and entry > 0:
        if direction == "long":
            mfe = round((max(float(k[2]) for k in held) / entry - 1) * 100, 2)
            mae = round((1 - min(float(k[3]) for k in held) / entry) * 100, 2)
        else:
            mfe = round((1 - min(float(k[3]) for k in held) / entry) * 100, 2)
            mae = round((max(float(k[2]) for k in held) / entry - 1) * 100, 2)
    entry_pos = None
    if pre and entry > 0:
        hi, lo = max(float(k[2]) for k in pre), min(float(k[3]) for k in pre)
        if hi > lo:
            entry_pos = round((entry - lo) / (hi - lo), 2)

    closed = klines[:-1] if len(klines) >= 2 else klines
    atr = calc_atr(closed) if closed else None
    atr_pct = round(atr / price * 100, 2) if atr and price > 0 else None
    try:
        ema_state = analyze_ema(closed) if closed else None
    except Exception:
        ema_state = None

    realized = float(rec.realized_pnl or 0)
    unrealized = float(rec.unrealized_pnl or 0)
    risk = _risk_unit(rec)
    total = realized + unrealized
    peak = float(rec.peak_total_pnl or 0)

    # 剩余仓位比例：与 event_executor.remaining_qty 同源——raw 计划分批量精确推导
    # （新行），旧行缺 raw 按 50/25/75 状态回退。2026-10-09 分批调整为 60/20/20 后
    # 不能再按状态硬编码（新行 TP1_HIT 剩 40%、TP2_HIT 剩 20%）
    qty_total = float(rec.qty or 0)
    remaining_ratio = (remaining_qty(rec) / qty_total) if qty_total > 0 else 1.0

    # 近期K线压缩为紧凑数组（时间戳+OHLCV），上限最近 72 根控制 prompt 体积
    recent = [
        [int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), round(float(k[5] or 0), 1)]
        for k in klines[-72:]
    ]

    # 触发原因路由（与 advice_due 同源）：滞留 > 浮盈回撤（峰值回撤 ≥0.6R）> 浮亏
    if is_stale_hold(rec):
        trigger = "stale_no_tp1"
    elif risk > 0 and peak > 0 and peak - total >= risk * PEAK_RETRACE_R:
        trigger = "profit_retrace"
    else:
        trigger = "floating_loss"

    return {
        "symbol": rec.symbol,
        "direction": direction,
        # 触发原因（AI 按场景调整判断侧重，见 ADVICE_SYSTEM）
        "diagnosis_trigger": trigger,
        "peak_total_pnl": round(peak, 6) if peak > 0 else None,
        "opening_thesis": {
            "trade_type": snap.get("trade_type"),
            "recommendation": snap.get("recommendation"),
            "analysis": (snap.get("analysis") or "")[:1500],
            "plan": {
                "entry": snap.get("entry_price"),
                "stop_loss": snap.get("stop_loss"),
                "take_profit_1": snap.get("take_profit_1"),
                "take_profit_2": snap.get("take_profit_2"),
            },
        },
        "position_now": {
            "entry_fill": float(rec.entry_price) if rec.entry_price else None,
            "current_price": price,
            "stop_loss_now": float(rec.stop_loss) if rec.stop_loss else None,
            "tp1": float(rec.tp1) if rec.tp1 else None,
            "tp2": float(rec.tp2) if rec.tp2 else None,
            "leverage": rec.leverage,
            "remaining_ratio": remaining_ratio,
            "tp1_hit": rec.status != "OPENED",  # false=第一档止盈从未触发（滞留判定依据）
            "risk_unit": risk or None,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 6),
            "pnl_pct_of_risk": round((realized + unrealized) / risk * 100, 1) if risk > 0 else None,
            "hold_hours": held_hours,
        },
        "kline_facts": {
            "entry_pos_in_20bar_range": entry_pos,  # 1=区间顶 0=区间底
            "mfe_pct": mfe,   # 持仓期间最大有利波动%
            "mae_pct": mae,   # 持仓期间最大不利波动%
            "ema_state": ema_state,
            "atr_pct": atr_pct,
            "atr_abs": round(atr, 8) if atr else None,  # N-17：绝对值供候选价规则计算
            "held_bars": len(held),
        },
        "klines_recent": recent,  # 升序 OHLCV，含当前未收盘根
    }


def tighten_candidate(facts: dict, rec) -> "float | None":
    """程序规则预计算收紧候选价（N-17 批次②；AI 只判方向，报价权在规则——
    prompt「禁止报价」约束不破坏）。任一守卫不满足返回 None（不执行不推送）。

    口径：long 候选 = min(近 3 根已收盘 K 线低点) − 0.25×ATR，short 对称；
    守卫：① 改善 ≥0.1R（价格差 × 剩余仓折 USDT 对比 risk_amount）；
    ② 距现价 ≥0.3×ATR（太贴必被噪音扫掉，JST 03:31 时距止损仅 ~0.5ATR）；
    ③ 方向正确（SL 在价格行进反侧，②蕴含但不显式判余量耗尽情形）。
    """
    kf = facts.get("kline_facts") or {}
    atr = kf.get("atr_abs")
    price = (facts.get("position_now") or {}).get("current_price")
    klines = facts.get("klines_recent") or []
    old_sl = float(rec.stop_loss or 0)
    risk = _risk_unit(rec)
    if not atr or not price or len(klines) < TIGHTEN_CAND_LOW_N + 1 \
            or old_sl <= 0 or risk <= 0:
        return None
    closed = klines[:-1][-TIGHTEN_CAND_LOW_N:]
    long = rec.direction == "long"
    ext = (min(float(k[3]) for k in closed) if long
           else max(float(k[2]) for k in closed))
    cand = (ext - atr * TIGHTEN_CAND_ATR_K if long
            else ext + atr * TIGHTEN_CAND_ATR_K)
    from app.services.event_executor import remaining_qty
    remaining = remaining_qty(rec)
    improve = (cand - old_sl) * remaining if long else (old_sl - cand) * remaining
    if improve < risk * TIGHTEN_MIN_IMPROVE_R:
        return None
    dist = price - cand if long else cand - price
    if dist < atr * TIGHTEN_NOISE_ATR:
        return None
    if long and cand >= price:
        return None
    if not long and cand <= price:
        return None
    return round(cand, 8)


def diagnose_trade(db: Session, rec) -> "dict | None":
    """单笔诊断：事实包 → LLM → 词表校验 → 落库（ai_advice + AI_ADVICE 事件）→ 推送。

    手动 API 与定时子任务共用。调用方须保证：rec 为在跑状态、AI_API_KEY 已配置。
    失败抛异常（调用方决定记日志/报错），不落半截数据。
    返回 None = 竞态丢弃：LLM 执行期间（1~18 分钟）仓位已平仓结算/被清理，
    结论陈旧不落库（2026-09-26 竞态守卫）。"""
    if not settings.AI_API_KEY:
        raise ValueError("AI 未配置（AI_API_KEY 为空）")
    client = get_client()
    facts = build_facts(db, rec)
    resp = client.chat.completions.create(
        model=current_model(),
        messages=[
            {"role": "system", "content": ADVICE_SYSTEM},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        temperature=0.2,
        max_tokens=32000,  # GLM 思考型：reasoning 计入配额，12000 被思考耗尽过（同 ai_analyzer 口径）
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    data = parse_llm_json(content)

    # 竞态守卫（2026-09-26）：LLM 返回耗时 1~18 分钟，期间 SL 可能已触发结算，
    # 事实包与结论均已陈旧 → 落库前重读状态，非在跑即丢弃（不落误导数据）
    try:
        db.refresh(rec)
    except Exception:
        logger.info("AI 持仓诊断丢弃：%s 记录已于诊断期间被清理", rec.symbol)
        return None
    if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
        logger.info("AI 持仓诊断丢弃：%s 已于诊断执行期间平仓结算（status=%s）",
                    rec.symbol, rec.status)
        return None

    action = data.get("action") if data.get("action") in ACTIONS else "hold"
    # N-17：tighten → 程序规则预计算候选止损价（AI 不报价），随建议落库
    cand = tighten_candidate(facts, rec) if action == "tighten" else None
    advice = {
        "thesis_valid": bool(data.get("thesis_valid")),
        "trend_broken": bool(data.get("trend_broken")),
        "action": action,
        "reason": str(data.get("reason") or "").strip()[:150],
        "detail": str(data.get("detail") or "").strip()[:800],
        "tighten_candidate": cand,
    }
    total = round(float(rec.realized_pnl or 0) + float(rec.unrealized_pnl or 0), 6)
    prev_action = (rec.ai_advice or {}).get("action")
    stale = is_stale_hold(rec)
    # 事件触发路由（与 build_facts facts.diagnosis_trigger 同源）
    _risk = _risk_unit(rec)
    _peak = float(rec.peak_total_pnl or 0)
    if stale:
        trigger_label = "stale_no_tp1"
    elif _risk > 0 and _peak > 0 and _peak - total >= _risk * PEAK_RETRACE_R:
        trigger_label = "profit_retrace"
    else:
        trigger_label = "floating_loss"

    rec.ai_advice = advice
    rec.ai_advice_at = datetime.utcnow()
    rec.ai_advice_pnl = total  # 恶化判定基线
    db.add(TradeEvent(trade_record_id=rec.id, event_type="AI_ADVICE", detail={
        **advice, "total_pnl": total, "trigger": trigger_label,
    }))
    db.commit()

    # ── N-17 通知/执行路由 ──────────────────────────────────────
    # tighten：结果通知制——守卫全过 → 自动撤旧挂新 + 结果卡片（通知制，
    # 「我已改，知会你」）；守卫未过 → 只落库不推送（噪音控制）。
    # exit/reduce：人工动作类——卡片建议（exit 带平仓按钮），同单连续同动作不重推。
    if stale:
        _notify_stale(rec, advice, total)
        # 滞留镜像只落库：滞留是各网自身持仓状态判定（孪生可能已 TP1），
        # 且 live 滞留分支本就不执行 tighten——镜像不得反向多执行
        _mirror_advice_to_testnet(db, rec, advice, execute=False)
        return advice
    if action == "tighten":
        if cand is not None:
            atr_abs = (facts.get("kline_facts") or {}).get("atr_abs") or 0
            _auto_tighten(db, rec, cand, atr_abs, advice)
        # cand=None（规则否决）→ 不推送
        _mirror_advice_to_testnet(db, rec, advice)
        return advice
    if action in ("exit", "reduce") and prev_action != action:
        _notify_advice(rec, advice, total)
    _mirror_advice_to_testnet(db, rec, advice)
    return advice


def _mirror_advice_to_testnet(db: Session, live_rec, advice: dict,
                              execute: bool = True) -> None:
    """正式网建议镜像到测试网孪生（双网分析所有权 Rule B，2026-10-08）。

    正式网（live_rec.testnet=False）建议落库后调用：同 ai_analysis_id 的在跑
    测试网孪生逐单——建议落库（mirror="live" 标记 + AI_ADVICE 事件 + 基线
    刷新），tighten 按孪生自身数据复算候选价（结构位同 K 线，entry/risk 各自
    适配）走 _auto_tighten 守卫执行（结果卡片自带「测试网」标注）；
    exit/reduce 孪生同推建议卡（各网平各网仓，prev_action 去重）；
    hold 只落库不推卡；execute=False（滞留触发）仅落库——滞留是各网自身
    持仓状态判定且 live 滞留分支不执行 tighten，镜像不得反向多执行。
    单向 live→testnet；每孪生异常隔离，失败仅记日志，绝不影响正式网
    主流程与其余孪生单。"""
    if live_rec.testnet or not live_rec.ai_analysis_id:
        return
    twins = db.execute(
        select(TradeRecord).where(
            TradeRecord.ai_analysis_id == live_rec.ai_analysis_id,
            TradeRecord.testnet == True,  # noqa: E712
            TradeRecord.status.in_(_RUNNING_STATUSES),
        )
    ).scalars().all()
    for twin in twins:
        try:
            db.refresh(twin)
            if twin.status not in _RUNNING_STATUSES:
                continue  # 事实包构建期间（秒级）孪生已平仓结算 → 丢弃
            prev_action = (twin.ai_advice or {}).get("action")
            advice_t = {**advice, "mirror": "live"}
            facts_t = None
            if advice_t.get("action") == "tighten":
                # 候选价按孪生自身数据复算（AI 结论复用，报价权仍在规则）
                facts_t = build_facts(db, twin)
                advice_t["tighten_candidate"] = tighten_candidate(facts_t, twin)
            total_t = round(float(twin.realized_pnl or 0)
                            + float(twin.unrealized_pnl or 0), 6)
            twin.ai_advice = advice_t
            twin.ai_advice_at = datetime.utcnow()
            twin.ai_advice_pnl = total_t  # 恶化判定基线随镜像刷新
            db.add(TradeEvent(trade_record_id=twin.id, event_type="AI_ADVICE",
                              detail={**advice_t, "total_pnl": total_t,
                                      "trigger": "mirror_live"}))
            db.commit()
            if not execute:
                continue  # 滞留镜像：只落库（孪生可能已 TP1，不得反向多执行）
            if advice_t.get("action") == "tighten":
                cand_t = advice_t.get("tighten_candidate")
                if cand_t is not None:
                    atr_t = ((facts_t.get("kline_facts") or {}).get("atr_abs") or 0)
                    _auto_tighten(db, twin, cand_t, atr_t, advice_t)
                # cand=None（孪生侧规则否决）→ 只落库不推送
            elif advice_t.get("action") in ("exit", "reduce") \
                    and prev_action != advice_t.get("action"):
                _notify_advice(twin, advice_t, total_t)
        except Exception:
            db.rollback()
            logger.exception("镜像建议到测试网孪生失败 %s（%s）",
                             twin.symbol, twin.account_key)


ACTION_LABEL = {
    "hold": "继续持有",
    "tighten": "收紧止损",
    "reduce": "建议减仓",
    "exit": "建议离场",
}


def _net_label(rec) -> str:
    """飞书卡片的网络标注（2026-10-04）：双网同符号同向开单时，凭卡片内容
    （币种/价格）无法区分是哪张网的单，误点平仓按钮会平错网"""
    return "测试网" if rec.testnet else "正式网"


def _notify_advice(rec, advice: dict, total: float) -> None:
    """exit/reduce 人工动作建议卡（N-17 改名泛化：AI 永不自动平仓/减仓——
    这两类仍走「建议+人工确认」）。失败仅记日志，调用方已保证同单不连续重复推送。

    卡片布局：header 红 → 全宽浮亏高亮行（金额/持仓时长）→
    四列价格栅格（方向/开仓价/现价/止损）→ AI 建议（动作加粗 +
    thesis/trend 标注）→ 理由正文 → 市价平仓按钮（仅 exit，feishu_bot 回调）→
    note 脚注。经自建应用发送；未配置则整体跳过。
    """
    try:
        from app.services.feishu_bot import configured, send_app_card
        if not configured():
            logger.info("飞书自建应用未配置，跳过持仓建议推送")
            return
        # 防御：total 可能被调用方传 Decimal（Numeric 列），统一转 float；
        # 卡片按钮 value 走 JSON 序列化，UUID 须转字符串
        total = float(total)
        trade_id = str(rec.id)
        side = "多" if rec.direction == "long" else "空"
        entry = float(rec.entry_price or 0)
        snap = rec.ai_snapshot or {}
        sl = snap.get("stop_loss")
        held_h = (
            round((datetime.utcnow() - rec.opened_at).total_seconds() / 3600, 1)
            if rec.opened_at else None
        )

        def _num(x):
            if x in (None, "", 0):
                return "—"
            try:
                return f"{float(x):g}"
            except (TypeError, ValueError):
                return str(x)

        held_txt = f" · 已持仓 {held_h}h" if held_h is not None else ""
        advice_line = (
            f"**{ACTION_LABEL.get(action_f, action_f) if (action_f := advice.get('action')) else '-'}**"
            f"（开仓逻辑{'成立' if advice.get('thesis_valid') else '已破坏'}"
            f" · 走势{'已走坏' if advice.get('trend_broken') else '未走坏'}）"
        )

        def _col(title: str, value: str) -> dict:
            return {"tag": "column", "width": "weighted", "weight": 1,
                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**{title}**\n{value}"}}]}

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": (f"⚠️ AI 持仓建议 · "
                                      f"{ACTION_LABEL.get(advice.get('action'), '建议')} · "
                                      f"{_net_label(rec)} · {rec.symbol}")},
                "template": "red",
            },
            "elements": [
                # 全宽浮盈亏高亮行：核心结论一眼可见（N-17：reduce 可发生在
                # 浮盈回吐早期，label/颜色随符号——原版硬编码「浮亏」红色）
                {"tag": "div", "text": {"tag": "lark_md", "content":
                 f"{_net_label(rec)} {side}单{held_txt}\n"
                 f"{'🔻 浮亏' if total < 0 else '📈 浮盈'}　"
                 f"<font color='{('red' if total < 0 else 'green')}'>"
                 f"{total:+.2f} USDT</font>"}},
                # 四列价格栅格（等宽，窄屏自动堆叠）
                {"tag": "column_set", "flex_mode": "stretch", "columns": [
                    _col("方向", side),
                    _col("开仓价", _num(entry)),
                    _col("现价", _num(snap.get("current_price"))),
                    _col("止损", _num(sl)),
                ]},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"🤖 **AI 建议**　{advice_line}"}},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": advice.get("reason") or "—"}},
            ],
        }
        # 交互按钮：仅 exit（自动平仓红线，reduce 涉及数量拆分留人工）；
        # 自建应用发的卡片才收得到回调；点击后后台平仓并发结果卡片
        # （feishu_bot._exec_close）。缺 ACTION_TOKEN 时不带按钮（纯展示）
        if advice.get("action") == "exit" and settings.FEISHU_ACTION_TOKEN:
            card["elements"] += [
                {"tag": "action", "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🛑 市价平仓"},
                    "type": "danger",
                    "value": {
                        "act": "close_market",
                        "token": settings.FEISHU_ACTION_TOKEN,
                        "trade_id": trade_id,
                    },
                }]},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": "点击「市价平仓」即以市价全平该单并回报结果 · 详情见交易记录"}]},
            ]
        send_app_card(card, testnet=bool(rec.testnet))
    except Exception as e:
        logger.warning("持仓建议飞书推送失败 %s: %s", rec.symbol, e)


def _auto_tighten(db: Session, rec, cand: float, atr: float, advice: dict) -> bool:
    """自动收紧止损执行（N-17 批次②，所有者 2026-10-08 决策放开）：
    AI 判 tighten 且规则候选价合法 → 账号锁内撤旧挂新 → 飞书结果卡片。

    降风险单向（新 SL 恒优于旧，与 TP1 后保本移损/ATR 跟进轨同域）；
    自动平仓/自动开仓红线不动。守卫链（任一不过放弃、只记日志）：
    ① 状态在跑 ② 幂等（SL 已等于候选）③ 账号凭据 ④ 噪音距离
    （实时价复验，诊断耗时 10~30s 行情可能已变）⑤ 改善足额
    ⑥ 全账号锁（与巡检/事件结算/手动平仓互斥，react_to_fill 同款）。
    失败不落库（旧 SL 由下一轮巡检 _rehang_lost_sl 兜底重挂）。
    返回是否实际执行。
    """
    from uuid import uuid4
    from redis import Redis
    from app.services.binance_trader import BinanceTrader
    from app.services.trade_locks import (
        OPEN_LOCK_TTL_S, acquire_open_lock, release_open_lock,
    )
    try:
        if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
            return False
        old_sl = float(rec.stop_loss or 0)
        if old_sl <= 0 or atr <= 0 or abs(old_sl - cand) < 1e-12:
            return False
        # 存量单 account_key 可能为 NULL：与下方锁键同款回退（docs/17 前旧数据）
        trader = BinanceTrader(
            rec.account_key or ("testnet-1" if rec.testnet else "live-1"))
        if not trader.configured:
            logger.info("自动收紧 %s 跳过：账号凭据未配置", rec.symbol)
            return False
        cand = trader.round_price(rec.symbol, cand)
        price = trader.price(rec.symbol)
        long = rec.direction == "long"
        dist = price - cand if long else cand - price
        if dist < atr * TIGHTEN_NOISE_ATR:
            logger.info("自动收紧 %s 放弃：新 SL %s 距现价 %s 不足 %.1f×ATR(%.6g)",
                        rec.symbol, cand, price, TIGHTEN_NOISE_ATR, atr)
            return False
        from app.services.event_executor import remaining_qty
        risk = _risk_unit(rec)
        remaining = remaining_qty(rec)
        improve = ((cand - old_sl) if long else (old_sl - cand)) * remaining
        if risk <= 0 or improve < risk * TIGHTEN_MIN_IMPROVE_R:
            logger.info("自动收紧 %s 放弃：改善 %.4fU 不足 %.1fR(%.4fU)",
                        rec.symbol, improve, TIGHTEN_MIN_IMPROVE_R,
                        risk * TIGHTEN_MIN_IMPROVE_R)
            return False
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        account = rec.account_key or ("testnet-1" if rec.testnet else "live-1")
        ident = f"tighten-{uuid4().hex[:8]}"
        if not acquire_open_lock(r, ident, wait_s=5, ttl_s=OPEN_LOCK_TTL_S,
                                 account=account):
            logger.info("自动收紧 %s 放弃：开仓锁被占用（下一轮触发重试）", rec.symbol)
            return False
        try:
            db.refresh(rec)
            if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
                return False
            # open_algo_orders 返回币安原始 dict 列表（algoId/type/side/...），
            # 取 algoId 集合供 _replace_sl 判断旧单是否在挂（dict 不可哈希，
            # 直接 set() 会 TypeError——2026-10-08 LUNA2USDT 首跑实锤）
            open_ids = {int(o["algoId"]) for o in trader.open_algo_orders(rec.symbol)
                        if o.get("algoId") is not None}
            from app.services.trade_engine import _replace_sl
            _replace_sl(db, trader, rec, open_ids, cand, reason="ai_auto_tighten")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.exception("自动收紧 %s 失败（旧 SL %s 由巡检兜底重挂）",
                             rec.symbol, old_sl)
            _notify_tighten_result(rec, old_sl, cand, price, advice, ok=False, err=str(e))
            return False
        finally:
            release_open_lock(r, ident, account=account)
        _notify_tighten_result(rec, old_sl, cand, price, advice, ok=True)
        return True
    except Exception:
        logger.exception("自动收紧 %s 异常", rec.symbol)
        return False


def _notify_tighten_result(rec, old_sl: float, new_sl: float, price: float,
                           advice: dict, ok: bool, err: str = None) -> None:
    """自动收紧结果卡片（N-17 通知制：tighten 不再推「建议」，只推「结果」）。
    成功 orange 回执 / 失败红色告警，均无按钮。失败仅记日志不抛。"""
    try:
        from app.services.feishu_bot import configured, send_app_card
        if not configured():
            return
        side = "多" if rec.direction == "long" else "空"
        entry = float(rec.entry_price or 0)
        risk = _risk_unit(rec)
        from app.services.event_executor import remaining_qty
        remaining = remaining_qty(rec)
        loss_new = (entry - new_sl) * remaining if rec.direction == "long" \
            else (new_sl - entry) * remaining
        loss_old = (entry - old_sl) * remaining if rec.direction == "long" \
            else (old_sl - entry) * remaining
        r_unit = (loss_old - loss_new) / risk if risk > 0 else 0.0
        held_h = (
            round((datetime.utcnow() - rec.opened_at).total_seconds() / 3600, 1)
            if rec.opened_at else None
        )
        held_txt = f"已持仓 {held_h}h · " if held_h is not None else ""

        def _num(x) -> str:
            try:
                return f"{float(x):g}"
            except (TypeError, ValueError):
                return "—"

        if ok:
            header = f"🔒 止损已自动收紧 · {_net_label(rec)} · {rec.symbol}"
            template = "orange"
            body = (
                f"{_net_label(rec)} {side}单 · {held_txt}现价 {_num(price)}\n"
                f"旧止损 {_num(old_sl)} → **新止损 {_num(new_sl)}**"
                f"（改善 {r_unit:+.2f}R）\n"
                f"扫损 ≈ <font color='green'>{(-loss_new):+.2f}U"
                f"（{(-loss_new / risk):+.2f}R）</font>"
                f"　原止损为 {(-loss_old):+.2f}U（{(-loss_old / risk):+.2f}R）\n"
                f"🤖 AI 判定：收紧止损（开仓逻辑"
                f"{'成立' if advice.get('thesis_valid') else '已动摇'}）\n"
                f"{advice.get('reason') or ''}"
            )
            note = "程序按规则价自动改挂（新 SL 恒优于旧）· 自动平仓仍需人工确认"
        else:
            header = f"❌ 止损自动收紧失败 · {_net_label(rec)} · {rec.symbol}"
            template = "red"
            body = (
                f"{_net_label(rec)} {side}单 · 目标止损 {_num(new_sl)}\n"
                f"旧止损 {_num(old_sl)} 未变，下一轮巡检将兜底重挂\n"
                f"原因：{err or '未知'}"
            )
            note = "仓位保护未中断，可忽略或在交易记录中人工处理"
        send_app_card({
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
            ],
        }, testnet=bool(rec.testnet))
    except Exception as e:
        logger.warning("自动收紧结果卡片推送失败 %s: %s", rec.symbol, e)


def _notify_stale(rec, advice: dict, total: float) -> None:
    """滞留诊断（持仓 ≥24h 未触发 TP1）→ 飞书卡片：浮盈亏 + AI 分析结果。

    每次诊断都推送（24h 一次不刷屏）；action=exit 时附带市价平仓按钮
    （feishu_bot 回调执行，需 FEISHU_ACTION_TOKEN）。失败仅记日志。
    """
    try:
        from app.services.feishu_bot import configured, send_app_card
        if not configured():
            logger.info("飞书自建应用未配置，跳过滞留持仓建议推送")
            return
        total = float(total)
        trade_id = str(rec.id)
        side = "多" if rec.direction == "long" else "空"
        snap = rec.ai_snapshot or {}
        held_h = (
            round((datetime.utcnow() - rec.opened_at).total_seconds() / 3600, 1)
            if rec.opened_at else None
        )
        action_f = advice.get("action")
        advice_line = (
            f"**{ACTION_LABEL.get(action_f, action_f) if action_f else '-'}**"
            f"（开仓逻辑{'成立' if advice.get('thesis_valid') else '已破坏'}"
            f" · 走势{'已走坏' if advice.get('trend_broken') else '未走坏'}）"
        )

        def _num(x):
            if x in (None, "", 0):
                return "—"
            try:
                return f"{float(x):g}"
            except (TypeError, ValueError):
                return str(x)

        # 浮盈绿 / 浮亏红 / 持平默认
        if total > 0:
            pnl_md = f"🟢 **浮盈　<font color='success'>{total:+.2f} USDT</font>**"
        elif total < 0:
            pnl_md = f"🔻 **浮亏　<font color='red'>{total:+.2f} USDT</font>**"
        else:
            pnl_md = f"**浮动盈亏　{total:+.2f} USDT**"

        def _col(title: str, value: str) -> dict:
            return {"tag": "column", "width": "weighted", "weight": 1,
                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**{title}**\n{value}"}}]}

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"⏰ AI 持仓建议 · 持仓超24h未触发止盈 · {_net_label(rec)} · {rec.symbol}"},
                "template": "red" if action_f == "exit"
                            else ("orange" if action_f in ("reduce", "tighten") else "blue"),
            },
            "elements": [
                # 全宽浮动盈亏高亮行（浮盈/浮亏都展示）
                {"tag": "div", "text": {"tag": "lark_md", "content":
                 f"{_net_label(rec)} {side}单 · 已持仓 {held_h}h 未触发 TP1\n"
                 f"{pnl_md}"}},
                {"tag": "column_set", "flex_mode": "stretch", "columns": [
                    _col("方向", side),
                    _col("开仓价", _num(rec.entry_price)),
                    _col("现价", _num(snap.get("current_price"))),
                    _col("止盈一", _num(rec.tp1)),
                ]},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"🤖 **AI 建议**　{advice_line}"}},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": advice.get("reason") or "—"}},
            ],
        }
        # 仅 exit 带平仓按钮（hold/tighten/reduce 无可执行动作，纯展示）
        if action_f == "exit" and settings.FEISHU_ACTION_TOKEN:
            card["elements"] += [
                {"tag": "action", "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🛑 市价平仓"},
                    "type": "danger",
                    "value": {
                        "act": "close_market",
                        "token": settings.FEISHU_ACTION_TOKEN,
                        "trade_id": trade_id,
                    },
                }]},
            ]
        card["elements"].append({"tag": "note", "elements": [{"tag": "plain_text",
         "content": "持仓超24h未触发第一档止盈的定期复诊 · 详情见交易记录"}]})
        send_app_card(card, testnet=bool(rec.testnet))
    except Exception as e:
        logger.warning("滞留持仓建议飞书推送失败 %s: %s", rec.symbol, e)
