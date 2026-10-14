"""现货屯币任务（docs/18 P0：只算不发单）

spot-daily-check（每日 08:08，日K收盘 BJ 08:00 后）：
拉三币 日/周/月K → 落 spot_kline_cache → evaluate_day 评分（纯函数）→
飞书卡片推送评分与建议份数（纯展示，不下单——P1 接确认下单）。

零侵入约束（docs/18 §1）：独立任务命名 spot-*；不读写合约任何表；
复用仅限 task_log / notify / spot_client 基础设施。
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.celery_app import celery_app
from app.models.spot import SPOT_SYMBOLS, SpotKlineCache
from app.services.spot_engine import evaluate_day
from app.services.spot_settings import load_spot_settings

logger = logging.getLogger("app.spot")

_TZ_BJ = timezone(timedelta(hours=8))


def _bar_close_date(open_ms: int, interval: str) -> str:
    """K线收盘时刻的北京日期（as_of 标签）：日K=开盘+1天，周/月K≈开盘日+周期。"""
    span_ms = {"1d": 86400_000, "1w": 7 * 86400_000, "1M": 30 * 86400_000}[interval]
    close_bj = datetime.fromtimestamp((open_ms + span_ms) / 1000, _TZ_BJ)
    return close_bj.strftime("%Y-%m-%d")


def _upsert_cache(db, symbol: str, interval: str, raw: list) -> None:
    """K线缓存 upsert：只存精简列 [t,o,h,l,c,v]，与已有缓存**合并**（按 t 去重）。

    合并而非替换（2026-09-25）：全量回填的深历史不能被日检/周研判的
    最近 N 根覆盖——否则缓存永远只有 300 根，回测数据始终不足。
    raw 兼容两种形态：
    - 币安原始数组 [open_time, o, h, l, c, v, ...]（日检/周研判直传）
    - 缓存/合并后的 dict [{t, o, h, l, c, v}]（回测 _load 回补直传）
    """
    slim = []
    for k in raw:
        if isinstance(k, dict):
            slim.append({
                "t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
                "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"]),
            })
        else:
            slim.append({
                "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
            })
    row = (
        db.query(SpotKlineCache)
        .filter(SpotKlineCache.symbol == symbol, SpotKlineCache.interval == interval)
        .first()
    )
    if row is None:
        db.add(SpotKlineCache(symbol=symbol, interval=interval, klines=slim))
    else:
        row.klines = _merge_klines(row.klines, slim)
        row.updated_at = datetime.utcnow()


def _closes(raw: list) -> list:
    return [float(k[4]) for k in raw]


@celery_app.task(name="app.tasks.spot_tasks.run_spot_daily_task")
def run_spot_daily_task() -> dict:
    """现货日检：三币评分 + 飞书推送（P0 纯展示）。"""
    from app.database import SessionLocal
    from app.services.spot_client import SpotClient
    from app.services.task_log import close_task, open_task
    from app.services.notify import send_feishu_card

    task_id = open_task("spot", "现货日检评分")
    db = SessionLocal()
    client = SpotClient()
    try:
        settings_dict = load_spot_settings(db)
        decisions = []
        for symbol in SPOT_SYMBOLS:
            daily = client.klines(symbol, "1d", 300)
            weekly = client.klines(symbol, "1w", 200)
            monthly = client.klines(symbol, "1M", 150)
            if not daily:
                logger.warning("spot %s 日K为空，跳过", symbol)
                continue
            _upsert_cache(db, symbol, "1d", daily)
            _upsert_cache(db, symbol, "1w", weekly)
            _upsert_cache(db, symbol, "1M", monthly)

            as_of = _bar_close_date(int(daily[-1][0]), "1d")
            decisions.append(evaluate_day(
                symbol, as_of,
                _closes(daily), _closes(weekly), _closes(monthly),
            ))
        db.commit()

        if not decisions:
            close_task(task_id, "skipped", summary="无可用K线，未产生评分")
            return {"ok": False, "reason": "no klines"}

        push_ok = _push_card(decisions, settings_dict)
        close_task(
            task_id, "completed",
            summary="；".join(
                f"{d['symbol']} {d['score']['total']}分 {d['action']}{d['lots'] or ''}"
                for d in decisions
            ) + ("（飞书已推送）" if push_ok else "（飞书未配置/失败，仅落库）"),
            detail={
                "as_of": decisions[0]["as_of"],
                "env": {d["symbol"]: d["env"] for d in decisions},
                "actions": {d["symbol"]: {"action": d["action"], "lots": d["lots"],
                                          "score": d["score"]["total"],
                                          "plan": d["rules"]["plan_text"]} for d in decisions},
            },
        )
        return {"ok": True, "pushed": push_ok, "count": len(decisions)}
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=str(e))
        logger.exception("现货日检失败")
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass
        db.close()


def _cache_klines(db, symbol: str, interval: str) -> list:
    """读缓存K线（t 排序旧→新）。"""
    row = (
        db.query(SpotKlineCache)
        .filter(SpotKlineCache.symbol == symbol, SpotKlineCache.interval == interval)
        .first()
    )
    if row is None:
        return []
    return sorted(row.klines, key=lambda k: k["t"])


def _merge_klines(local: list, remote: list) -> list:
    """合并本地与远端K线（按 t 去重，远端覆盖同 t，旧→新）。"""
    by_t = {k["t"] if isinstance(k, dict) else int(k[0]): k for k in local}
    for k in remote:
        t = k["t"] if isinstance(k, dict) else int(k[0])
        by_t[t] = (
            k if isinstance(k, dict)
            else {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                  "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        )
    return [by_t[t] for t in sorted(by_t)]


# ---------------- K线全量回填（免费公开行情，2026-09-25） ----------------

# 周期跨度（ms）与全量目标根数：1d 拉满上市至今（BTC ~3300 根 ≈ 4 页），
# 1w/1M 一页即可覆盖全历史
_KLINE_SPAN_MS = {"1d": 86_400_000, "1w": 7 * 86_400_000, "1M": 30 * 86_400_000}
_KLINE_MAX_BARS = {"1d": 4000, "1w": 600, "1M": 200}


def _backfill_symbol(db, client, symbol: str, interval: str) -> int:
    """单币单周期全量回填（正式网公开端点，endTime 向前翻页），返回缓存总根数。

    增量口径：缓存已足量且新鲜（末根距今 ≤3 个周期）则跳过；
    否则先拉最新 1000 根保新鲜，再从最旧一根向前翻页补深度，直至拉到上市。
    """
    existing = _cache_klines(db, symbol, interval)
    now_ms = int(time.time() * 1000)
    span = _KLINE_SPAN_MS[interval]
    if (
        len(existing) >= _KLINE_MAX_BARS[interval]
        and now_ms - int(existing[-1]["t"]) <= 3 * span
    ):
        return len(existing)

    merged = list(existing)
    end_ms: Optional[int] = None
    for _ in range(50):  # 防御上限（1d 全历史 ~4 页，足够）
        if len(merged) >= _KLINE_MAX_BARS[interval]:
            break
        batch = client.klines(symbol, interval, 1000, end_ms=end_ms)
        if not batch:
            break  # 已翻到上市首根
        slim = [
            {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in batch
        ]
        merged = _merge_klines(merged, slim)
        oldest = slim[0]["t"]
        if merged and int(merged[0]["t"]) < oldest - 1:
            break  # 缓存里已有更早历史，无需继续向前翻页
        end_ms = oldest - 1
    _upsert_cache(db, symbol, interval, merged)
    return len(merged)


@celery_app.task(name="app.tasks.spot_tasks.run_spot_backfill_task")
def run_spot_backfill_task() -> dict:
    """K线全量回填：三币 × 1d/1w/1M，正式网公开端点分页拉全历史 → spot_kline_cache。

    免费（公开数据无需 key，与交易环境无关）；增量幂等：足量且新鲜即跳过。
    回测/日检/周研判共用同一缓存。"""
    from app.database import SessionLocal
    from app.services.spot_client import SpotClient
    from app.services.task_log import close_task, open_task

    task_id = open_task("spot", "K线全量回填")
    db = SessionLocal()
    client = SpotClient(public=True)
    try:
        counts = {}
        for symbol in SPOT_SYMBOLS:
            for interval in ("1d", "1w", "1M"):
                before = len(_cache_klines(db, symbol, interval))
                after = _backfill_symbol(db, client, symbol, interval)
                counts[f"{symbol}:{interval}"] = after
                logger.info(
                    "K线回填 %s %s: %d → %d 根%s",
                    symbol, interval, before, after,
                    "（已足量新鲜，跳过）" if before == after else "",
                )
        db.commit()
        close_task(
            task_id, "completed",
            summary="；".join(f"{k}={v}根" for k, v in counts.items()),
            detail={"counts": counts},
        )
        return {"ok": True, "counts": counts}
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=str(e))
        logger.exception("K线全量回填失败")
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass
        db.close()


_ACTION_TEXT = {
    "BUY": ("低买", "green"),
    "SELL": ("高卖", "red"),
    "HOLD": ("观望", "blue"),
}


# ---------------- 周度定投 + AI 周度研判（docs/18 §6，P1） ----------------


def _week_label() -> str:
    """ISO 周标识（如 2026-W39），北京时区。"""
    iso = datetime.now(_TZ_BJ).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@celery_app.task(name="app.tasks.spot_tasks.run_spot_dca_task")
def run_spot_dca_task() -> dict:
    """周度定投（周一 08:15）：dca_enabled 才执行，每币买入 dca_amount。

    走 spot_exec.exec_spot_order（与飞书确认 / 手动 API 同一风控与锁），
    trigger_type='dca' 不受信号单每日一次冷却限制。
    定投账号口径：现货启用清单第一个账号（resolve_spot_account，P1 不做扇出）。"""
    from app.database import SessionLocal
    from app.services.spot_exec import exec_spot_order
    from app.services.task_log import close_task, open_task

    task_id = open_task("spot", "现货周度定投")
    db = SessionLocal()
    try:
        s = load_spot_settings(db)
        if not s["dca_enabled"] or s["dca_amount"] <= 0:
            close_task(task_id, "skipped", summary="定投未开启或金额为 0，跳过")
            return {"ok": False, "reason": "dca disabled"}

        week = _week_label()
        results = {}
        for symbol in SPOT_SYMBOLS:
            results[symbol] = exec_spot_order(
                db, symbol, "BUY", quote_amount=s["dca_amount"],
                trigger_type="dca",
                trigger_detail={"week": week, "dca_amount": s["dca_amount"]},
            )
        ok_count = sum(1 for v in results.values() if v["ok"])

        def _cell(sym: str) -> str:
            v = results[sym]
            if v["ok"]:
                return f"{sym.replace('USDT', '')} ✅ 买入 {v.get('quote_spent', 0):.2f} U → {v.get('executed_qty', 0):.8f}"
            return f"{sym.replace('USDT', '')} ❌ {v.get('reason') or '失败'}"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🪙 现货周度定投 · {week}"},
                "template": "green" if ok_count == len(SPOT_SYMBOLS) else ("orange" if ok_count else "red"),
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(
                    _cell(sym) for sym in SPOT_SYMBOLS
                )}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                    f"定投兜底累积（每周一 08:15）· 单次 {s['dca_amount']:,.0f} U/币 · 详情见现货订单记录"
                )}]},
            ],
        }
        try:
            from app.services.notify import send_feishu_card

            push_ok = send_feishu_card(card)
        except Exception as e:
            logger.warning("定投卡片推送失败: %s", e)
            push_ok = False

        close_task(
            task_id, "completed" if ok_count else "failed",
            summary="；".join(
                f"{sym} {'成交' if results[sym]['ok'] else '失败'}" for sym in SPOT_SYMBOLS
            ) + ("" if push_ok else "（飞书推送失败）"),
            detail={"week": week, "results": {
                sym: {"ok": v["ok"], "reason": v.get("reason"),
                      "quote_spent": v.get("quote_spent"),
                      "executed_qty": v.get("executed_qty")} for sym, v in results.items()
            }},
        )
        return {"ok": ok_count == len(SPOT_SYMBOLS), "results": results}
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("现货周度定投失败")
        raise
    finally:
        db.close()


_WEEKLY_REGIMES = ("积累期", "上升期", "分配期", "下降期")

_WEEKLY_SYSTEM_PROMPT = """你是加密货币现货屯币策略的周度宏观研判助手。基于给定的三币三周期（月/周/日）量化快照，输出本周研判。
严格输出 JSON（不要 markdown 包裹、不要思考过程），字段：
- regime: 市场阶段，四选一："积累期"（月线熊市/深度回撤，屯币黄金区）/ "上升期"（月线转多头、周线牛熊线上方初期）/ "分配期"（月线多头高位、周线过热，波段卖出窗口）/ "下降期"（月线跌破 MA12月，转入熊市）
- outlook: 对象，每个币种 "偏多"|"中性"|"偏空" 三选一，键为 BTC/ETH/BNB
- risks: 数组，本周风险提示，最多 3 条，每条一句话
- commentary: ≤400 字 Markdown 正文。要求：结合月K环境（牛熊/距ATH回撤）、周K牛熊线、日K超卖/过热信号，说明当前阶段判断依据、屯币动作倾向（多买/定投/只卖波段等）、以及本周需要关注的变化。禁止给出具体价格目标，禁止任何合约/杠杆建议，禁止臆测快照之外的宏观事件。"""


@celery_app.task(name="app.tasks.spot_tasks.run_spot_weekly_review_task")
def run_spot_weekly_review_task() -> dict:
    """AI 周度研判（周一 08:30，纯展示、无执行权限，docs/18 §4.3）：
    三币 evaluate_day 快照 → LLM → spot_weekly_reviews（week 唯一，重复跳过）→ 飞书卡片。"""
    from app.database import SessionLocal
    from app.models.spot import SPOT_SYMBOLS, SpotWeeklyReview
    from app.models.system_config import SystemConfig
    from app.services.llm_client import configure_from
    from app.services.spot_client import SpotClient
    from app.services.task_log import close_task, open_task

    task_id = open_task("spot", "AI 周度研判")
    db = SessionLocal()
    configure_from(db.get(SystemConfig, 1))  # 生效 AI 运行参数每轮重读 DB
    client = SpotClient()
    try:
        week = _week_label()
        if db.query(SpotWeeklyReview).filter(SpotWeeklyReview.week == week).first():
            close_task(task_id, "skipped", summary=f"{week} 研判已存在，跳过")
            return {"ok": False, "reason": "already exists"}

        decisions = []
        snapshot = {}
        for symbol in SPOT_SYMBOLS:
            daily = client.klines(symbol, "1d", 300)
            weekly = client.klines(symbol, "1w", 200)
            monthly = client.klines(symbol, "1M", 150)
            if not daily:
                continue
            _upsert_cache(db, symbol, "1d", daily)
            _upsert_cache(db, symbol, "1w", weekly)
            _upsert_cache(db, symbol, "1M", monthly)
            d = evaluate_day(
                symbol, _bar_close_date(int(daily[-1][0]), "1d"),
                _closes(daily), _closes(weekly), _closes(monthly),
            )
            decisions.append(d)
            snapshot[symbol] = {
                "price": d["price"], "score": d["score"]["total"], "action": d["action"],
                "env": d["env"],
            }
        db.commit()
        if len(decisions) < len(SPOT_SYMBOLS):
            raise RuntimeError(f"K线数据不全（{len(decisions)}/{len(SPOT_SYMBOLS)}），本周研判跳过")

        # LLM 调用（10~30s，任务内异步执行，不进任何 Redis 锁）
        parsed = _call_weekly_llm(snapshot)

        row = SpotWeeklyReview(
            week=week, regime=parsed["regime"], outlook=parsed["outlook"],
            risks=parsed["risks"], commentary=parsed["commentary"],
            indicator_snapshot={"decisions": snapshot},
        )
        db.add(row)
        db.commit()

        push_ok = _push_review_card(week, parsed, decisions)
        close_task(
            task_id, "completed",
            summary=f"{week} {parsed['regime']}（飞书{'已推送' if push_ok else '未推送'}）",
            detail={"week": week, "regime": parsed["regime"]},
        )
        return {"ok": True, "week": week, "regime": parsed["regime"]}
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=str(e))
        logger.exception("AI 周度研判失败")
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass
        db.close()


def _call_weekly_llm(snapshot: dict) -> dict:
    """快照 → LLM JSON（容错解析 + 字段收紧）。"""
    from app.services.llm_client import current_model, get_client, parse_llm_json

    lines = []
    for sym, d in snapshot.items():
        env = d["env"]
        lines.append(
            f"{sym}: 现价 {d['price']:.2f} · 三周期评分 {d['score']} · 建议动作 {d['action']} | "
            f"月K: {'熊市' if env['monthly_bear'] else '非熊市'}、距ATH回撤 {env['ath_drawdown']}、"
            f"深度价值区 {env['deep_value']} | 周K: 牛熊线上{'方' if env['weekly_above'] else '下方'}"
            f"（vs MA30周 {env['vs_ma30w']}）、周RSI {env['weekly_rsi']}、"
            f"周MACD{'金叉后' if env['weekly_macd_golden'] else '未金叉'} | 日K: vs MA200 {env['vs_ma200']}"
        )
    user_prompt = "三币三周期量化快照（评分越高越接近超卖低吸区，动作来自程序规则仅供参考）：\n" + "\n".join(lines)

    client = get_client()
    resp = client.chat.completions.create(
        model=current_model(),
        messages=[
            {"role": "system", "content": _WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=4000,
    )
    content = (resp.choices[0].message.content or "").strip()
    # 统一健壮解析（与 ai_analyzer 同款）：标准解析失败降级 json_repair，
    # 兼容 JSON 前导文本 / markdown 围栏 / 尾逗号。原围栏剥离逻辑在模型
    # 输出 JSON 前带一句话说明时直接炸 json.loads（2026-10-05 周度研判失败）
    raw = parse_llm_json(content)

    regime = raw.get("regime")
    if regime not in _WEEKLY_REGIMES:
        raise ValueError(f"LLM regime 非法: {regime}")
    outlook_raw = raw.get("outlook") or {}
    outlook = {
        sym.replace("USDT", ""): (outlook_raw.get(sym.replace("USDT", "")) or "中性")
        for sym in SPOT_SYMBOLS
    }
    for k, v in outlook.items():
        if v not in ("偏多", "中性", "偏空"):
            outlook[k] = "中性"
    risks = [str(x)[:100] for x in (raw.get("risks") or [])][:3]
    commentary = str(raw.get("commentary") or "").strip()[:2000]
    return {"regime": regime, "outlook": outlook, "risks": risks, "commentary": commentary}


def _push_review_card(week: str, parsed: dict, decisions: list) -> bool:
    """周度研判卡片（纯展示）。"""
    outlook_cells = []
    score_map = {d["symbol"]: d["score"]["total"] for d in decisions}
    for sym in ("BTC", "ETH", "BNB"):
        v = parsed["outlook"].get(sym, "中性")
        color = {"偏多": "green", "偏空": "red"}.get(v, "blue")
        outlook_cells.append({
            "tag": "column", "width": "weighted", "weight": 1,
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": (
                f"**{sym}**\n<font color='{color}'>{v}</font>（评分 {score_map.get(sym + 'USDT', '—')}）"
            )}}],
        })
    elements: list = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"**市场阶段：{parsed['regime']}**"}},
        {"tag": "column_set", "flex_mode": "stretch", "columns": outlook_cells},
        {"tag": "hr"},
    ]
    if parsed["risks"]:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": (
            "**风险提示**\n" + "\n".join(f"- {r}" for r in parsed["risks"])
        )}})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": parsed["commentary"]}})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": (
        "AI 周度研判 · 纯展示无执行权限（docs/18 §4.3）· 留档见现货历史记录"
    )}]})
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔭 现货周度研判 · {week}"},
            "template": "purple",
        },
        "elements": elements,
    }
    try:
        from app.services.notify import send_feishu_card

        return send_feishu_card(card)
    except Exception as e:
        logger.warning("周度研判卡片推送失败: %s", e)
        return False


def _push_card(decisions: list, settings_dict: dict) -> bool:
    """飞书评分卡片（P1 半自动：BUY/SELL 决策币种带一键确认按钮）。

    按钮仅在自建应用三件套 + FEISHU_ACTION_TOKEN 配置齐全时展示
    （webhook 卡片收不到回调，同合约「市价平仓」按钮的降级规则）。
    value 全用字符串（UUID/数字序列化事故教训）。"""
    from app.config import settings as app_settings
    from app.services import feishu_bot

    actionable = feishu_bot.configured() and bool(app_settings.FEISHU_ACTION_TOKEN)
    date_label = decisions[0]["as_of"]
    cols = []
    for d in decisions:
        act_text, _color = _ACTION_TEXT[d["action"]]
        if d["action"] == "BUY":
            act = f"**买入 {d['lots']} 份**（{d['quote_amount']:,.0f} U）"
        elif d["action"] == "SELL":
            act = f"**卖出 {d['lots']} 份**（波段仓）"
        else:
            act = "观望"
        cols.append({
            "tag": "column", "width": "weighted", "weight": 1,
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join([
                f"**{d['symbol'].replace('USDT', '')}**",
                f"现价 {d['price']:,.2f}",
                f"评分 **{d['score']['total']}**",
                act,
                d["rules"]["plan_text"],
            ])}}],
        })

    elements: list = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**三币屯币评分**"}},
        {"tag": "column_set", "flex_mode": "stretch", "columns": cols},
    ]

    if actionable:
        buttons = []
        for d in decisions:
            sym = d["symbol"]
            if d["action"] == "BUY":
                buttons.append({
                    "tag": "button", "type": "primary",
                    "text": {"tag": "plain_text",
                             "content": f"确认买入 {sym.replace('USDT', '')} {d['lots']}份"},
                    "value": {
                        "act": "spot_buy", "token": app_settings.FEISHU_ACTION_TOKEN,
                        "symbol": sym, "lots": str(d["lots"] or ""),
                        "quote": str(d["quote_amount"] or ""),
                    },
                })
            elif d["action"] == "SELL":
                buttons.append({
                    "tag": "button", "type": "danger",
                    "text": {"tag": "plain_text",
                             "content": f"确认卖出 {sym.replace('USDT', '')} {d['lots']}份"},
                    "value": {
                        "act": "spot_sell", "token": app_settings.FEISHU_ACTION_TOKEN,
                        "symbol": sym, "lots": str(d["lots"] or ""),
                    },
                })
        if buttons:
            elements.append({"tag": "hr"})
            elements.append({"tag": "action", "actions": buttons})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": (
        "半自动模式：点击按钮确认后市价执行（统一风控：投入上限/保留垫/波段仓 30%/每日一次冷却）。"
        f"单份 {settings_dict['per_order_usdt']:,.0f} U · 单币上限 {settings_dict['max_total_usdt']:,.0f} U"
        if actionable else
        "评分仅供参考，不自动下单（docs/18）。配置自建应用后可一键确认下单。"
        f"单份 {settings_dict['per_order_usdt']:,.0f} U · 单币上限 {settings_dict['max_total_usdt']:,.0f} U"
    )}]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🪙 现货屯币日报 · {date_label}"},
            "template": "blue",
        },
        "elements": elements,
    }
    try:
        from app.services.notify import send_feishu_card

        return send_feishu_card(card)
    except Exception as e:
        logger.warning("现货评分卡片推送失败: %s", e)
        return False
