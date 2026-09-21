"""在跑单 AI 持仓建议（docs/16）

浮亏在跑单的持仓诊断：开单逻辑是否仍成立、走势是否走坏、建议动作
（hold 继续持有 / tighten 收紧止损 / reduce 减仓 / exit 离场）。
模式复用 ai_attribution（事实包 JSON + 单次 LLM + 结构化输出 + 词表校验），
结论落库 trade_records.ai_advice 并写 AI_ADVICE 事件；action=exit 时飞书推送
（同单连续 exit 不重复刷屏）。

调度约束：LLM 调用（单笔 10~30s）只在 Redis 开仓锁外执行——结算巡检
每小时整点派发 celery 子任务、手动走 API 同步调用，绝不进巡检锁内。

AI 只做判断不越权：程序算事实（MFE/MAE/入场位置/指标状态），建议仅
结构性描述、禁止输出具体委托价，且永不自动执行平仓（沿用项目原则）。
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.system_config import SystemConfig
from app.models.trade import TradeEvent
from app.services.llm_client import get_client

logger = logging.getLogger(__name__)

ADVICE_COOLDOWN_HOURS = 3   # 两次自动诊断最小间隔
ADVICE_WORSEN_R = 0.5       # 冷却期内全程盈亏较上次诊断恶化 ≥0.5R → 提前刷新

ACTIONS = ("hold", "tighten", "reduce", "exit")

ADVICE_SYSTEM = """你是期货交易系统的持仓风控顾问。一笔在跑交易当前处于浮亏，\
根据给出的事实（开单逻辑、当前持仓状态、K线特征、近期K线）做持仓诊断。

判断与输出要求：
1. thesis_valid：开仓逻辑是否仍成立（方向依据、关键位、形态是否被破坏）
2. trend_broken：当前走势是否已走坏（结构破坏/趋势反转/动能衰竭迹象）
3. action 从以下枚举选择一个：
   - hold：开仓逻辑未破坏，浮亏在计划波动范围内，继续持有
   - tighten：逻辑动摇但未失效，建议收紧止损（只描述结构位，不给具体价格）
   - reduce：走势明显不利，建议先减仓降低风险
   - exit：开仓逻辑已破坏，建议离场
4. 事实优先于推测：给定的 MFE/MAE/入场位置/指标状态是硬数据；浮亏本身不等于逻辑破坏。
5. 禁止输出具体委托价格，建议只做结构性描述（如"跌破开仓依据的摆动低点"）。
6. reason ≤120 字；detail ≤500 字（可选，展开分析）。
只输出 JSON：{"thesis_valid": bool, "trend_broken": bool, "action": "...", "reason": "...", "detail": "..."}\
不要输出其他内容。"""


def advice_due(rec, total: float, now: datetime) -> bool:
    """自动诊断触发判定（docs/16 §3.1）：浮亏 + 首次/冷却到期/恶化 ≥0.5R。

    rec 为 TradeRecord；total = 已实现 + 浮动（巡检刚写入的最新值）。
    """
    if not settings.AI_API_KEY:
        return False
    if total >= 0:
        return False
    last = rec.ai_advice_at
    if last is None:
        return True  # 首次浮亏
    if now - last >= _timedelta_hours(ADVICE_COOLDOWN_HOURS):
        return True  # 冷却到期
    base = float(rec.ai_advice_pnl or 0)
    risk = float(rec.risk_amount or 0)
    return risk > 0 and base - total >= risk * ADVICE_WORSEN_R  # 恶化提前刷新


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
    risk = float(rec.risk_amount or 0)

    # 剩余仓位比例：按状态推导（与分批止盈规则一致），避免逐笔查交易所
    remaining_ratio = {"OPENED": 1.0, "TP1_HIT": 0.25 if not rec.tp2 else 0.5, "TP2_HIT": 0.25}.get(
        rec.status, 1.0)

    # 近期K线压缩为紧凑数组（时间戳+OHLCV），上限最近 72 根控制 prompt 体积
    recent = [
        [int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), round(float(k[5] or 0), 1)]
        for k in klines[-72:]
    ]

    return {
        "symbol": rec.symbol,
        "direction": direction,
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
            "risk_amount": risk or None,
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
            "held_bars": len(held),
        },
        "klines_recent": recent,  # 升序 OHLCV，含当前未收盘根
    }


def diagnose_trade(db: Session, rec) -> dict:
    """单笔诊断：事实包 → LLM → 词表校验 → 落库（ai_advice + AI_ADVICE 事件）→ 推送。

    手动 API 与定时子任务共用。调用方须保证：rec 为在跑状态、AI_API_KEY 已配置。
    失败抛异常（调用方决定记日志/报错），不落半截数据。
    """
    if not settings.AI_API_KEY:
        raise ValueError("AI 未配置（AI_API_KEY 为空）")
    client = get_client()
    facts = build_facts(db, rec)
    resp = client.chat.completions.create(
        model=settings.AI_MODEL,
        messages=[
            {"role": "system", "content": ADVICE_SYSTEM},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        temperature=0.2,
        max_tokens=12000,  # GLM 思考型：reasoning tokens 计入配额（同 ai_attribution 口径）
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    data = json.loads(content)

    action = data.get("action") if data.get("action") in ACTIONS else "hold"
    advice = {
        "thesis_valid": bool(data.get("thesis_valid")),
        "trend_broken": bool(data.get("trend_broken")),
        "action": action,
        "reason": str(data.get("reason") or "").strip()[:150],
        "detail": str(data.get("detail") or "").strip()[:800],
    }
    total = round(float(rec.realized_pnl or 0) + float(rec.unrealized_pnl or 0), 6)
    prev_action = (rec.ai_advice or {}).get("action")

    rec.ai_advice = advice
    rec.ai_advice_at = datetime.utcnow()
    rec.ai_advice_pnl = total  # 恶化判定基线
    db.add(TradeEvent(trade_record_id=rec.id, event_type="AI_ADVICE", detail={
        **advice, "total_pnl": total,
    }))
    db.commit()

    if action == "exit" and prev_action != "exit":
        _notify_exit(rec, advice, total)
    return advice


ACTION_LABEL = {
    "hold": "继续持有",
    "tighten": "收紧止损",
    "reduce": "建议减仓",
    "exit": "建议离场",
}


def _notify_exit(rec, advice: dict, total: float) -> None:
    """建议离场 → 飞书卡片推送（失败仅记日志）。调用方已保证同单不连续重复推送。

    卡片布局对齐日报：header 按盈亏着色（浮亏红）→ 持仓概要 fields 两列栅格
    （币种方向/持有时长、开仓价/现价、浮亏金额/占比R）→ AI 建议（动作 +
    thesis/trend 标注）→ 理由正文 → 市价平仓按钮（自建应用交互卡片，
    feishu_bot 回调执行）。卡片失败降级纯文本保底。
    """
    try:
        from app.services.feishu_bot import configured as feishu_app_ready, send_app_card
        from app.services.notify import send_feishu_card, send_feishu_text
        risk = float(rec.risk_amount or 0)
        side = "多" if rec.direction == "long" else "空"
        entry = float(rec.entry_price or 0)
        snap = rec.ai_snapshot or {}
        tp1 = snap.get("tp1")
        sl = snap.get("stop_loss")
        held_h = (
            round((datetime.utcnow() - rec.opened_at).total_seconds() / 3600, 1)
            if rec.opened_at else None
        )
        r_mult = f"{total / risk:.2f}R" if risk > 0 else "-"

        def _num(x):
            if x in (None, "", 0):
                return "—"
            try:
                return f"{float(x):g}"
            except (TypeError, ValueError):
                return str(x)

        # 浮亏占比（相对止损金额），防御 risk<=0
        loss_pct = f"{abs(total) / risk * 100:.0f}%" if risk > 0 else "—"
        advice_line = (
            f"**{ACTION_LABEL.get(action_f, action_f) if (action_f := advice.get('action')) else '-'}**"
            f"（开仓逻辑{'成立' if advice.get('thesis_valid') else '已破坏'}"
            f" · 走势{'已走坏' if advice.get('trend_broken') else '未走坏'}）"
        )

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"⚠️ AI 持仓建议：建议离场 · {rec.symbol}"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "**📋 持仓概要**"}},
                {"tag": "div", "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**方向**　{side}{f' · 已持仓 {held_h}h' if held_h is not None else ''}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**浮亏**　<font color='red'>{total:+.2f} USDT（{r_mult} / 风险额的 {loss_pct}）</font>"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**开仓价**　{_num(entry)}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**现价**　{_num(snap.get('current_price'))}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**止损**　{_num(sl)}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**TP1**　{_num(tp1)}"}},
                ]},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"🤖 **AI 建议**　{advice_line}"}},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": f"{advice.get('reason') or '—'}"}},
            ],
        }
        # 交互卡片（2026-09-21）：自建应用发的卡片才收得到按钮回调；点击后
        # 后台平仓并在群内发结果卡片（feishu_bot._exec_close）。未配置则
        # webhook 纯展示、不带按钮
        app_ready = feishu_app_ready()
        if app_ready and settings.FEISHU_ACTION_TOKEN:
            card["elements"].append({"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 市价平仓"},
                "type": "danger",
                "value": {
                    "act": "close_market",
                    "token": settings.FEISHU_ACTION_TOKEN,
                    "trade_id": rec.id,
                },
            }]})
        ok = send_app_card(card) if app_ready else send_feishu_card(card)
        if not ok:
            # 降级纯文本（保底送达）
            pct = f"（风险额的 {loss_pct}）" if risk > 0 else ""
            text = (
                f"⚠️ AI 持仓建议：离场\n"
                f"{rec.symbol} {side}｜浮亏 {total:.2f} USDT（{r_mult}）{pct}\n"
                f"理由：{advice.get('reason') or '-'}"
            )
            send_feishu_text(text)
    except Exception as e:
        logger.warning("持仓建议飞书推送失败 %s: %s", rec.symbol, e)
