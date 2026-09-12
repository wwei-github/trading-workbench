"""自动交易引擎（docs/06）

开仓（try_open_trades）：
    候选 = AI 结论 suggest 且推荐度 ≥60 且未开过仓，按推荐度降序
    逐个过闸门（在跑单上限 / 余额 70% 规则 / 同币无仓 / 方向价格复验），
    固定亏损法定仓位（基数分档 ×3% ÷ 止损距离）→ 市价开仓 + 挂 SL/TP1/TP2 → 入库

结算（settle_trades）：
    以交易所为真源（持仓量/挂单/已实现盈亏）推断成交事件：
    仓位归零 → 结算收益（正/负值）；TP1/TP2 成交 → 状态迁移；
    TP2 后每次巡检跟进止损（最近3根已收盘K线极值，只收紧不放松）
"""
import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.models.scan import AIAnalysis, ScanResult
from app.models.system_config import SystemConfig
from app.models.trade import TradeEvent, TradeRecord
from app.services.binance_trader import BinanceTrader
from app.services.exchange_pool import ExchangePool

logger = logging.getLogger(__name__)


def _capital_base(wallet: float) -> float:
    """风险基数：钱包余额向下落到最近的分档档位；低于最小档返回 0（不开仓）"""
    tiers = sorted(
        float(x) for x in str(settings.TRADING_CAPITAL_TIERS).split(",") if x.strip()
    )
    base = 0.0
    for t in tiers:
        if wallet >= t:
            base = t
    return base


def _add_event(db: Session, rec: TradeRecord, event_type: str, detail: Optional[dict] = None) -> None:
    db.add(TradeEvent(trade_record_id=rec.id, event_type=event_type, detail=detail or {}))


def _close_side(direction: str) -> str:
    return "SELL" if direction == "long" else "BUY"


# ── 开仓 ──────────────────────────────────────────────────────


def _open_candidates(db: Session):
    """未开过仓的 ≥60 分 suggest 分析（推荐度降序 = 开单优先级）。
    已有 FAILED 记录的 symbol（如交易所未上架）整只排除，避免每小时空转"""
    tr_fail = aliased(TradeRecord)
    return db.execute(
        select(AIAnalysis, ScanResult)
        .join(ScanResult, AIAnalysis.scan_result_id == ScanResult.id)
        .outerjoin(TradeRecord, TradeRecord.ai_analysis_id == AIAnalysis.id)
        .where(
            AIAnalysis.trade_decision == "suggest",
            AIAnalysis.direction.in_(("long", "short")),
            AIAnalysis.entry_price > 0,
            AIAnalysis.stop_loss > 0,
            AIAnalysis.take_profit_1 > 0,
            AIAnalysis.recommendation >= settings.TRADING_MIN_RECOMMENDATION,
            TradeRecord.id.is_(None),
            ~select(tr_fail.id).where(
                tr_fail.symbol == AIAnalysis.symbol, tr_fail.status == "FAILED"
            ).exists(),
        )
        .order_by(AIAnalysis.recommendation.desc(), AIAnalysis.created_at.desc())
    ).all()


def try_open_trades(db: Session, trader: BinanceTrader) -> int:
    """按评分优先逐个尝试开仓；返回本次成功开仓数"""
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        return 0
    candidates = _open_candidates(db)
    if not candidates:
        return 0
    logger.info("自动开仓候选 %d 个", len(candidates))

    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    positions = trader.positions()  # {symbol: {...}}
    running = len(positions)
    opened = 0

    for a, _sr in candidates:
        if running >= cfg.max_open_trades:
            logger.info("在跑单子已达上限 %d，停止本轮开仓", cfg.max_open_trades)
            break
        # 余额风控（70% 规则）：可用 = 钱包 − 在跑单占用保证金
        occupied = sum(p["initial_margin"] for p in positions.values())
        if wallet > 0 and wallet - occupied < wallet * settings.TRADING_MIN_FREE_PCT:
            logger.info("可用余额低于总资金 %.0f%%，停止本轮开仓", settings.TRADING_MIN_FREE_PCT * 100)
            break
        if _capital_base(wallet) <= 0:
            logger.info("钱包余额 %s 低于最小风险档位，不开仓", wallet)
            break

        symbol = a.symbol
        if symbol in positions:
            logger.info("开仓跳过 %s：已有持仓", symbol)
            continue

        direction = a.direction
        entry_ai = float(a.entry_price)
        sl = float(a.stop_loss)
        tp1 = float(a.take_profit_1)
        tp2 = float(a.take_profit_2 or 0)
        try:
            price = trader.price(symbol)
            # 方向-价格一致性复验（分析到开仓之间价格可能移动）
            if direction == "long" and not (sl < price < tp1):
                logger.info("开仓作废 %s：现价 %s 已不在止损/止盈一区间内", symbol, price)
                continue
            if direction == "short" and not (sl > price > tp1):
                logger.info("开仓作废 %s：现价 %s 已不在止损/止盈一区间内", symbol, price)
                continue
            if abs(price - entry_ai) / entry_ai > 0.01:
                logger.info("开仓作废 %s：现价偏离 AI 入场价超 1%%", symbol)
                continue

            # 固定亏损仓位：基数分档 × 3% ÷ 止损距离
            stop_pct = abs(price - sl) / price
            risk_budget = _capital_base(wallet) * settings.TRADING_RISK_PCT / 100
            notional = risk_budget / stop_pct
            qty = trader.round_qty(symbol, notional / price)
            f = trader.filters(symbol)
            if qty < f["min_qty"] or qty * price < f["min_notional"]:
                logger.info(
                    "开仓放弃 %s：计算数量 %s 低于交易所最小规则（min_qty=%s, min_notional=%s）",
                    symbol, qty, f["min_qty"], f["min_notional"],
                )
                continue
            if qty > f["max_qty"]:
                logger.info("开仓 %s：数量 %s 超交易所单笔上限 %s，按上限缩减（实际风险低于 %.0f%% 预算）",
                            symbol, qty, f["max_qty"], settings.TRADING_RISK_PCT)
                qty = trader.round_qty(symbol, f["max_qty"])
            leverage = settings.TRADING_LEVERAGE
            margin_used = qty * price / leverage
            if margin_used > wallet - occupied:
                logger.info("开仓放弃 %s：所需保证金 %.2f 超过可用 %.2f", symbol, margin_used, wallet - occupied)
                continue
            # 实际风险金额按最终数量精确计（向下取整/上限缩减只会低于 3% 预算）
            risk_amount = round(qty * abs(price - sl), 4)

            # 下单：市价开仓 → 挂 SL/TP1/TP2 → 入库，全在保护块内：
            # 任何一步失败都紧急撤单+平仓，不留裸仓（含记录构建/入库失败）
            trader.setup_leverage(symbol, leverage)
            side = "BUY" if direction == "long" else "SELL"
            close_side = _close_side(direction)
            entry_order = trader.market_order(symbol, side, qty)
            try:
                sl_order = trader.stop_market_close(
                    symbol, close_side, trader.round_price(symbol, sl))
                qty_tp1 = trader.round_qty(symbol, qty * 0.5)
                qty_tp2 = trader.round_qty(symbol, qty * 0.25)
                tp1_order = trader.take_profit_reduce(
                    symbol, close_side, qty_tp1, trader.round_price(symbol, tp1))
                tp2_order = None
                if tp2 > 0 and qty_tp2 >= f["min_qty"] and qty_tp1 + qty_tp2 < qty:
                    tp2_order = trader.take_profit_reduce(
                        symbol, close_side, qty_tp2, trader.round_price(symbol, tp2))
                now = datetime.utcnow()
                rec = TradeRecord(
                    symbol=symbol,
                    direction=direction,
                    scan_result_id=a.scan_result_id,
                    ai_analysis_id=a.id,
                    recommendation=float(a.recommendation) if a.recommendation is not None else None,
                    entry_price=price,
                    qty=qty,
                    notional=round(qty * price, 2),
                    leverage=leverage,
                    margin_mode="isolated",
                    margin_used=round(margin_used, 4),
                    risk_amount=round(risk_amount, 4),
                    stop_loss=sl,
                    tp1=tp1,
                    tp2=tp2 if tp2_order else 0,
                    status="OPENED",
                    opened_at=now,
                    raw={
                        # SL/TP 为 algo 条件单，标识是 algoId（MARKET 入场单仍是 orderId）
                        "entry_order_id": entry_order["orderId"],
                        "sl_order_id": sl_order.get("algoId") or sl_order.get("orderId"),
                        "tp1_order_id": tp1_order.get("algoId") or tp1_order.get("orderId"),
                        "tp2_order_id": (tp2_order.get("algoId") or tp2_order.get("orderId")) if tp2_order else None,
                        "qty_tp1": qty_tp1,
                        "qty_tp2": qty_tp2 if tp2_order else 0,
                        "capital_base": _capital_base(wallet),
                        "wallet": wallet,
                        "testnet": settings.TRADING_TESTNET,
                    },
                )
                db.add(rec)
                db.flush()
                _add_event(db, rec, "OPEN", {
                    "price": price, "qty": qty, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
                    "risk_amount": round(risk_amount, 4), "entry_order_id": entry_order["orderId"],
                })
                db.commit()
            except Exception:
                # 紧急处理：先撤全部挂单再只减仓平仓（reduceOnly：开仓单未成交时会被
                # 交易所拒绝，而不是反向开成裸仓）
                try:
                    trader.cancel_all_algo(symbol)
                    trader.market_order(symbol, close_side, qty, reduce_only=True)
                except Exception as close_err:
                    logger.error("紧急平仓失败 %s: %s（若开仓单未成交则本无仓位）", symbol, close_err)
                raise

            opened += 1
            running += 1
            positions[symbol] = {"amt": qty if direction == "long" else -qty,
                                 "initial_margin": margin_used, "entry_price": price}
            logger.info("已开仓 %s %s qty=%s sl=%s tp1=%s tp2=%s（评分 %s）",
                        symbol, direction, qty, sl, tp1, tp2, a.recommendation)
        except Exception as e:
            db.rollback()
            logger.warning("开仓失败 %s: %s", symbol, e)
            # 交易所无此合约（-1121，如 testnet 未上架）每轮必失败：落 FAILED 记录并跳过同 symbol 后续分析
            if "-1121" in str(e):
                try:
                    exists = db.execute(
                        select(TradeRecord.id).where(
                            TradeRecord.symbol == symbol, TradeRecord.status == "FAILED")
                    ).first()
                    if not exists:
                        rec = TradeRecord(
                            symbol=symbol, direction=direction, ai_analysis_id=a.id,
                            recommendation=float(a.recommendation) if a.recommendation is not None else None,
                            status="FAILED", raw={"fail_reason": str(e)[:200]},
                        )
                        db.add(rec)
                        db.flush()
                        _add_event(db, rec, "ERROR", {"message": "交易所未上架该合约（-1121）"})
                    db.commit()
                except Exception:
                    db.rollback()
            continue
    return opened


# ── 结算 ──────────────────────────────────────────────────────


def _trailing_stop(rec: TradeRecord, klines: list[list]) -> Optional[float]:
    """跟进止损（只收紧不放松）：多单=max(当前SL, 最近3根已收盘K线最低价)，空单反之。

    TP2_HIT 状态下额外以 TP1 价夹紧（保本锚定）：多单 SL 不得低于 TP1，
    空单不得高于 TP1——即使最近 3 根 K 线极值越过 TP1 也不放松到保本价之外。
    """
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < 3:
        return None
    cur = float(rec.stop_loss or 0)
    if cur <= 0:
        return None
    if rec.direction == "long":
        extreme = min(float(k[3]) for k in closed[-3:])
        new_sl = max(cur, extreme)
        if rec.status == "TP2_HIT" and rec.tp1:
            new_sl = max(new_sl, float(rec.tp1))
    else:
        extreme = max(float(k[2]) for k in closed[-3:])
        new_sl = min(cur, extreme)
        if rec.status == "TP2_HIT" and rec.tp1:
            new_sl = min(new_sl, float(rec.tp1))
    return new_sl if new_sl != cur else None


def _move_sl_trailing(db: Session, trader: BinanceTrader, rec: TradeRecord,
                      interval: str, open_ids: set[int]) -> None:
    """按跟进规则移动止损：撤旧 SL 挂新 SL（closePosition），更新现值并写事件"""
    klines = ExchangePool().get_klines(rec.symbol, interval, 20)
    new_sl = _trailing_stop(rec, klines)
    if new_sl is None:
        return
    new_sl = trader.round_price(rec.symbol, new_sl)
    old_sl = float(rec.stop_loss)
    raw = rec.raw or {}
    old_id = raw.get("sl_order_id")
    if old_id and old_id in open_ids:
        trader.cancel_order(rec.symbol, int(old_id))
    sl_order = trader.stop_market_close(rec.symbol, _close_side(rec.direction), new_sl)
    rec.stop_loss = new_sl
    raw["sl_order_id"] = sl_order["orderId"]
    rec.raw = raw
    _add_event(db, rec, "SL_MOVE", {"from": old_sl, "to": new_sl,
                                    "sl_order_id": sl_order["orderId"]})
    logger.info("跟进止损 %s：%s → %s", rec.symbol, old_sl, new_sl)


def _derive_exit_reason(rec: TradeRecord) -> str:
    if rec.status == "TP2_HIT":
        return "trail_sl"
    if rec.status == "TP1_HIT":
        return "tp1_then_sl"
    return "sl"


def settle_trades(db: Session, trader: BinanceTrader) -> int:
    """对非终态交易：以交易所为准推断事件、跟进止损、结算收益。返回处理笔数"""
    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    records = db.execute(
        select(TradeRecord).where(TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")))
    ).scalars().all()
    if not records:
        return 0
    positions = trader.positions()
    handled = 0

    for rec in records:
        try:
            qty = float(rec.qty or 0)
            raw = rec.raw or {}
            pos = positions.get(rec.symbol)
            amt = abs(pos["amt"]) if pos else 0.0
            open_ids = trader.open_order_ids(rec.symbol)
            tp1_gone = raw.get("tp1_order_id") and int(raw["tp1_order_id"]) not in open_ids
            tp2_gone = raw.get("tp2_order_id") and int(raw["tp2_order_id"]) not in open_ids

            # 仓位归零 → 结算（以币安已实现盈亏为准）
            if qty <= 0 or amt < qty * 0.05:
                # 清理残留挂单（SL 先成交时 TP reduceOnly 单会一直挂着）
                trader.cancel_all_algo(rec.symbol)
                since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
                pnl = trader.realized_pnl_since(rec.symbol, since_ms)
                rec.realized_pnl = round(pnl, 6)
                rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
                exit_reason = _derive_exit_reason(rec)  # 须在改状态前取（依当前状态推断）
                rec.status = "CLOSED"
                rec.closed_at = datetime.utcnow()
                rec.exit_reason = exit_reason
                _add_event(db, rec, "SETTLE", {
                    "realized_pnl": rec.realized_pnl, "pnl_pct": rec.pnl_pct,
                    "exit_reason": rec.exit_reason,
                })
                db.commit()
                handled += 1
                logger.info("交易结算 %s %s：pnl=%s（%s%%）", rec.symbol, rec.direction,
                            rec.realized_pnl, rec.pnl_pct)
                continue

            ratio = amt / qty
            if rec.status == "OPENED":
                if tp2_gone and ratio <= 0.35:
                    # 同一小时两档止盈都成交
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1")})
                    rec.status = "TP1_HIT"
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2")})
                    rec.status = "TP2_HIT"
                    _move_sl_trailing(db, trader, rec, interval, open_ids)
                    db.commit()
                    handled += 1
                elif tp1_gone and ratio <= 0.75:
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1")})
                    rec.status = "TP1_HIT"
                    db.commit()
                    handled += 1
            elif rec.status == "TP1_HIT":
                if tp2_gone and ratio <= 0.35:
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2")})
                    rec.status = "TP2_HIT"
                    _move_sl_trailing(db, trader, rec, interval, open_ids)
                    db.commit()
                    handled += 1
            elif rec.status == "TP2_HIT":
                # 每小时跟进止损（只收紧不放松）
                _move_sl_trailing(db, trader, rec, interval, open_ids)
                db.commit()
                handled += 1
        except Exception as e:
            db.rollback()
            logger.warning("结算 %s 失败: %s", rec.symbol, e)
            continue
    return handled
