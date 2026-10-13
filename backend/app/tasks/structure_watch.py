"""每小时技术结构检测（structure-watch，2026-10-13）

对大盘 BTCUSDT + ETHUSDT 双标的，每小时整点做技术结构巡检（只读不决策）：
- 1h：K线结构形态识别（摆动点结构 up/down/range）+ 信号有效性验证（结构破位判定）
- 4h：结构完整性检测（已收盘根数/缺口/陈旧 + analyze_htf_background 状态机背景态）
- 输出：结构类型、关键点位（最近摆动高/低点及距现价%）、信号有效性 → 任务记录 detail
- 误差自监控：触发延迟 >30s（task:lag）、上轮完成距今 >70min（task:missed）
- 四类异常自动标记：结构破位 / 周期冲突 / 数据异常 / 执行异常
- 飞书推送「仅异常·去重」（2026-10-13 拍板）：异常出现 / 异常集合变化 /
  异常消除（恢复）才推；持续同一异常不刷屏；正常时段只写任务记录

结果经 /api/market/overview.structure 透出到 Header 大盘条（MarketOverviewBar）。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Optional

import numpy as np
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.ops import TaskRecord
from app.models.system_config import SystemConfig
from app.services.exchange_pool import AllExchangesFailed, ExchangePool
from app.services.notify import send_feishu_card, send_feishu_text
from app.services.scan_filters import resolve_scan_filters
from app.services.strategy import recent_swings
from app.services.strategy.swing import classify_structure, find_swing_points, merge_swings
from app.services.strategy.technical import analyze_htf_background

logger = logging.getLogger(__name__)

_SYMBOLS = ("BTCUSDT", "ETHUSDT")
_NAME = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
_TASK_NAME = "技术结构检测"

_KLINE_1H, _KLINE_4H = 500, 300  # 取数根数（对齐 ai_tasks 惯例）
_MIN_1H_CLOSED = 200             # 1h 数据门（对齐 ema_touch_min_bars）
_MIN_4H_CLOSED = 155             # analyze_htf_background 硬要求（EMA144 种子 + 收敛）
_LAG_LIMIT_SEC = 30              # 整点触发误差上限（需求 4：>30s 标记执行异常）
_MISS_LIMIT_MIN = 70             # 上轮 completed 距今超此值 = 漏检
_H1_MS = 3600_000
_H4_MS = 4 * 3600_000
_TZ_BJ = timezone(timedelta(hours=8))

_TREND_LABEL = {"up": "上涨结构", "down": "下降结构", "range": "区间震荡", "unknown": "结构未定"}


def _now_bj() -> datetime:
    """当前北京时间（模块级函数：测试 monkeypatch 触发延迟）"""
    return datetime.now(_TZ_BJ)


@celery_app.task(name="app.tasks.structure_watch.run_structure_watch_task")
def run_structure_watch_task() -> dict:
    """每小时整点技术结构检测（beat 触发）。"""
    from app.services.task_log import close_task, open_task

    task_id = open_task("review", _TASK_NAME, trigger="scheduled")
    try:
        return _structure_watch_body(task_id)
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("技术结构检测任务失败")
        raise


def _structure_watch_body(task_id: str) -> dict:
    """检测主体（任务记录由外层管理）：分析 → 异常汇总 → 去重推送 → 落记录。"""
    from app.services.task_log import close_task

    now = _now_bj()
    # 需求 4：整点触发误差自监控（beat 抖动 / worker 排队落 detail，超限告警）
    lag = (now - now.replace(minute=0, second=0, microsecond=0)).total_seconds()

    order = _resolve_order()

    # 上一轮 completed 记录：一次查询同时供漏检判定 / 去重 / 恢复判定
    last = _last_completed()
    last_sig, gap_min = "", None
    if last is not None:
        last_sig = str((last.detail or {}).get("anomaly_signature") or "")
        if last.started_at:
            gap_min = (datetime.utcnow() - last.started_at).total_seconds() / 60.0

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(partial(_analyze_symbol, order=order, now_ts=now.timestamp()), _SYMBOLS))
    per = dict(zip(_SYMBOLS, results))

    # 异常汇总：标的级 code 前缀 symbol，任务级前缀 task
    codes: list[str] = []
    messages: list[str] = []
    for sym in _SYMBOLS:
        codes.extend(f"{sym}:{c}" for c in per[sym]["codes"])
        messages.extend(per[sym]["messages"])
    if lag > _LAG_LIMIT_SEC:
        codes.append("task:lag")
        messages.append(f"执行异常：触发延迟 {lag:.0f}s（>{_LAG_LIMIT_SEC}s）")
    if gap_min is not None and gap_min > _MISS_LIMIT_MIN:
        codes.append("task:missed")
        messages.append(f"执行异常：上轮检测缺失（间隔 {gap_min:.0f} 分钟）")

    signature = "|".join(sorted(codes))

    # 推送策略「仅异常·去重」：持续同一异常不重复推；正常→异常推告警；异常→正常推恢复
    if signature and signature == last_sig:
        notify_mode, ok = "dedup", False
    elif signature:
        ok = _push_alert(now, messages, per, order, lag)
        notify_mode = "alert"
    elif last_sig:
        ok = _push_recover(now, per, order, lag)
        notify_mode = "recover"
    else:
        notify_mode, ok = "silent", False

    detail = {
        "lag_seconds": round(lag, 1),
        "swing_order": order,
        "gap_minutes": round(gap_min, 1) if gap_min is not None else None,
        "anomaly_signature": signature,
        "anomalies": messages,
        "notified": ok,
        "notify_mode": notify_mode,
        "symbols": {sym: per[sym]["detail"] for sym in _SYMBOLS},
    }
    if messages:
        status = " · ".join(_sym_status(_NAME[sym], per[sym]) for sym in _SYMBOLS)
        summary = f"{status} · {len(messages)} 项异常（飞书{'已推送' if ok else '未推送'}）"
    else:
        trends = " / ".join(
            f"{_NAME[sym]} {per[sym]['detail']['structure']['trend']}" for sym in _SYMBOLS
        )
        tail = "飞书已推送恢复" if ok else "飞书未推送"
        summary = f"{trends} · 无异常（{tail}）"

    close_task(task_id, "completed", summary=summary, detail=detail)
    logger.info("技术结构检测完成：%s", summary)
    return {"signature": signature, "anomaly_count": len(messages),
            "notified": ok, "notify_mode": notify_mode}


def _resolve_order() -> int:
    """摆动阶数跟随 SystemConfig.swing_order（单一来源，与扫描/标注口径一致）；失败回落 env。"""
    try:
        db = SessionLocal()
        try:
            cfg = db.get(SystemConfig, 1)
        finally:
            db.close()
        return int(resolve_scan_filters(cfg).swing_order)
    except Exception as e:
        logger.debug("读取摆动阶数失败（回落 env）: %s", e)
        return int(settings.SWING_ORDER)


def _last_completed() -> Optional[TaskRecord]:
    """最新一条 completed「技术结构检测」记录（去重/恢复/漏检判定的单一来源）。

    本轮记录尚为 running，天然不会被 status 过滤命中。
    """
    try:
        db = SessionLocal()
        try:
            return db.execute(
                select(TaskRecord)
                .where(TaskRecord.task_name == _TASK_NAME, TaskRecord.status == "completed")
                .order_by(TaskRecord.started_at.desc())
                .limit(1)
            ).scalars().first()
        finally:
            db.close()
    except Exception as e:
        logger.warning("查询上轮结构检测记录失败（忽略）: %s", e)
        return None


# ── 单标的检测 ──


def _analyze_symbol(symbol: str, order: int, now_ts: float) -> dict:
    """单标的结构检测，返回 {"codes", "messages", "detail"}；任何内部异常都
    收敛为数据异常（不中断另一标的、不拖垮任务整体）。"""
    name = _NAME.get(symbol, symbol)
    codes: list[str] = []
    messages: list[str] = []

    def add(code: str, msg: str) -> None:
        if code not in codes:  # 同类异常只记一次
            codes.append(code)
            messages.append(msg)

    structure: dict = {
        "trend": "unknown", "label_high": None, "label_low": None,
        "last_h": None, "last_l": None, "close": None,
        "valid": None, "anchor": None, "note": "",
    }
    key_levels: dict = {
        "resistance": None, "resistance_dist_pct": None,
        "support": None, "support_dist_pct": None,
    }
    htf: Optional[dict] = None
    integrity = {"k1h_gaps": 0, "k4h_gaps": 0, "k1h_closed": 0, "k4h_closed": 0}
    error: Optional[str] = None

    try:
        k1, k4 = _fetch(pool=ExchangePool(), symbol=symbol)

        # ── 1h 结构（全部基于已收盘 K 线）──
        closed1 = k1[:-1]
        integrity["k1h_closed"] = len(closed1)
        if len(closed1) < _MIN_1H_CLOSED:
            add("data_1h_short",
                f"{name} 数据异常：1h 已收盘 {len(closed1)} 根 <{_MIN_1H_CLOSED}")
        else:
            closes = np.array([float(k[4]) for k in closed1], dtype=float)
            hi, lo = find_swing_points(closes, closes, order)
            swings = merge_swings(hi, lo, closes, closes)
            cls = classify_structure(swings)
            sw = recent_swings(k1, order=order, n=3)
            close_last = float(closed1[-1][4])
            structure = {
                "trend": cls["trend"],
                "label_high": sw["highs"][0]["label"] if sw["highs"] else None,
                "label_low": sw["lows"][0]["label"] if sw["lows"] else None,
                "last_h": float(cls["last_h"]) if cls["last_h"] is not None else None,
                "last_l": float(cls["last_l"]) if cls["last_l"] is not None else None,
                "close": close_last,
                "valid": None, "anchor": None, "note": "",
            }
            if cls["last_h"] is not None:
                key_levels["resistance"] = float(cls["last_h"])
                key_levels["resistance_dist_pct"] = round(
                    (float(cls["last_h"]) / close_last - 1) * 100, 2)
            if cls["last_l"] is not None:
                key_levels["support"] = float(cls["last_l"])
                key_levels["support_dist_pct"] = round(
                    (float(cls["last_l"]) / close_last - 1) * 100, 2)

            # 信号有效性 / 破位（range/unknown 不判，不告警）
            valid, anchor, broken_level, note = _structure_validity(swings, closes, cls["trend"])
            structure["valid"] = valid
            structure["anchor"] = anchor
            structure["note"] = note
            if valid is False and broken_level is not None:
                if cls["trend"] == "up":
                    add("break_down", f"{name} 结构破位：上升结构收盘跌破摆动低点 "
                        f"{broken_level:,.0f}（最新 {close_last:,.0f}）")
                else:
                    add("break_up", f"{name} 结构破位：下降结构收盘升破摆动高点 "
                        f"{broken_level:,.0f}（最新 {close_last:,.0f}）")

        behind = _stale(k1, now_ts, _H1_MS)
        if behind:
            add("stale_1h", f"{name} 数据异常：1h 数据陈旧（落后 {behind} 根）")
        integrity["k1h_gaps"] = _gaps(k1, _H1_MS)
        if integrity["k1h_gaps"]:
            add("gap_1h", f"{name} 数据异常：1h 缺口 {integrity['k1h_gaps']} 处")

        # ── 4h 完整性（根数/缺口/陈旧 + 背景状态机）──
        closed4 = k4[:-1]
        integrity["k4h_closed"] = len(closed4)
        if len(closed4) < _MIN_4H_CLOSED:
            add("data_4h_short",
                f"{name} 数据异常：4h 已收盘 {len(closed4)} 根 <{_MIN_4H_CLOSED}")
        else:
            bg = analyze_htf_background(k4)
            if bg is None:
                add("data_4h_short", f"{name} 数据异常：4h 背景状态机不可用")
            else:
                htf = {
                    "state": bg["state"], "state_label": bg["state_label"],
                    "dir": int(bg["dir"]), "age_bars": bg["age_bars"],
                    "closed_bars": len(closed4),
                }
        behind = _stale(k4, now_ts, _H4_MS)
        if behind:
            add("stale_4h", f"{name} 数据异常：4h 数据陈旧（落后 {behind} 根）")
        integrity["k4h_gaps"] = _gaps(k4, _H4_MS)
        if integrity["k4h_gaps"]:
            add("gap_4h", f"{name} 数据异常：4h 缺口 {integrity['k4h_gaps']} 处")

        # ── 周期冲突：1h 趋势与 4h 背景方向相反 ──
        if htf and structure["trend"] in ("up", "down") and htf["dir"] != 0:
            trend_dir = 1 if structure["trend"] == "up" else -1
            if trend_dir != htf["dir"]:
                add("conflict", f"{name} 周期冲突：1h "
                    f"{_TREND_LABEL[structure['trend']]} vs 4h {htf['state_label']}")
    except Exception as e:
        logger.warning("结构检测失败 %s: %s", symbol, e)
        add("fetch", f"{name} 数据异常：{e}")
        error = str(e)[:300]

    return {
        "codes": codes,
        "messages": messages,
        "detail": {
            "structure": structure, "key_levels": key_levels, "htf": htf,
            "integrity": integrity, "anomalies": codes, "error": error,
        },
    }


def _fetch(pool: ExchangePool, symbol: str) -> tuple[list, list]:
    """取 1h/4h K 线（不 force_refresh：小时桶缓存与扫描/图表共享）。"""
    try:
        return (pool.get_klines(symbol, "1h", _KLINE_1H),
                pool.get_klines(symbol, "4h", _KLINE_4H))
    except AllExchangesFailed as e:
        raise RuntimeError(f"取数失败（{e}）") from e


def _structure_validity(
    swings: list[tuple[int, str, float]], closes: np.ndarray, trend: str,
) -> tuple[Optional[bool], Optional[float], Optional[float], str]:
    """1h 信号有效性：返回 (valid, anchor, broken_level, note)。

    up 以摆动低点为防守位（收盘跌破=破位），down 以摆动高点为防守位（收盘升破=破位）；
    range/unknown 不判（valid=None，不告警）。
    - valid=False：最近防守位已破且最新收盘仍在破位侧（结构破位，broken_level=该位）
    - valid=True + note：曾破但最新收盘已收回（不告警，仅注记）
    - valid=True：自形成以来从未被收盘击穿（anchor=结构防守位）
    """
    if trend == "up":
        seq, below = [s for s in swings if s[1] == "L"], True
    elif trend == "down":
        seq, below = [s for s in swings if s[1] == "H"], False
    else:
        return None, None, None, ""
    if not seq:
        return None, None, None, ""

    idx_last, _kind, level_last = seq[-1]
    level_last = float(level_last)
    if not np.isfinite(level_last) or level_last <= 0:
        return None, None, None, ""

    seg = closes[idx_last + 1:]
    broke = bool(((seg < level_last) if below else (seg > level_last)).any())
    last = float(closes[-1])
    still = (last < level_last) if below else (last > level_last)

    if broke and still:
        # 破位未收回。锚点回退到更旧的未破位（若有）——供前端展示次级防守位
        return False, _latest_unbroken(seq[:-1], closes, below), level_last, ""
    if broke:
        return True, level_last, None, "曾跌破后收回" if below else "曾升破后收回"
    return True, level_last, None, ""


def _latest_unbroken(
    seq: list[tuple[int, str, float]], closes: np.ndarray, below: bool,
) -> Optional[float]:
    """最近一个自形成以来未被收盘价击穿的摆动极值位（新→旧扫描，全部已破返回
    None；语义对齐 trigger._unbroken_pivot_level，本模块独立实现不跨模块引用私有函数）。"""
    for idx, _kind, price in reversed(seq):
        price = float(price)
        if idx < 0 or idx >= len(closes):
            continue
        seg = closes[idx + 1:]
        broken = bool(((seg < price) if below else (seg > price)).any())
        if not broken:
            return price
    return None


def _gaps(klines: list[list], step_ms: int) -> int:
    """相邻 K 线 open_time 差 ≠ 桶宽的缺口处数（重复/乱序也计为缺口）。"""
    ts = [int(k[0]) for k in klines]
    return sum(1 for a, b in zip(ts, ts[1:]) if b - a != step_ms)


def _stale(klines: list[list], now_ts: float, step_ms: int) -> int:
    """末根 K 线落后当前整桶的根数（0=新鲜）。

    滞后 1 根内视为新鲜：整点刚过 / 服务器时钟略超前时，交易所可能尚未开出
    新桶（末根仍是上一桶），滞后超过一整根才判数据陈旧（避免边界误报）。
    """
    bucket = int(now_ts * 1000) // step_ms * step_ms
    bars = max(0, (bucket - int(klines[-1][0])) // step_ms)
    return int(bars) if bars >= 2 else 0


# ── 推送 ──


def _sym_status(name: str, res: dict) -> str:
    """标的一句话状态（summary 用）。"""
    codes = res["codes"]
    if "break_down" in codes or "break_up" in codes:
        return f"{name} 结构破位"
    if "conflict" in codes:
        return f"{name} 周期冲突"
    if codes:
        return f"{name} 数据异常"
    return f"{name} 正常"


def _symbol_line(name: str, res: dict) -> str:
    """标的结构摘要行（卡片用：结构类型 · 关键点位 · 有效性 · 4h 背景）。"""
    d = res["detail"]
    st, kl, htf = d["structure"], d["key_levels"], d["htf"]
    parts = [f"**{name}**　{_TREND_LABEL.get(st['trend'], '结构未定')}"]
    labels = " / ".join(x for x in (st.get("label_high"), st.get("label_low")) if x)
    if labels:
        parts.append(labels)
    if kl.get("support") is not None:
        parts.append(f"支撑 {kl['support']:,.0f}（{kl['support_dist_pct']:+.2f}%）")
    if kl.get("resistance") is not None:
        parts.append(f"阻力 {kl['resistance']:,.0f}（{kl['resistance_dist_pct']:+.2f}%）")
    if st.get("valid") is False:
        parts.append("<font color='red'>结构破位</font>")
    elif st.get("valid") is True:
        note = f"（{st['note']}）" if st.get("note") else ""
        parts.append(f"信号有效{note}")
    if htf:
        parts.append(f"4h {htf['state_label']}")
    return " · ".join(parts)


def _push_alert(now: datetime, messages: list[str], per: dict, order: int, lag: float) -> bool:
    """异常告警卡（red）；发送失败降级纯文本。"""
    body = "\n".join(f"• {m}" for m in messages)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"⚠️ 技术结构异常 · {now.strftime('%m-%d %H:%M')}"},
            "template": "red",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**🚨 异常清单**\n{body}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📐 结构详情**\n" + "\n".join(
                _symbol_line(_NAME[sym], per[sym]) for sym in _SYMBOLS)}},
            _note(order, lag),
        ],
    }
    ok = send_feishu_card(card)
    if not ok:
        text = "\n".join([
            f"⚠️ 技术结构异常 · {now.strftime('%m-%d %H:%M')}",
            *[f"• {m}" for m in messages],
            *[_symbol_line(_NAME[sym], per[sym]) for sym in _SYMBOLS],
        ])
        ok = send_feishu_text(text)
    return ok


def _push_recover(now: datetime, per: dict, order: int, lag: float) -> bool:
    """异常恢复卡（green）；发送失败降级纯文本。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"✅ 技术结构恢复 · {now.strftime('%m-%d %H:%M')}"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📐 当前结构**\n" + "\n".join(
                _symbol_line(_NAME[sym], per[sym]) for sym in _SYMBOLS)}},
            _note(order, lag),
        ],
    }
    ok = send_feishu_card(card)
    if not ok:
        text = "\n".join([
            f"✅ 技术结构恢复 · {now.strftime('%m-%d %H:%M')}",
            *[_symbol_line(_NAME[sym], per[sym]) for sym in _SYMBOLS],
        ])
        ok = send_feishu_text(text)
    return ok


def _note(order: int, lag: float) -> dict:
    """卡片脚注：检测口径（小字灰显）。"""
    return {"tag": "note", "elements": [{"tag": "plain_text",
            "content": (f"口径：1h 结构 + 4h 完整性均取已收盘 K 线 · 摆动阶数 {order} · "
                        f"触发延迟 {lag:.0f}s · 关键点位为最近摆动高/低点")}]}