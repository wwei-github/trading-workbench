# -*- coding: utf-8 -*-
"""每日归因快照（docs/16 §21，D6，2026-10-28）。

问题：本轮的每条结论（4h 门是不是砍了利润、pos 闸有没有用、质量分下限该不该开）
都是我临时写脚本从生产库现算的，第二天要重算一遍，脚本散在 `/tmp` 里且口径
互不一致——最典型的是"延迟成交"用 `opened_at` 分钟数代理，结论完全错误。

做法：每天固定一个时点，把同一套口径算一遍、**原样落库**（`task_records.detail`），
并推一张飞书卡片。落库是为了"当时的数字"可回溯：后面的改动（新开关、新阈值）
不会反过来改写历史归因。

设计约束：
- 只读生产库，不写业务表（唯一写入是 `task_records` 一行）；
- 分档在 Python 里做，不写复杂的 SQL CASE——口径要能被单测固定住；
- 任何子块算失败只让该块为 None，不拖垮整份快照（半份快照 > 没有快照）；
- 不参与任何交易决策。
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ops import AppLog
from app.models.scan import AIAnalysis, ScanRecord, ScanResult
from app.models.shadow import ShadowMark
from app.models.trade import TradeRecord

logger = logging.getLogger(__name__)

# 分档口径（与分析脚本保持一致，改这里就要同步改 docs/16 的表格说明）
STOP_ATR_BUCKETS = ((0.0, 1.0, "<1ATR"), (1.0, 1.5, "1-1.5ATR"),
                    (1.5, 2.0, "1.5-2ATR"), (2.0, 99.0, "≥2ATR"))
POS_BUCKETS = ((0.0, 0.25, "pos<0.25"), (0.25, 0.6, "pos 0.25-0.6"),
               (0.6, 99.0, "pos≥0.6"))
ROOM_BUCKETS = ((0.0, 1.0, "room<1R"), (1.0, 1.5, "room 1-1.5R"),
                (1.5, 2.5, "room 1.5-2.5R"), (2.5, 99.0, "room≥2.5R"))
# 回放侧新增两维（17-复盘审计 P0-1）：计划盈亏比与质量分，分档与实盘/日报口径一致
PLANNED_RR_BUCKETS = ((0.0, 1.5, "<1.5R"), (1.5, 2.0, "1.5-2R"),
                      (2.0, 3.0, "2-3R"), (3.0, 999.0, "≥3R"))
QUALITY_BUCKETS = ((0.0, 60.0, "<60"), (60.0, 70.0, "60-69"),
                   (70.0, 80.0, "70-79"), (80.0, 90.0, "80-89"),
                   (90.0, 999.0, "≥90"))


def _bucket(value: Optional[float], buckets) -> Optional[str]:
    """把数值落进分档；None/非数返回 None（不进任何档，也不进"未知"）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for lo, hi, label in buckets:
        if lo <= v < hi:
            return label
    return None


# 公开别名：/review/stats 复用同一分档口径；_bucket 保留给既有测试与内部调用
bucket_value = _bucket


def summarize_r(rows) -> dict:
    """一组已平仓单的汇总：n / 胜率 / ΣR / 均R（R = pnl_pct/100）。"""
    rs = []
    for r in rows:
        v = r.get("pnl_pct") if isinstance(r, dict) else None
        if v is None:
            continue
        try:
            rs.append(float(v) / 100.0)
        except (TypeError, ValueError):
            continue
    n = len(rs)
    if n == 0:
        return {"n": 0, "win_rate": None, "sum_r": 0.0, "avg_r": None}
    wins = sum(1 for x in rs if x > 0)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "sum_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / n, 3),
    }


def group_by(rows: list[dict], key_fn) -> dict:
    """按 key 分组后逐组 summarize_r；用于信号族/触点/分档等所有"按 X 拆"的表。"""
    out: dict[str, list] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        out.setdefault(str(k), []).append(r)
    return {k: summarize_r(v) for k, v in sorted(out.items())}


def _day_window(day: date) -> tuple[datetime, datetime]:
    """UTC 日窗口 [当天 00:00, 次日 00:00)。"""
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def _diag(row: TradeRecord) -> dict:
    """交易记录里的入场诊断（entry_diag，2026-10-27 起落库）。"""
    raw = row.raw if isinstance(row.raw, dict) else {}
    diag = raw.get("entry_diag")
    return diag if isinstance(diag, dict) else {}


def build_snapshot(db: Session, day: Optional[date] = None) -> dict:
    """构建某 UTC 日的归因快照（JSON 可序列化）。任何子块失败置 None。"""
    day = day or (datetime.now(timezone.utc).date() - timedelta(days=1))
    start, end = _day_window(day)
    snap: dict[str, Any] = {
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # 1) 扫描漏斗：把该日每轮的 funnel_json 累加（口径见 docs/16 §1）
    try:
        rounds = db.execute(
            select(ScanRecord).where(ScanRecord.started_at >= start,
                                     ScanRecord.started_at < end)
        ).scalars().all()
        stages: dict[str, int] = {}
        htf_states: dict[str, int] = {}
        universe = scanned = errors = 0
        for r in rounds:
            fj = r.funnel_json if isinstance(r.funnel_json, dict) else {}
            universe += int(fj.get("universe") or 0)
            scanned += int(fj.get("scanned") or 0)
            errors += int(fj.get("errors") or 0)
            for k, v in (fj.get("stages") or {}).items():
                stages[k] = stages.get(k, 0) + int(v or 0)
            for k, v in (fj.get("htf_states") or {}).items():
                htf_states[k] = htf_states.get(k, 0) + int(v or 0)
        snap["funnel"] = {
            "rounds": len(rounds),
            "universe": universe,
            "scanned": scanned,
            "errors": errors,
            "stages": dict(sorted(stages.items(), key=lambda kv: -kv[1])),
            "htf_states": dict(sorted(htf_states.items(), key=lambda kv: -kv[1])),
        }
    except Exception:
        logger.warning("归因快照：漏斗块失败", exc_info=True)
        snap["funnel"] = None

    # 2) 候选与 AI：命中（scan_results）/ suggest（ai_analyses）
    try:
        res_rows = db.execute(
            select(ScanResult).where(ScanResult.created_at >= start,
                                     ScanResult.created_at < end)
        ).scalars().all()
        snap["candidates"] = {
            "n": len(res_rows),
            "by_signal": group_by([{"signal_type": r.signal_type} for r in res_rows],
                                  lambda x: x["signal_type"]),
            "by_touch": group_by([{"touch_ref": r.touch_ref} for r in res_rows],
                                 lambda x: x["touch_ref"]),
            "avg_quality": (
                round(sum(float(r.quality_score) for r in res_rows
                          if r.quality_score is not None) /
                      max(1, sum(1 for r in res_rows if r.quality_score is not None)), 1)
                if any(r.quality_score is not None for r in res_rows) else None),
        }
    except Exception:
        logger.warning("归因快照：候选块失败", exc_info=True)
        snap["candidates"] = None

    try:
        ai_rows = db.execute(
            select(AIAnalysis).where(AIAnalysis.created_at >= start,
                                     AIAnalysis.created_at < end)
        ).scalars().all()
        dec: dict[str, int] = {}
        for a in ai_rows:
            dec[a.trade_decision or "?"] = dec.get(a.trade_decision or "?", 0) + 1
        snap["ai"] = {"n": len(ai_rows), "by_decision": dec}
    except Exception:
        logger.warning("归因快照：AI 块失败", exc_info=True)
        snap["ai"] = None

    # 3) 当日已平仓交易：总量 + 按信号族/出场/分档
    try:
        closed = db.execute(
            select(TradeRecord).where(TradeRecord.closed_at >= start,
                                      TradeRecord.closed_at < end,
                                      TradeRecord.pnl_pct.isnot(None),
                                      TradeRecord.risk_amount > 1)
        ).scalars().all()
        live = [r for r in closed if not r.testnet]
        rows = [{"symbol": r.symbol, "direction": r.direction,
                 "signal_type": r.signal_type, "exit_reason": r.exit_reason,
                 "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                 "diag": _diag(r)} for r in live]
        touch_by_id = {}
        ids = [r.scan_result_id for r in live if r.scan_result_id]
        if ids:
            for sr in db.execute(select(ScanResult).where(ScanResult.id.in_(ids))).scalars():
                touch_by_id[str(sr.id)] = sr.touch_ref
        for row, rec in zip(rows, live):
            row["touch_ref"] = touch_by_id.get(str(rec.scan_result_id)) if rec.scan_result_id else None
        snap["trades"] = {
            "total": summarize_r(rows),
            "by_signal": group_by(rows, lambda x: x["signal_type"]),
            "by_touch": group_by(rows, lambda x: x["touch_ref"]),
            "by_exit": group_by(rows, lambda x: x["exit_reason"] or "?"),
            "by_direction": group_by(rows, lambda x: x["direction"]),
            "buckets": {
                "stop_atr": group_by(rows, lambda x: _bucket(x["diag"].get("stop_atr"), STOP_ATR_BUCKETS)),
                "pos": group_by(rows, lambda x: _bucket(x["diag"].get("pos"), POS_BUCKETS)),
                "room_r": group_by(rows, lambda x: _bucket(x["diag"].get("room_r"), ROOM_BUCKETS)),
                "entry_path": group_by(rows, lambda x: x["diag"].get("entry_path")),
            },
            "worst": sorted(
                [{"symbol": x["symbol"], "direction": x["direction"],
                  "signal_type": x["signal_type"], "exit_reason": x["exit_reason"],
                  "r": round((x["pnl_pct"] or 0) / 100.0, 2)}
                 for x in rows if (x["pnl_pct"] or 0) < 0], key=lambda z: z["r"])[:5],
        }
    except Exception:
        logger.warning("归因快照：交易块失败", exc_info=True)
        snap["trades"] = None

    # 4) 闸门侧：被拦候选的 probe 命中率（D1 落库的 touch_ref/golden）
    try:
        marks = db.execute(
            select(ShadowMark.feature_key, ShadowMark.value_json).where(
                ShadowMark.created_at >= start, ShadowMark.created_at < end,
                ShadowMark.feature_key == "htf_blocked",
                ShadowMark.ref_type == "blocked_scan")
        ).all()
        tot = touch = golden = both = 0
        for _fk, vj in marks:
            tot += 1
            _vj = vj if isinstance(vj, dict) else {}
            probe = _vj.get("probe") if isinstance(_vj, dict) else None
            if not isinstance(probe, dict):
                continue
            if probe.get("touch_ref"):
                touch += 1
            if probe.get("golden"):
                golden += 1
            if probe.get("touch_ref") and probe.get("golden"):
                both += 1
        snap["blocked_probe"] = {
            "n": tot, "has_touch": touch, "has_golden": golden, "touch_and_golden": both,
            "pct_touch": round(touch / tot * 100, 1) if tot else None,
            "pct_full": round(both / tot * 100, 1) if tot else None,
        }
    except Exception:
        logger.warning("归因快照：被拦候选块失败", exc_info=True)
        snap["blocked_probe"] = None

    # 5) 异常面：日志级别计数 + 该日错误
    try:
        lv = dict(db.execute(
            select(AppLog.level, func.count()).where(
                AppLog.created_at >= start, AppLog.created_at < end).group_by(AppLog.level)
        ).all())
        snap["logs"] = {str(k): int(v) for k, v in lv.items()}
    except Exception:
        logger.warning("归因快照：日志块失败", exc_info=True)
        snap["logs"] = None

    return snap


def format_card(snap: dict) -> dict:
    """把快照渲染成一张飞书卡片（只放当日最该看的六个数字 + 最差三笔）。"""
    day = snap.get("day") or "—"
    tr = snap.get("trades") or {}
    total = tr.get("total") or {}
    funnel = snap.get("funnel") or {}
    ai = snap.get("ai") or {}
    probe = snap.get("blocked_probe") or {}

    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    sum_r = total.get("sum_r")
    win = total.get("win_rate")
    n = total.get("n") or 0
    color = "red" if (sum_r or 0) < 0 else "green" if (sum_r or 0) > 0 else "grey"

    lines = []
    if total:
        lines.append(f"已平仓 **{n}** 笔 · ΣR **{sum_r:+.2f}** · 胜率 **{win}%**" if n else "当日无已平仓交易")
    if funnel:
        lines.append(
            f"扫描 {funnel.get('rounds')} 轮 · 币池 {funnel.get('universe')} · "
            f"扫描 {funnel.get('scanned')} · 候选 {(snap.get('candidates') or {}).get('n')} · "
            f"分析 {ai.get('n')}")
    if probe.get("n"):
        lines.append(f"被 4h 门拦下 {probe['n']} 条 · 真实触达 **{probe.get('pct_full')}%**"
                     f"（有触点 {probe.get('pct_touch')}%）")
    worst = tr.get("worst") or []
    if worst:
        lines.append("最差三笔：" + "、".join(
            f"{w['symbol']} {w['r']:+.2f}R" for w in worst[:3]))
    logs = snap.get("logs") or {}
    if logs.get("ERROR") or logs.get("CRITICAL"):
        lines.append(f"⚠️ 当日 ERROR {logs.get('ERROR', 0)} / CRITICAL {logs.get('CRITICAL', 0)}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"📊 每日归因快照 · {day}"},
                   "template": color},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "\n".join(lines) or "（无数据）"}},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": "口径：UTC 日 · 正式网 · pnl_pct 已非空；明细见 任务记录→每日归因快照"}]},
        ],
    }
