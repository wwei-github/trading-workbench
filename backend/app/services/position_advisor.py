"""在跑单 AI 持仓建议（docs/16）

两类自动触发（2026-09-24 起）：
- 浮亏触发：首次浮亏 / 3h 冷却到期 / 较上次诊断恶化 ≥0.5R
- 滞留触发：持仓 ≥24h 且从未触发 TP1（status 仍为 OPENED）——AI 评估继续
  持有的必要性（资金效率：占用一天未达第一目标），首诊后每 24h 复诊

持仓诊断：开单逻辑是否仍成立、走势是否走坏、建议动作
（hold 继续持有 / tighten 收紧止损 / reduce 减仓 / exit 离场）。
模式复用 ai_attribution（事实包 JSON + 单次 LLM + 结构化输出 + 词表校验），
结论落库 trade_records.ai_advice 并写 AI_ADVICE 事件；滞留诊断每次推送飞书
卡片（浮盈亏 + AI 结果，24h 一次不刷屏），浮亏诊断仅 action=exit 推送
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

ADVICE_COOLDOWN_HOURS = 3   # 浮亏诊断两次间隔
ADVICE_WORSEN_R = 0.5       # 冷却期内全程盈亏较上次诊断恶化 ≥0.5R → 提前刷新
STALE_HOLD_HOURS = 24       # 滞留触发：持仓 ≥24h 且从未触发 TP1
STALE_RECHECK_HOURS = 24    # 滞留复诊间隔（首诊后每 24h 再评估，直到 TP1/结算）

ACTIONS = ("hold", "tighten", "reduce", "exit")

ADVICE_SYSTEM = """你是期货交易系统的持仓风控顾问。一笔在跑交易需要持仓诊断，\
触发原因见事实包的 diagnosis_trigger 字段：
- floating_loss：当前处于浮亏，判断浮亏是否在计划波动范围内；
- stale_no_tp1：持仓已超过 24 小时仍未触发第一档止盈，需重点评估继续持有的必要性——\
资金已被占用一天而未兑现第一目标，结合走势是否停滞（MFE 距离 TP1 的差距、动能、结构）\
判断是"接近突破前的蓄势"还是"逻辑失效的空耗"，不要仅因浮盈为正就默认继续持有。

判断与输出要求：
1. thesis_valid：开仓逻辑是否仍成立（方向依据、关键位、形态是否被破坏）
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


def advice_due(rec, total: float, now: datetime) -> bool:
    """自动诊断触发判定：浮亏触发 + 滞留触发（docs/16 §3.1 + 2026-09-24 扩展）。

    rec 为 TradeRecord；total = 已实现 + 浮动（巡检刚写入的最新值）。
    浮亏：首次浮亏 / 3h 冷却到期 / 较上次诊断恶化 ≥0.5R。
    滞留：持仓 ≥24h 且从未触发 TP1（status 仍 OPENED），首诊后每 24h 复诊。
    """
    if not settings.AI_API_KEY:
        return False
    last = rec.ai_advice_at
    # ── 浮亏触发 ──
    if total < 0:
        if last is None:
            return True  # 首次浮亏
        if now - last >= _timedelta_hours(ADVICE_COOLDOWN_HOURS):
            return True  # 冷却到期
        base = float(rec.ai_advice_pnl or 0)
        risk = float(rec.risk_amount or 0)
        if risk > 0 and base - total >= risk * ADVICE_WORSEN_R:  # 恶化提前刷新
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
        # 触发原因（AI 按场景调整判断侧重，见 ADVICE_SYSTEM）
        "diagnosis_trigger": "stale_no_tp1" if is_stale_hold(rec) else "floating_loss",
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
    stale = is_stale_hold(rec)

    rec.ai_advice = advice
    rec.ai_advice_at = datetime.utcnow()
    rec.ai_advice_pnl = total  # 恶化判定基线
    db.add(TradeEvent(trade_record_id=rec.id, event_type="AI_ADVICE", detail={
        **advice, "total_pnl": total,
        "trigger": "stale_no_tp1" if stale else "floating_loss",
    }))
    db.commit()

    # 滞留诊断每次推送（24h 一次不刷屏，浮盈亏 + AI 结果）；
    # 浮亏诊断沿用 docs/16 规则：仅 exit 且同单不连续重复时推送
    if stale:
        _notify_stale(rec, advice, total)
    elif action == "exit" and prev_action != "exit":
        _notify_exit(rec, advice, total)
    return advice


ACTION_LABEL = {
    "hold": "继续持有",
    "tighten": "收紧止损",
    "reduce": "建议减仓",
    "exit": "建议离场",
}


def _notify_exit(rec, advice: dict, total: float) -> None:
    """建议离场 → 飞书交互卡片推送（失败仅记日志）。调用方已保证同单不连续重复推送。

    卡片布局：header 红 → 全宽浮亏高亮行（金额/持仓时长）→
    四列价格栅格（方向/开仓价/现价/止损）→ AI 建议（动作加粗 +
    thesis/trend 标注）→ 理由正文 → 市价平仓按钮（feishu_bot 回调执行）→
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
                "title": {"tag": "plain_text", "content": f"⚠️ AI 持仓建议 · 建议离场 · {rec.symbol}"},
                "template": "red",
            },
            "elements": [
                # 全宽浮亏高亮行：核心结论一眼可见
                {"tag": "div", "text": {"tag": "lark_md", "content":
                 f"{side}单{held_txt}\n"
                 f"🔻 **浮亏　<font color='red'>{total:+.2f} USDT</font>**"}},
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
        # 交互按钮：自建应用发的卡片才收得到回调；点击后后台平仓并发结果卡片
        # （feishu_bot._exec_close）。缺 ACTION_TOKEN 时不带按钮（纯展示）
        if settings.FEISHU_ACTION_TOKEN:
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
        send_app_card(card)
    except Exception as e:
        logger.warning("持仓建议飞书推送失败 %s: %s", rec.symbol, e)


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
                          "content": f"⏰ AI 持仓建议 · 持仓超24h未触发止盈 · {rec.symbol}"},
                "template": "red" if action_f == "exit"
                            else ("orange" if action_f in ("reduce", "tighten") else "blue"),
            },
            "elements": [
                # 全宽浮动盈亏高亮行（浮盈/浮亏都展示）
                {"tag": "div", "text": {"tag": "lark_md", "content":
                 f"{side}单 · 已持仓 {held_h}h 未触发 TP1\n"
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
        send_app_card(card)
    except Exception as e:
        logger.warning("滞留持仓建议飞书推送失败 %s: %s", rec.symbol, e)
