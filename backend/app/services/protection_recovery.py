"""Durable recovery for positions whose exchange protection failed."""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models.trade import ProtectionIncident, TradeRecord
from app.services.trade_locks import mark_pending_sweep_active

logger = logging.getLogger(__name__)
# 未闭环的事故状态：账号封锁与巡检取数共用同一口径（勿在调用方另写字面量）
ACTIVE_STATUSES = ("pending", "closing", "awaiting_reconcile", "manual_review")
_ACTIVE = ACTIVE_STATUSES


def has_active_incident(db, account_key: str, testnet: bool) -> bool:
    return db.execute(select(ProtectionIncident.id).where(
        ProtectionIncident.account_key == account_key,
        ProtectionIncident.testnet == testnet,
        ProtectionIncident.status.in_(_ACTIVE),
    ).limit(1)).first() is not None


def create_incident_after_rollback(db, *, account_key, testnet, symbol, direction,
                                   reason, qty, trade_record_id=None,
                                   entry_order_id=None, entry_price=None,
                                   opened_at=None, error=None, rollback_session=True,
                                   status="pending"): 
    """Persist via a fresh session so the failed trading transaction cannot erase it."""
    # 解除挂单巡检空转短路：事故要在下一轮巡检（≤15 分钟）重试，不能被缓存挡住
    mark_pending_sweep_active()
    incident = ProtectionIncident(
        account_key=account_key, testnet=bool(testnet), symbol=symbol,
        direction=direction, reason=reason, qty=max(float(qty or 0), 0),
        trade_record_id=trade_record_id, entry_order_id=str(entry_order_id) if entry_order_id else None,
        entry_price=entry_price, opened_at=opened_at, status=status,
        last_error=str(error or "")[:500] or None,
        next_retry_at=datetime.utcnow(),
    )
    if rollback_session:
        db.rollback()
    independent = SessionLocal()
    try:
        existing = independent.execute(select(ProtectionIncident).where(
            ProtectionIncident.account_key == account_key,
            ProtectionIncident.testnet == bool(testnet),
            ProtectionIncident.symbol == symbol,
            ProtectionIncident.trade_record_id == trade_record_id,
            ProtectionIncident.status.in_(_ACTIVE),
        ).limit(1)).scalar_one_or_none()
        if existing is not None:
            existing.last_error = str(error or existing.last_error or "")[:500] or None
            independent.commit()
            return existing
        independent.add(incident)
        independent.commit()
        independent.refresh(incident)
        return incident
    except Exception:
        independent.rollback()
        logger.exception("保护事故持久化失败 account=%s symbol=%s", account_key, symbol)
        raise
    finally:
        independent.close()


_RETRYABLE_REJECTIONS = ("-2022", "-2019", "-4130", "-4164")


def status_for_close_error(error) -> str:
    """Classify an emergency-close failure: only explicit rejections are retryable.

    A timeout/connection error may have delivered the order, so it must never be
    re-sent automatically. Unknown errors are treated the same conservative way.
    """
    text = str(error or "")
    return "pending" if any(code in text for code in _RETRYABLE_REJECTIONS) else "manual_review"


def _mark_review(row, message, *, now=None):
    now = now or datetime.utcnow()
    row.status = "manual_review"
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.last_error = str(message)[:500]
    # 人工复核是终态等待区：仓位仍在时每小时静默复查一次，避免每分钟重写/重发告警。
    # 空仓解除不依赖该退避——recover_due_incidents 每轮都会先做空仓判定。
    row.next_retry_at = now + timedelta(hours=1)


def _escalate(db, row, message, *, now=None):
    """Escalate to manual review; alert only on the first transition."""
    first = row.status != "manual_review"
    _mark_review(row, message, now=now)
    db.commit()
    if first:
        _alert(row)


def _effective_entry_price(db, row) -> float:
    """Incident entry price, falling back to the linked TradeRecord when missing.

    Some failure paths persist the incident after the trade row was rolled back,
    so the incident may carry no entry price. A linked record is a safe fallback;
    without either value the generation stays unverifiable (manual review).
    """
    planned = float(row.entry_price or 0)
    if planned > 0:
        return planned
    if row.trade_record_id:
        rec = db.get(TradeRecord, row.trade_record_id)
        if rec is not None and rec.entry_price:
            return float(rec.entry_price)
    return 0.0


def position_matches_generation(row, position, expected_entry=None):
    """Conservative Binance one-way position match; require same direction and entry.

    Positions without a usable exchange entry price are never auto-closed.
    expected_entry lets callers supply the effective incident entry (e.g. from the
    linked TradeRecord when the incident row itself has no price).
    """
    entry = float(position.get("entry_price") or 0)
    planned = float(
        expected_entry if expected_entry is not None else (row.entry_price or 0)
    )
    amt = abs(float(position.get("amt") or 0))
    qty = float(row.qty or 0)
    return bool(entry > 0 and planned > 0 and qty > 0
                and (float(position.get("amt") or 0) > 0) == (row.direction == "long")
                and abs(entry - planned) / planned <= 0.001
                and amt <= qty * 1.001)


_STOP_ORDER_TYPES = ("STOP", "STOP_MARKET")


def position_is_protected(trader, row, position) -> bool:
    """Return True only when an open stop covers the whole remaining position.

    Protection restored by a later rehang/manual order releases the incident
    without sending another close. Read failures or ambiguous orders never count
    as protection, so the reconciler keeps the conservative path.
    """
    try:
        price = float(trader.price(row.symbol) or 0)
        orders = trader.open_algo_orders(row.symbol) or []
    except Exception as exc:
        logger.warning("保护事故对账：读取在挂止损失败 %s: %s", row.symbol, exc)
        return False
    amt = abs(float(position.get("amt") or 0))
    close_side = "SELL" if row.direction == "long" else "BUY"
    for order in orders:
        if str(order.get("side") or "").upper() != close_side:
            continue
        otype = str(order.get("orderType") or order.get("type") or "").upper()
        if otype not in _STOP_ORDER_TYPES:
            continue
        try:
            trigger = float(order.get("triggerPrice") or 0)
        except (TypeError, ValueError):
            continue
        if trigger <= 0 or price <= 0:
            continue
        # A stop on the wrong side of the market would fire immediately or never
        # protect the position; never treat it as restored protection.
        if row.direction == "long" and trigger >= price:
            continue
        if row.direction == "short" and trigger <= price:
            continue
        if str(order.get("closePosition") or "").lower() == "true":
            return True
        try:
            qty = float(order.get("quantity") or order.get("origQty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty >= amt * (1 - 1e-9):
            return True
    return False


def _handle_position_read_failure(db, rows, now, exc):
    """A failed position snapshot must never be read as "flat"."""
    due_ids = [r.id for r in rows if not r.next_retry_at or r.next_retry_at <= now]
    for incident_id in due_ids:
        db.rollback()
        fresh = db.get(ProtectionIncident, incident_id)
        if fresh is None:
            continue
        if fresh.status == "pending":
            fresh.last_error = f"持仓/对账读取失败，可重试：{exc}"[:500]
            fresh.next_retry_at = now + timedelta(minutes=1)
            db.commit()
        else:
            _escalate(db, fresh, f"对账异常：{exc}", now=now)


def recover_due_incidents(db, trader, account_key: str, testnet: bool,
                          generation_matches=None, *, now=None):
    """Reconcile active incidents. Never repeat an uncertain close request.

    generation_matches must positively bind the observed exchange position to this
    incident. Absence of a checker is deliberately treated as ambiguous.

    The position snapshot is taken once per account for every active incident:
    a flat symbol resolves immediately even while a manual-review backoff is
    still pending, so an externally closed position releases the block without
    waiting for the hourly retry. Non-flat incidents still honor next_retry_at.
    """
    now = now or datetime.utcnow()
    rows = db.execute(select(ProtectionIncident).where(
        ProtectionIncident.account_key == account_key,
        ProtectionIncident.testnet == testnet,
        ProtectionIncident.status.in_(_ACTIVE),
    ).order_by(ProtectionIncident.created_at)).scalars().all()
    if not rows:
        return 0

    try:
        positions = trader.positions()
    except Exception as exc:
        _handle_position_read_failure(db, rows, now, exc)
        return len(rows)

    for row in rows:
        try:
            pos = positions.get(row.symbol)
            amt = float((pos or {}).get("amt") or 0)
            if not pos or amt == 0:
                # Do not cancel algo orders here: an account may already have a later
                # same-symbol position whose protection must not be removed.
                # 限价入场响应丢失（无 orderId）的残留普通挂单同理不能默认已消失：
                # 撤单确认成功后才算闭环，失败则保持封锁并下轮重试。
                if row.reason == "open_limit_entry_submit_unknown":
                    # 人工复核退避期内不重复撤单，避免每分钟打交易所/刷告警
                    if row.next_retry_at and row.next_retry_at > now:
                        continue
                    try:
                        trader.cancel_all_open_orders(row.symbol)
                    except Exception as cancel_exc:
                        first = row.status != "manual_review"
                        _mark_review(
                            row, f"撤单复核失败，无法确认无残留挂单：{cancel_exc}")
                        row.next_retry_at = now + timedelta(hours=1)
                        db.commit()
                        if first:
                            _alert(row)
                        continue
                row.status = "resolved"
                row.resolved_at = now
                row.last_error = None
                db.commit()
                continue
            if row.next_retry_at and row.next_retry_at > now:
                continue
            expected = _effective_entry_price(db, row)
            if generation_matches is not None:
                matched = generation_matches(row, pos)
            else:
                matched = position_matches_generation(row, pos, expected)
            if not matched:
                _escalate(db, row, "无法确认事故与当前同币仓位的交易世代；未发送平仓请求", now=now)
                continue
            if (amt > 0) != (row.direction == "long"):
                _escalate(db, row, "交易所持仓方向与事故记录不符；未发送平仓请求", now=now)
                continue
            # 保护已由后续补挂/人工单恢复：无需再平仓，解除账号封锁。
            if position_is_protected(trader, row, pos):
                row.status = "resolved"
                row.resolved_at = now
                row.last_error = None
                db.commit()
                logger.warning("保护事故解除 %s：当前仓位已有有效止损保护", row.symbol)
                continue
            if row.status == "closing":
                _escalate(db, row, "进程在平仓请求期间中断，无法判断请求是否送达；未重发平仓请求", now=now)
                continue
            if row.status in ("awaiting_reconcile", "manual_review"):
                if row.status == "manual_review":
                    # Already escalated: keep the block, but stop rewriting/alarming
                    # every minute. Still polled hourly so an externally closed
                    # position eventually releases the account.
                    row.next_retry_at = now + timedelta(hours=1)
                    db.commit()
                    continue
                _escalate(db, row, "存在仓位但事故动作可能已送达或无法证明当前仓位世代；未重发平仓请求", now=now)
                continue
            row.status = "closing"
            row.attempt_count = int(row.attempt_count or 0) + 1
            db.commit()  # durable intent before exchange side effect
            try:
                trader.market_order(row.symbol, "SELL" if amt > 0 else "BUY",
                                    abs(amt), reduce_only=True)
            except Exception as exc:
                text = str(exc)
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.last_error = f"平仓请求被明确拒绝，可安全重试：{text}"[:500]
                row.status = "pending" if any(code in text for code in ("-2022", "-2019", "-4130", "-4164")) else "manual_review"
                row.next_retry_at = now + timedelta(minutes=1)
                db.commit()
                if row.status == "manual_review":
                    _alert(row)
                continue
            row.status = "awaiting_reconcile"
            row.last_error = "平仓请求已返回；等待下轮仓位对账，禁止重发"
            row.next_retry_at = now + timedelta(minutes=1)
            db.commit()
        except Exception as exc:
            db.rollback()
            fresh = db.get(ProtectionIncident, row.id)
            if fresh is not None:
                if fresh.status == "pending":
                    fresh.last_error = f"持仓/对账读取失败，可重试：{exc}"[:500]
                    fresh.next_retry_at = now + timedelta(minutes=1)
                    db.commit()
                else:
                    _escalate(db, fresh, f"对账异常：{exc}", now=now)
    return len(rows)


def _alert(row):
    logger.critical("保护事故需人工复核 account=%s symbol=%s incident=%s: %s",
                    row.account_key, row.symbol, row.id, row.last_error)
    try:
        from app.services.notify import send_feishu_text
        send_feishu_text(
            f"保护事故需人工复核：{row.account_key} {row.symbol}，事故 {row.id}。"
            f"原因：{row.last_error}", testnet=bool(row.testnet))
    except Exception:
        logger.exception("保护事故告警发送失败 incident=%s", row.id)
