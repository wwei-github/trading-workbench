"""现货执行服务（docs/18 P1：半自动确认后的下单闭环）

三入口共用（飞书卡片确认按钮 / 周度定投任务 / 手动 API）：
统一风控 → 市价单 → spot_orders 落库（FILLED / FAILED）。

风控铁律（docs/18 §8，实盘口径）：
- 白名单硬编码 SPOT_SYMBOLS，其他 symbol 直接拒绝
- BUY：单币累计投入（FILLED BUY 之和，按账号口径）+ 本笔 ≤ max_total_usdt；
  本笔 ≥ MIN_NOTIONAL_USDT；买入后保留垫 ≥ usdt_reserve_pct × 买入前 free USDT
  （§5 的 spot_budget 全局预算列未落地，实盘按现金池比例兜底，注释在案）
- SELL：只动波段仓——数量 ≤ 该币 free × 30%（底仓 ≥70% 永不卖，§3.3）；
  名义值 ≥ MIN_NOTIONAL_USDT
- signal 触发单币每日最多 1 次（冷却防连日追买）；manual / dca 不受限

锁：lock:spot_open:{account_key}，现货自写 SET NX PX 简单锁——
不碰合约 trade_tasks 的 _acquire_open_lock（零侵入约束）。
"""
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from redis import Redis
from sqlalchemy.orm import Session

from app.config import settings
from app.models.spot import SPOT_SYMBOLS, SpotOrder
from app.services.spot_engine import MIN_NOTIONAL_USDT

logger = logging.getLogger("app.spot")

_TZ_BJ = timezone(timedelta(hours=8))

# 波段可卖上限：底仓 ≥70% 锁定（docs/18 §3.3）
SWING_SELL_CAP = 0.30
# 市价单秒级完成；TTL 防进程崩溃死锁
_SPOT_LOCK_TTL_S = 30


def _acquire_spot_lock(r: Redis, ident: str, account_key: str, wait_s: int = 10) -> bool:
    """现货执行互斥锁（SET NX PX + 自旋等待）。"""
    key = f"lock:spot_open:{account_key}"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if r.set(key, ident, nx=True, px=_SPOT_LOCK_TTL_S * 1000):
            return True
        time.sleep(0.2)
    return False


def _release_spot_lock(r: Redis, ident: str, account_key: str) -> None:
    key = f"lock:spot_open:{account_key}"
    try:
        # 只释放自己持有的（TTL 过期被他人持有时不碰）
        if r.get(key) == ident:
            r.delete(key)
    except Exception:
        logger.warning("spot 锁释放失败（%s），等待 TTL 过期", key)


def _symbol_invested(db: Session, symbol: str, account_key: str) -> float:
    """单币累计投入（FILLED BUY 金额之和，按账号口径）。"""
    rows = (
        db.query(SpotOrder)
        .filter(
            SpotOrder.symbol == symbol,
            SpotOrder.side == "BUY",
            SpotOrder.status == "FILLED",
            SpotOrder.account_key == account_key,
        )
        .all()
    )
    return sum(float(o.quote_amount or 0) for o in rows)


def _signal_filled_today(db: Session, symbol: str, account_key: str) -> bool:
    """该币今日（北京日历日）是否已有 signal 成交单（每日一次冷却）。"""
    today = datetime.now(_TZ_BJ).strftime("%Y-%m-%d")
    rows = (
        db.query(SpotOrder.executed_at)
        .filter(
            SpotOrder.symbol == symbol,
            SpotOrder.trigger_type == "signal",
            SpotOrder.status == "FILLED",
            SpotOrder.account_key == account_key,
        )
        .all()
    )
    for (executed_at,) in rows:
        if executed_at is None:
            continue
        # 落库为 naive UTC（datetime.utcnow），显式挂 UTC 后转北京日
        bj = executed_at.replace(tzinfo=timezone.utc).astimezone(_TZ_BJ)
        if bj.strftime("%Y-%m-%d") == today:
            return True
    return False


def exec_spot_order(
    db: Session,
    symbol: str,
    side: str,
    quote_amount: Optional[float] = None,
    qty: Optional[float] = None,
    lots: Optional[int] = None,
    trigger_type: str = "manual",
    trigger_detail: Optional[dict] = None,
    account_key: Optional[str] = None,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """执行现货市价单并落库（三入口共用，不抛异常——失败也落 FAILED 行）。

    入参口径（三选一由 side 决定）：
    - BUY：quote_amount 优先，其次 lots × per_order_usdt（按金额买，价格无关）
    - SELL：qty 优先（须先过波段仓 30% 上限裁剪），其次按 lots 折算
      （lots × per_order_usdt ÷ 现价，与 free × 30% 取小）

    返回 {ok, reason?, order_id?}；ok=False 时 reason 必填（风控拒绝也返回，
    不落库——除币安下单抛错外，纯拒绝不留失败行，避免噪音）。
    """
    from app.services.spot_client import SpotClient

    if symbol not in SPOT_SYMBOLS:
        return {"ok": False, "reason": f"非白名单币种 {symbol}（仅 {'/'.join(SPOT_SYMBOLS)}）"}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "reason": f"非法方向 {side}"}
    if trigger_type not in ("signal", "dca", "manual"):
        return {"ok": False, "reason": f"非法触发类型 {trigger_type}"}

    own_client = client is None
    if client is None:
        client = SpotClient(account_key)
        if client.account is None:
            return {"ok": False, "reason": client.resolve_error or "现货账号未配置"}
    acc = client.account
    acc_key = acc.key

    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = uuid4().hex
    if not _acquire_spot_lock(r, ident, acc_key):
        return {"ok": False, "reason": "系统忙（该账号有执行中任务），请稍后重试"}

    order_row = None
    try:
        s = _load_settings(db)
        notes: list = []

        # ---- 金额/数量归一（lots 兜底折算） ----
        if side == "BUY":
            quote = float(quote_amount or 0) or (lots or 0) * s["per_order_usdt"]
            if quote <= 0:
                return {"ok": False, "reason": "缺少买入金额（quote_amount / lots）"}
        else:
            sell_qty = float(qty or 0)
            if sell_qty <= 0 and lots:
                price_now = client.ticker_price(symbol)
                if not price_now:
                    return {"ok": False, "reason": "获取现价失败，无法折算卖出数量"}
                sell_qty = lots * s["per_order_usdt"] / price_now
            if sell_qty <= 0:
                return {"ok": False, "reason": "缺少卖出数量（qty / lots）"}

        # ---- 风控（铁律 §8 #1/2/3/5/6） ----
        if side == "BUY":
            invested = _symbol_invested(db, symbol, acc_key)
            remaining = s["max_total_usdt"] - invested
            if remaining <= 0:
                return {"ok": False, "reason": f"已达单币投入上限 {s['max_total_usdt']:,.0f} U，到顶只允许卖出"}
            if quote > remaining + 1e-9:
                quote = math.floor(remaining * 100) / 100
                notes.append(f"按单币投入上限裁剪至 {quote:,.2f} U")
            free_usdt = client.asset_balance("USDT")
            if free_usdt is None:
                return {"ok": False, "reason": "查询 USDT 余额失败"}
            spendable = free_usdt * (1 - s["usdt_reserve_pct"])
            if quote > spendable:
                quote = math.floor(spendable * 100) / 100
                notes.append(
                    f"按 USDT 保留垫（{s['usdt_reserve_pct']:.0%} × {free_usdt:,.2f}）裁剪至 {quote:,.2f} U"
                )
            if quote < MIN_NOTIONAL_USDT:
                return {
                    "ok": False,
                    "reason": f"买入金额 {quote:,.2f} U 低于最小名义 {MIN_NOTIONAL_USDT:.0f} U（保留垫/上限裁剪后不足）",
                }
            exec_qty, exec_quote = None, quote
        else:
            free_qty = client.asset_balance(symbol.replace("USDT", ""))
            if free_qty is None:
                return {"ok": False, "reason": "查询持仓余额失败"}
            max_qty = free_qty * SWING_SELL_CAP
            if sell_qty > max_qty:
                sell_qty = max_qty
                notes.append(f"按波段仓上限裁剪至 free 的 {SWING_SELL_CAP:.0%}")
            sell_qty = client.round_qty(symbol, sell_qty)
            price_now = client.ticker_price(symbol)
            if not price_now or sell_qty <= 0:
                return {"ok": False, "reason": "可卖数量为 0（裁剪后低于精度）"}
            if sell_qty * price_now < MIN_NOTIONAL_USDT:
                return {"ok": False, "reason": f"卖出名义 {sell_qty * price_now:,.2f} U 低于最小名义 {MIN_NOTIONAL_USDT:.0f} U"}
            exec_qty, exec_quote = sell_qty, None

        if trigger_type == "signal" and _signal_filled_today(db, symbol, acc_key):
            return {"ok": False, "reason": "该币今日已有信号单成交（每日一次冷却）"}

        # ---- 落 PENDING_CONFIRM 行（审计：执行中崩溃可追溯） ----
        order_row = SpotOrder(
            symbol=symbol, side=side, trigger_type=trigger_type,
            trigger_detail={**(trigger_detail or {}), "risk_notes": notes} if notes else trigger_detail,
            qty=exec_qty, quote_amount=exec_quote,
            status="PENDING_CONFIRM", testnet=client.testnet, account_key=acc_key,
        )
        db.add(order_row)
        db.commit()

        # ---- 市价单 ----
        try:
            result = client.market_order(
                symbol, side,
                quantity=exec_qty if side == "SELL" else None,
                quote_order_qty=exec_quote if side == "BUY" else None,
            )
        except Exception as e:
            db.rollback()
            order_row.status = "FAILED"
            order_row.fail_reason = str(e)[:500]
            db.commit()
            logger.warning("spot 市价单失败 %s %s: %s", side, symbol, e)
            return {"ok": False, "reason": f"币安下单失败：{str(e)[:120]}"}

        order_row.status = "FILLED" if result["status"] == "FILLED" else result["status"] or "FILLED"
        order_row.binance_order_id = result["order_id"]
        order_row.qty = result["executed_qty"] or None
        order_row.quote_amount = result["quote_spent"] or None
        order_row.price = result["avg_price"]
        order_row.executed_at = datetime.utcnow()
        db.commit()

        logger.info(
            "spot %s %s 成交：qty=%s quote=%s 均价=%s（%s/%s）%s",
            side, symbol, result["executed_qty"], result["quote_spent"],
            result["avg_price"], trigger_type, acc_key,
            "；".join(notes),
        )
        return {
            "ok": True,
            "order_id": str(order_row.id),
            "executed_qty": result["executed_qty"],
            "quote_spent": result["quote_spent"],
            "avg_price": result["avg_price"],
            "risk_notes": notes,
            "testnet": client.testnet,
        }
    finally:
        _release_spot_lock(r, ident, acc_key)
        if own_client:
            try:
                client.close()
            except Exception:
                pass


def _load_settings(db: Session) -> Dict[str, Any]:
    from app.services.spot_settings import load_spot_settings

    return load_spot_settings(db)
