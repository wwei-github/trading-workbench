"""现货屯币任务（docs/18 P0：只算不发单）

spot-daily-check（每日 08:08，日K收盘 BJ 08:00 后）：
拉三币 日/周/月K → 落 spot_kline_cache → evaluate_day 评分（纯函数）→
飞书卡片推送评分与建议份数（纯展示，不下单——P1 接确认下单）。

零侵入约束（docs/18 §1）：独立任务命名 spot-*；不读写合约任何表；
复用仅限 task_log / notify / spot_client 基础设施。
"""
import logging
from datetime import datetime, timedelta, timezone

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
    """K线缓存 upsert：只存精简列 [t,o,h,l,c,v]。"""
    slim = [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        for k in raw
    ]
    row = (
        db.query(SpotKlineCache)
        .filter(SpotKlineCache.symbol == symbol, SpotKlineCache.interval == interval)
        .first()
    )
    if row is None:
        db.add(SpotKlineCache(symbol=symbol, interval=interval, klines=slim))
    else:
        row.klines = slim
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


# ---------------- 回测（docs/18 §10，P1） ----------------

@celery_app.task(name="app.tasks.spot_tasks.run_backtest_task")
def run_backtest_task(bt_id: str) -> dict:
    """回测任务：读 spot_backtests 参数 → 从 spot_kline_cache/币安拉三周期K线 →
    run_backtest 日历重放 → 结果落库（summary/daily_series/orders_detail）。
    纯计算秒级~十秒级；AI 复核跳过（§10.4 唯一差异）。"""
    from app.database import SessionLocal
    from app.models.spot import SpotBacktest, SpotKlineCache
    from app.services.spot_backtest import run_backtest
    from app.services.spot_client import SpotClient
    from app.services.spot_engine import SpotDefaults
    from app.services.spot_settings import load_spot_settings
    from app.services.task_log import close_task, open_task

    task_id = open_task("spot", f"回测 {bt_id[:8]}")
    db = SessionLocal()
    try:
        row = db.get(SpotBacktest, bt_id)
        if row is None:
            raise ValueError(f"回测 {bt_id} 不存在")
        p = row.params

        # 配置：请求覆盖 > spot_settings > 默认
        s = load_spot_settings(db)
        cfg = SpotDefaults(
            per_order_usdt=p.get("per_order_usdt") or s["per_order_usdt"],
            max_total_usdt=s["max_total_usdt"],
            dca_enabled=p.get("dca_enabled") if p.get("dca_enabled") is not None else s["dca_enabled"],
            dca_amount=p.get("dca_amount") or s["dca_amount"],
        )

        # K线：优先缓存，不足/缺失则从币安拉全量历史回补
        client = SpotClient()
        klines = {"daily": {}, "weekly": {}, "monthly": {}}
        try:
            for sym in p["symbols"]:
                need_daily = 400  # MA200 + 环境缓冲
                daily = _cache_klines(db, sym, "1d")
                if len(daily) < need_daily:
                    daily = _merge_klines(daily, client.klines(sym, "1d", need_daily + 100))
                    _upsert_cache(db, sym, "1d", daily)
                weekly = _cache_klines(db, sym, "1w")
                if len(weekly) < 60:
                    weekly = _merge_klines(weekly, client.klines(sym, "1w", 160))
                    _upsert_cache(db, sym, "1w", weekly)
                monthly = _cache_klines(db, sym, "1M")
                if len(monthly) < 14:
                    monthly = _merge_klines(monthly, client.klines(sym, "1M", 150))
                    _upsert_cache(db, sym, "1M", monthly)
                klines["daily"][sym] = daily
                klines["weekly"][sym] = weekly
                klines["monthly"][sym] = monthly
            db.commit()
        finally:
            client.close()

        result = run_backtest(
            p["symbols"], klines["daily"], klines["weekly"], klines["monthly"],
            budget=float(p["budget"]), settings=cfg,
            start=p.get("start"), end=p.get("end"),
        )
        row.summary = result["summary"]
        row.daily_series = result["daily_series"]
        row.orders_detail = {"items": result["orders"]}
        row.status = "DONE"
        row.finished_at = datetime.utcnow()
        db.commit()
        close_task(
            task_id, "completed",
            summary=(
                f"{'/'.join(p['symbols'])} {p['start']}~{p['end']} "
                f"{result['summary']['orders_count']}笔 "
                f"超额 {result['summary']['excess_qty']}"
            ),
            detail={"backtest_id": str(row.id)},
        )
        return {"ok": True, "orders": result["summary"]["orders_count"]}
    except Exception as e:
        db.rollback()
        try:
            row = db.get(SpotBacktest, bt_id)
            if row is not None:
                row.status = "FAILED"
                row.error = str(e)[:2000]
                row.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("回测失败状态写入失败")
        close_task(task_id, "failed", error=str(e))
        logger.exception("现货回测失败")
        raise
    finally:
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


_ACTION_TEXT = {
    "BUY": ("低买", "green"),
    "SELL": ("高卖", "red"),
    "HOLD": ("观望", "blue"),
}


def _push_card(decisions: list, settings_dict: dict) -> bool:
    """飞书评分卡片（纯展示：评分/建议份数/三周期计划，无按钮——P1 加确认下单）。"""
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

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🪙 现货屯币日报 · {date_label}"},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**三币屯币评分**"}},
            {"tag": "column_set", "flex_mode": "stretch", "columns": cols},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                "P0 半自动：评分仅供参考，不自动下单（docs/18）。"
                f"单份 {settings_dict['per_order_usdt']:,.0f} U · 单币上限 {settings_dict['max_total_usdt']:,.0f} U"
            )}]},
        ],
    }
    try:
        from app.services.notify import send_feishu_card

        return send_feishu_card(card)
    except Exception as e:
        logger.warning("现货评分卡片推送失败: %s", e)
        return False
