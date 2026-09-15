"""自动交易引擎（docs/06）

开仓（唯一通道 = 即时开仓，2026-09-14 起不再有 :42 批量开仓）：
    AI 结论 suggest 且推荐度 ≥60 → 分析落库即分发 open_trade_for_analysis
    → try_open_for_analysis 过闸门（在跑单上限 / 余额 70% 规则 / 同币无仓 /
    方向价格复验）→ 固定亏损法定仓位（基数分档 ×3% ÷ 止损距离）
    → 市价开仓 + 挂 SL/TP1/TP2 → 入库；闸门未过即放弃，无批次重试

结算（settle_trades，每小时 :42 巡检）：
    以交易所为真源（持仓量/挂单/已实现盈亏）推断成交事件：
    仓位归零 → 结算收益（正/负值）；TP1/TP2 成交 → 状态迁移；
    止损挂单消失（被撤/交易所异常）→ 按当前止损价补挂（裸仓保护）；
    TP1 成交 → 止损移至成本价（保本，幂等）；
    无 TP2 的单：TP1 止盈 75%，剩余 25% 从 TP1 起跟进止损；
    TP2 后每次巡检跟进止损（最近3根已收盘K线极值，只收紧不放松）
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from uuid import UUID
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import TradeEvent, TradeRecord
from app.services.binance_trader import BinanceTrader, BinanceTradeError
from app.services.exchange_pool import ExchangePool
from app.services.risk_guard import liquidation_gate_pct
from app.services.strategy.ema import analyze_ema

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


def _tp_quantities(qty: float, tp2: float) -> tuple[float, float]:
    """TP1/TP2 分配数量：AI 给出 TP2 → 50%/25% 两档分批止盈；
    未给出（AI 未报 / 前方无结构位程序回退清零）→ TP1 一次止盈 75%，
    剩余 25% 从 TP1 成交起由跟进止损退出（与 TP2 后同机制）"""
    if tp2 and tp2 > 0:
        return qty * 0.5, qty * 0.25
    return qty * 0.75, 0.0


def _ai_snapshot(a: AIAnalysis) -> dict:
    """开单时 AI 分析结论快照：交易记录自持一份，不随 ai_analyses 清理断链。
    字段与前端 AIAnalysisCard 展示所需一致（scan_result_id 置空 = 不提供运行轨迹入口）"""
    def _num(v) -> Optional[float]:
        return float(v) if v is not None else None
    return {
        "id": str(a.id),
        "symbol": a.symbol,
        "trade_decision": a.trade_decision,
        "skip_reason": a.skip_reason,
        "direction": a.direction,
        "trade_type": a.trade_type,
        "analysis": a.analysis,
        "entry_price": _num(a.entry_price),
        "stop_loss": _num(a.stop_loss),
        "take_profit_1": _num(a.take_profit_1),
        "take_profit_2": _num(a.take_profit_2),
        "risk_reward_ratio": _num(a.risk_reward_ratio),
        "position_pct": _num(a.position_pct),
        "recommendation": _num(a.recommendation),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "scan_result_id": None,
    }


# ── 开仓 ──────────────────────────────────────────────────────


def _attempt_open(
    db: Session, trader: BinanceTrader, cfg: SystemConfig,
    interval: str, kline_window: int, a: AIAnalysis,
    wallet: float, positions: dict,
) -> bool:
    """对单条分析过全部开仓闸门并尝试开仓（docs/06 §3 逐项检查）。

    返回是否成功开仓；成功时更新 positions（供调用方累计在跑数与占用保证金）。
    即时开仓唯一入口（try_open_for_analysis，分析完成即分发）调用；
    开仓失败统一 rollback 并落 FAILED 记录（仅 -1121 未上架）。
    """
    occupied = sum(p["initial_margin"] for p in positions.values())
    symbol = a.symbol
    if symbol in positions:
        logger.info("开仓跳过 %s：已有持仓", symbol)
        return False
    # 开单策略开关：AI 归类类型未启用则不开仓（分析本身照常记录）
    trade_type = a.trade_type or ""
    type_switch = {
        "trend_follow": cfg.strategy_trend_follow_enabled,
        "structure_break": cfg.strategy_structure_break_enabled,
        "range_edge": cfg.strategy_range_edge_enabled,
    }.get(trade_type)
    if type_switch is None:
        logger.info("开仓跳过 %s：开单类型未知（%s）", symbol, trade_type or "无")
        return False
    if not type_switch:
        logger.info("开仓跳过 %s：策略未启用（%s）", symbol, trade_type)
        return False
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
            return False
        if direction == "short" and not (sl > price > tp1):
            logger.info("开仓作废 %s：现价 %s 已不在止损/止盈一区间内", symbol, price)
            return False
        if abs(price - entry_ai) / entry_ai > 0.01:
            logger.info("开仓作废 %s：现价偏离 AI 入场价超 1%%", symbol)
            return False
        # 盈亏比按开仓时现价复验：分析到开仓之间价格漂移会改变实际赔率
        # （多单现价抬高→风险距离变大、盈利空间变小），跌破铁律即作废——
        # 防"价格已涨/跌了一截仍追单"，此时开进去的实际盈亏比远低于分析值
        rr_open = abs(tp1 - price) / abs(price - sl)
        if rr_open < settings.AI_RR_MIN:
            logger.info(
                "开仓作废 %s：按现价复算盈亏比 %.2f < %.1f（现价 %s，入场 %s）",
                symbol, rr_open, settings.AI_RR_MIN, price, entry_ai,
            )
            return False
        # EMA 排列必须条件（顺势交易专属）：多单须 21>55>144 多头排列，空单须 144>55>21 空头排列
        if trade_type == "trend_follow":
            ema = analyze_ema(ExchangePool().get_klines(symbol, interval, kline_window))
            ema_ok = ema is not None and (
                (direction == "long" and ema["fast"] > ema["mid"] > ema["slow"])
                or (direction == "short" and ema["fast"] < ema["mid"] < ema["slow"])
            )
            if not ema_ok:
                logger.info("开仓跳过 %s：顺势交易 EMA 未按方向排列（%s），不满足开单前提",
                            symbol, ema["state_label"] if ema else "K线数据不足")
                return False
        # 固定亏损仓位：基数分档 × 3% ÷ 止损距离
        stop_pct = abs(price - sl) / price
        # 止损宽度闸门（防强平）：逐仓计划止损亏损 = 名义×stop_pct，保证金 = 名义/杠杆；
        # 止损距离超过 1/杠杆−维持保证金率（20× 即 4.5%）时，价格未到止损价
        # 保证金就先亏光 → 被强平并连累撤销 TP 挂单（BATUSDT 案例：止损 6.4% 被强平）。
        # 这类单子固定亏损法无解（预算放不进保证金里），直接跳过
        # （口径与 risk_guard.liquidation_gate_pct 共用；risk_guard 侧前置拦截已于
        # 2026-09-15 随"仅保留 RR 铁律"移除，本闸门是强平风险的唯一防线）
        liq_gate = liquidation_gate_pct()
        if stop_pct >= liq_gate:
            logger.info(
                "开仓跳过 %s：止损距离 %.2f%% 达到强平闸门 %.2f%%（1/%s 杠杆−维持保证金），"
                "价格未到止损必先被强平",
                symbol, stop_pct * 100, liq_gate * 100, settings.TRADING_LEVERAGE,
            )
            return False
        risk_budget = _capital_base(wallet) * settings.TRADING_RISK_PCT / 100
        notional = risk_budget / stop_pct
        qty = trader.round_qty(symbol, notional / price)
        f = trader.filters(symbol)
        if qty < f["min_qty"] or qty * price < f["min_notional"]:
            logger.info(
                "开仓放弃 %s：计算数量 %s 低于交易所最小规则（min_qty=%s, min_notional=%s）",
                symbol, qty, f["min_qty"], f["min_notional"],
            )
            return False
        if qty > f["max_qty"]:
            logger.info("开仓 %s：数量 %s 超交易所单笔上限 %s，按上限缩减（实际风险低于 %.0f%% 预算）",
                        symbol, qty, f["max_qty"], settings.TRADING_RISK_PCT)
            qty = trader.round_qty(symbol, f["max_qty"])
        leverage = settings.TRADING_LEVERAGE
        margin_used = qty * price / leverage
        if margin_used > wallet - occupied:
            logger.info("开仓放弃 %s：所需保证金 %.2f 超过可用 %.2f", symbol, margin_used, wallet - occupied)
            return False
        # TP1 分批数量预检（下单前）：低于交易所最小数量时整单放弃——
        # 开完仓才挂 TP1 被交易所拒单，会走紧急撤单+平仓，且该失败不落记录，
        # 候选每小时重试"开仓→拒单→平仓"空转（docs/07 §8-A1）。TP2 本就有
        # min_qty 检查（不足时静默跳过该档），TP1 是主止盈档、缺它无意义
        q1_pre, _ = _tp_quantities(qty, tp2)
        qty_tp1_pre = trader.round_qty(symbol, q1_pre)
        if qty_tp1_pre < f["min_qty"]:
            logger.info(
                "开仓放弃 %s：止盈一数量 %s 低于交易所最小数量 %s，分批止盈无法挂单",
                symbol, qty_tp1_pre, f["min_qty"],
            )
            return False
        # 实际风险金额按最终数量精确计（向下取整/上限缩减只会低于 3% 预算）
        risk_amount = round(qty * abs(price - sl), 4)

        # 下单：市价开仓 → 挂 SL/TP1/TP2 → 入库，全在保护块内：
        # 任何一步失败都紧急撤单+平仓，不留裸仓（含记录构建/入库失败）
        trader.setup_leverage(symbol, leverage)
        side = "BUY" if direction == "long" else "SELL"
        close_side = _close_side(direction)
        entry_order = trader.market_order(symbol, side, qty)
        # 成交回报：下单响应为 ACK 不含均价，市价单即时成交后查实际成交价/量。
        # 滑点 = 成交价对参考价的偏离（testnet 盘口薄，深吃单可达 0.5%+）；
        # 查询失败按参考价记账，不阻断挂 SL/TP（保护块兜底仍在）
        fill_price, fill_qty = price, qty
        try:
            od = trader.order(symbol, entry_order["orderId"])
            if float(od.get("avgPrice") or 0) > 0:
                fill_price = float(od["avgPrice"])
                fill_qty = float(od.get("executedQty") or qty)
        except Exception as fill_err:
            logger.warning("开仓 %s：成交回报查询失败，按参考价 %s 记账（%s）", symbol, price, fill_err)
        slippage_pct = (fill_price / price - 1) * 100
        actual_risk = fill_qty * abs(fill_price - sl)
        if risk_amount > 0 and actual_risk > risk_amount * 1.15:
            logger.warning(
                "开仓 %s：成交 %s 对参考价 %s 滑 %.2f%%，实际止损风险 %.2f 超预算 %.2f 的 %.0f%%"
                "（止损越近名义仓位越大，同等滑率放大越多）",
                symbol, fill_price, price, slippage_pct,
                actual_risk, risk_amount, (actual_risk / risk_amount - 1) * 100,
            )
        qty = fill_qty
        try:
            sl_order = trader.stop_market_close(
                symbol, close_side, trader.round_price(symbol, sl))
            q1, q2 = _tp_quantities(qty, tp2)
            qty_tp1 = trader.round_qty(symbol, q1)
            qty_tp2 = trader.round_qty(symbol, q2)
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
                testnet=settings.TRADING_TESTNET,
                ai_snapshot=_ai_snapshot(a),
                entry_price=fill_price,
                qty=qty,
                notional=round(qty * fill_price, 2),
                leverage=leverage,
                margin_mode="isolated",
                margin_used=round(qty * fill_price / leverage, 4),
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
                    # 价差盈亏书签（REALIZED_PNL 累计，仅价差不含费用）：
                    # TP1/TP2 成交事件用"当前累计 − 书签"算本档盈亏
                    "realized_bookmark": 0,
                },
            )
            db.add(rec)
            db.flush()
            _add_event(db, rec, "OPEN", {
                "price": price, "qty": qty, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
                "risk_amount": round(risk_amount, 4), "entry_order_id": entry_order["orderId"],
                "fill_price": fill_price, "slippage_pct": round(slippage_pct, 3),
                "actual_risk": round(actual_risk, 4),
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

        positions[symbol] = {"amt": qty if direction == "long" else -qty,
                             "initial_margin": qty * fill_price / leverage,
                             "entry_price": fill_price}
        logger.info("已开仓 %s %s qty=%s 成交=%s（对参考价 %s 滑 %.2f%%）sl=%s tp1=%s tp2=%s（评分 %s）",
                    symbol, direction, qty, fill_price, price, slippage_pct,
                    sl, tp1, tp2, a.recommendation)
        return True
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
                        testnet=settings.TRADING_TESTNET,
                        status="FAILED", raw={"fail_reason": str(e)[:200]},
                    )
                    db.add(rec)
                    db.flush()
                    _add_event(db, rec, "ERROR", {"message": "交易所未上架该合约（-1121）"})
                db.commit()
            except Exception:
                db.rollback()
        return False


def try_open_for_analysis(db: Session, trader: BinanceTrader, analysis_id) -> bool:
    """单条分析完成即开仓（2026-09-14：不再等 :42 统一批次，docs/06 §2）。

    闸门与批量开仓完全一致（§3）：上限 / 70% 余额 / 最小资金 / 同币无仓 /
    策略开关 / 方向价格复验 / 盈亏比复验 / EMA / 强平闸门。
    返回是否成功开仓；闸门未过即放弃（无批次重试，等该币下次信号重新分析）。
    """
    a = db.get(AIAnalysis, UUID(str(analysis_id)))
    if a is None:
        return False
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        return False
    # 开仓资格快速过滤（suggest / 方向 / 三价 / 评分门槛 / 1 小时内）
    if (
        a.trade_decision != "suggest"
        or a.direction not in ("long", "short")
        or not a.entry_price or not a.stop_loss or not a.take_profit_1
        or float(a.recommendation or 0) < settings.TRADING_MIN_RECOMMENDATION
    ):
        return False
    if a.created_at and a.created_at < datetime.utcnow() - timedelta(hours=1):
        logger.info("即时开仓跳过 %s：分析已超过 1 小时", a.symbol)
        return False
    if db.execute(
        select(TradeRecord.id).where(TradeRecord.ai_analysis_id == a.id)
    ).first():
        return False  # 该分析已开过仓
    positions = trader.positions()
    if len(positions) >= cfg.max_open_trades:
        logger.info("即时开仓跳过 %s：在跑单子已达上限 %d", a.symbol, cfg.max_open_trades)
        return False
    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    occupied = sum(p["initial_margin"] for p in positions.values())
    if wallet > 0 and wallet - occupied < wallet * settings.TRADING_MIN_FREE_PCT:
        logger.info("即时开仓跳过 %s：可用余额低于总资金 %.0f%%",
                    a.symbol, settings.TRADING_MIN_FREE_PCT * 100)
        return False
    if _capital_base(wallet) <= 0:
        logger.info("即时开仓跳过 %s：钱包余额低于最小风险档位", a.symbol)
        return False
    interval = cfg.kline_interval if cfg else "1h"
    # EMA144 需 ≥154 根已收盘 K 线，窗口下限 160
    kline_window = max(cfg.kline_window or 240, 160)
    return _attempt_open(db, trader, cfg, interval, kline_window, a, wallet, positions)


# ── 结算 ──────────────────────────────────────────────────────


def _trailing_stop(rec: TradeRecord, klines: list[list]) -> Optional[float]:
    """跟进止损（只收紧不放松）：多单=max(当前SL, 最近3根已收盘K线最低价)，空单反之。

    适用状态：TP2_HIT（有 TP2 的单）与 TP1_HIT（无 TP2 的单，TP1 成交后即启用）。
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
    """按跟进规则移动止损（只收紧不放松）；新 SL 会立即触发时跳过本轮（旧 SL 仍有效保护：
    仓位存在即旧 SL 未触发），避免交易所 -2021 拒单导致整轮结算回滚重试"""
    klines = ExchangePool().get_klines(rec.symbol, interval, 20)
    new_sl = _trailing_stop(rec, klines)
    if new_sl is None:
        return
    new_sl = trader.round_price(rec.symbol, new_sl)
    cur = float(rec.stop_loss or 0)
    if new_sl == cur:
        return
    price = trader.price(rec.symbol)
    if _immediate_trigger(new_sl, price, rec.direction):
        logger.info("跟进止损 %s 跳过：新 SL %s 会立即触发（现价 %s）", rec.symbol, new_sl, price)
        return
    _replace_sl(db, trader, rec, open_ids, new_sl)
    logger.info("跟进止损 %s：%s → %s", rec.symbol, cur, new_sl)


def _replace_sl(db: Session, trader: BinanceTrader, rec: TradeRecord,
                open_ids: set[int], new_sl: float, reason: Optional[str] = None) -> None:
    """撤旧 SL 挂新 SL（closePosition），更新现值并写 SL_MOVE 事件。

    -4130（同方向已有 closePosition 条件单）兜底：旧单撤单竞态未生效时，
    清掉该方向残留的 closePosition 单再挂一次（TP 单是 reduceOnly 带数量，不受影响）。
    """
    raw = rec.raw or {}
    old_id = raw.get("sl_order_id")
    if old_id and old_id in open_ids:
        trader.cancel_order(rec.symbol, int(old_id))
    close_side = _close_side(rec.direction)
    try:
        sl_order = trader.stop_market_close(rec.symbol, close_side, new_sl)
    except BinanceTradeError as e:
        if "-4130" not in str(e):
            raise
        logger.warning("SL 挂单遇 -4130 %s：清理残留 closePosition 单后重挂", rec.symbol)
        trader.cancel_stale_close_position_algo(rec.symbol, close_side)
        sl_order = trader.stop_market_close(rec.symbol, close_side, new_sl)
    old_sl = float(rec.stop_loss or 0)
    rec.stop_loss = new_sl
    # 必须赋新 dict：rec.raw = 原对象 会被 SQLAlchemy 判定无变化而丢库
    # （实测历史单 raw.sl_order_id 全部停留在开仓时旧值，docs/07 §8-A2）
    new_id = sl_order.get("algoId") or sl_order.get("orderId")
    rec.raw = {**raw, "sl_order_id": new_id}
    detail = {"from": old_sl, "to": new_sl, "sl_order_id": new_id}
    if reason:
        detail["reason"] = reason
    _add_event(db, rec, "SL_MOVE", detail)


def _immediate_trigger(new_sl: float, price: float, direction: str) -> bool:
    """新 SL 挂上即会触发：多单 SL ≥ 现价 / 空单 SL ≤ 现价"""
    return new_sl >= price if direction == "long" else new_sl <= price


def _rehang_lost_sl(db: Session, trader: BinanceTrader, rec: TradeRecord,
                    open_ids: set[int]) -> None:
    """止损挂单存活巡检（docs/07 §8-A2）：SL algo 单消失（手动撤销/交易所异常）时
    按当前止损价补挂——OPENED/TP1_HIT/TP2_HIT 状态下仓位无 SL 保护只能等强平。

    - SL 单仍在挂单列表（或记录无 sl_order_id 的极端情况无价可挂）→ 不动作；
    - 现价已被止损价越过：疑似 SL 刚触发、持仓量尚未反映，补挂会立即触发/挂空单，
      跳过本轮，留待仓位归零的结算路径确认。
    """
    raw = rec.raw or {}
    sl_id = raw.get("sl_order_id")
    if sl_id and int(sl_id) in open_ids:
        return
    cur_sl = float(rec.stop_loss or 0)
    if cur_sl <= 0:
        return
    price = trader.price(rec.symbol)
    if _immediate_trigger(cur_sl, price, rec.direction):
        logger.info(
            "止损单丢失 %s：现价 %s 已越过 SL %s，疑似刚触发，下轮结算确认",
            rec.symbol, price, cur_sl,
        )
        return
    _replace_sl(db, trader, rec, open_ids,
                trader.round_price(rec.symbol, cur_sl), reason="sl_rehang")
    db.commit()
    logger.warning("止损挂单丢失已补挂 %s：SL=%s（巡检发现挂单消失）", rec.symbol, cur_sl)


def _move_sl_breakeven(db: Session, trader: BinanceTrader, rec: TradeRecord,
                       open_ids: set[int]) -> bool:
    """TP1 成交后保本止损：SL 移到成本价（多单 SL=max(现SL,入场价)，空单反之，只收紧不放松）。

    幂等：SL 已不差于成本价（或取整后相同）时不动作；价格已回到成本价另一侧
    （挂上即立即触发）时跳过本轮保留原 SL，下轮巡检再试。返回是否实际移动。
    """
    entry = float(rec.entry_price or 0)
    cur = float(rec.stop_loss or 0)
    if entry <= 0 or cur <= 0:
        return False
    new_sl = max(cur, entry) if rec.direction == "long" else min(cur, entry)
    if new_sl == cur:
        return False
    new_sl = trader.round_price(rec.symbol, new_sl)
    if new_sl == cur:
        return False
    price = trader.price(rec.symbol)
    if _immediate_trigger(new_sl, price, rec.direction):
        logger.info("保本止损 %s 跳过：SL %s 会立即触发（现价 %s），保留原 SL 下轮再试",
                    rec.symbol, new_sl, price)
        return False
    _replace_sl(db, trader, rec, open_ids, new_sl, reason="tp1_breakeven")
    logger.info("保本止损 %s：%s → %s（成本价）", rec.symbol, cur, new_sl)
    return True


def _derive_exit_reason(rec: TradeRecord) -> str:
    if rec.status == "TP2_HIT":
        return "trail_sl"
    if rec.status == "TP1_HIT":
        # TP1 后止损已移至成本价；无 TP2 的单随后启用跟进止损（只收紧）——
        # SL 已被收紧越过成本价 → TP1后跟踪止损出场；仍贴着成本价 → 保本出场
        entry, sl = float(rec.entry_price or 0), float(rec.stop_loss or 0)
        if entry > 0 and sl > 0:
            eps = entry * 1e-6
            if rec.direction == "long":
                if sl > entry + eps:
                    return "tp1_trail"
                if sl >= entry - eps:
                    return "breakeven_sl"
            else:
                if sl < entry - eps:
                    return "tp1_trail"
                if sl <= entry + eps:
                    return "breakeven_sl"
        return "tp1_then_sl"
    return "sl"


def _zero_pos_exit_reason(rec: TradeRecord, tp1_gone: bool) -> str:
    """仓位归零时的出场原因：OPENED 状态下 TP1 单已消失，说明 TP1 与 SL 在同一巡检间隔内
    先后成交（如 75% TP1 后回踩触发 SL）→ tp1_then_sl；其余按状态推断"""
    if rec.status == "OPENED" and tp1_gone:
        return "tp1_then_sl"
    return _derive_exit_reason(rec)


def _record_realized(db: Session, trader: BinanceTrader, rec: TradeRecord) -> float:
    """部分止盈后记录截至当前的已实现净盈亏（已实现盈亏−手续费−资金费−强平清算，交易所 income 口径）。

    写入交易记录（运行中=已实现部分，供收益列展示已止盈金额；终态结算时会复算覆盖为
    全程净额）并返回，供 TP1_FILL/TP2_FILL 事件 detail 携带累计值。
    """
    # 起点回拨 5s：开仓手续费 income 的时间戳可能略早于 opened_at（实测时差 <1s），
    # 不回拨会把入场手续费漏在净盈亏之外
    since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
    pnl = round(trader.realized_pnl_since(rec.symbol, since_ms), 6)
    rec.realized_pnl = pnl
    rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
    return pnl


def _tranche_pnl(trader: BinanceTrader, rec: TradeRecord, raw: dict) -> float:
    """本档止盈盈亏：REALIZED_PNL 累计（仅价差不含费用）相对上次书签的差值，
    即该档成交自身的价差收益；书签推进后写回 rec.raw。"""
    since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
    cum = trader.realized_price_pnl_since(rec.symbol, since_ms)
    # 同轮巡检内 _rehang_lost_sl 等可能已更新 rec.raw，重读最新值，避免用本轮入口的旧快照覆盖回去
    cur_raw = rec.raw or raw
    tranche = round(cum - float(cur_raw.get("realized_bookmark") or 0), 6)
    rec.raw = {**cur_raw, "realized_bookmark": round(cum, 6)}
    return tranche


def _sweep_orphan_algo_orders(
    trader: BinanceTrader, active_syms: set, positions: dict,
) -> int:
    """孤儿条件单清扫：交易所在挂 algo 委托，但既无持仓也无在跑交易记录 → 撤销。

    巡检按 DB 记录驱动，清不到"无记录的遗留单"（DB 重置前的历史单、手动平仓后
    的残单等）——已止损/已平仓币种的条件委托会永远悬挂在交易所。每轮结算兜底
    一遍，只动本系统交易过的符号之外一律不碰（不做全账户无差别撤销）。
    返回清理的 symbol 数。
    """
    try:
        open_algo = trader.all_open_algo_orders()
    except Exception as e:
        logger.warning("孤儿清扫：拉取在挂条件单失败（跳过本轮）: %s", e)
        return 0
    if not open_algo:
        return 0
    held = {
        sym for sym, p in (positions or {}).items()
        if abs(float((p or {}).get("amt") or 0)) > 0
    }
    orphan_syms = {str(o.get("symbol")) for o in open_algo if o.get("symbol")} - active_syms - held
    cleaned = 0
    for sym in sorted(orphan_syms):
        try:
            trader.cancel_all_algo(sym)
            cleaned += 1
            logger.info("孤儿条件单清扫：撤销 %s 的在挂委托（无持仓、无在跑记录）", sym)
        except Exception as e:
            logger.warning("孤儿清扫撤销 %s 失败: %s", sym, e)
    return cleaned


def settle_trades(db: Session, trader: BinanceTrader) -> int:
    """对非终态交易：以交易所为准推断事件、跟进止损、结算收益。返回处理笔数"""
    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    records = db.execute(
        select(TradeRecord).where(TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")))
    ).scalars().all()
    # 在跑符号集（仅本网）：孤儿清扫时豁免这些 symbol 的在挂委托
    active_syms = {
        r.symbol for r in records if r.testnet == settings.TRADING_TESTNET
    }
    positions = trader.positions()
    if not records:
        _sweep_orphan_algo_orders(trader, active_syms, positions)
        return 0
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

            # 仓位归零 → 结算（净盈亏 = 已实现盈亏 − 手续费 − 资金费 − 强平清算，正/负值）
            if qty <= 0 or amt < qty * 0.05:
                # 清理残留挂单（SL 先成交时 TP reduceOnly 单会一直挂着）
                trader.cancel_all_algo(rec.symbol)
                # 起点回拨 5s：开仓手续费 income 的时间戳可能略早于 opened_at（同 _record_realized）
                since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
                rows = trader.income_rows(rec.symbol, since_ms)
                # 强平识别：逐仓保证金亏光时交易所强制平仓并撤销全部挂单（含 TP），
                # 会产生 INSURANCE_CLEAR 清算记录——此时 TP1 挂单消失≠TP1 成交，
                # 不能按 tp1_gone 误判为 tp1_then_sl
                insurance_clear = sum(
                    float(r.get("income") or 0)
                    for r in rows if r.get("incomeType") == "INSURANCE_CLEAR"
                )
                pnl = round(trader.sum_income(rows, trader.INCOME_KEEP), 6)
                rec.realized_pnl = pnl
                rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
                if insurance_clear != 0:
                    _add_event(db, rec, "LIQUIDATION", {
                        "qty": qty, "insurance_clear": round(insurance_clear, 6),
                        "realized_pnl": pnl,
                    })
                    exit_reason = "liquidation"
                else:
                    if rec.status == "OPENED" and tp1_gone:
                        # TP1 与 SL 在同一巡检间隔内先后成交（如 75% TP1 后回踩触发 SL）：补记 TP1_FILL
                        _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1"),
                                                         "realized_pnl": round(pnl, 6)})
                    exit_reason = _zero_pos_exit_reason(rec, tp1_gone)
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
            # 止损挂单存活巡检：SL 单被撤/丢失时补挂（裸仓保护，docs/07 §8-A2）
            _rehang_lost_sl(db, trader, rec, open_ids)
            if rec.status == "OPENED":
                if tp2_gone and ratio <= 0.35:
                    # 同一小时两档止盈都成交
                    realized = _record_realized(db, trader, rec)
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1")})
                    rec.status = "TP1_HIT"
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2"),
                                                     "realized_pnl": realized})  # 两档累计
                    rec.status = "TP2_HIT"
                    _move_sl_trailing(db, trader, rec, interval, open_ids)
                    db.commit()
                    handled += 1
                elif tp1_gone and ratio <= 0.75:
                    realized = _record_realized(db, trader, rec)
                    tranche = _tranche_pnl(trader, rec, raw)
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP1_HIT"
                    _move_sl_breakeven(db, trader, rec, open_ids)  # 用户规则：TP1 后止损移至成本价
                    if not rec.tp2:
                        # 无 TP2：TP1 止盈 75% 后剩余仓即启用跟进止损（与 TP2 后同机制）
                        _move_sl_trailing(db, trader, rec, interval, open_ids)
                    db.commit()
                    handled += 1
            elif rec.status == "TP1_HIT":
                if tp2_gone and ratio <= 0.35:
                    realized = _record_realized(db, trader, rec)
                    tranche = _tranche_pnl(trader, rec, raw)
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP2_HIT"
                    _move_sl_trailing(db, trader, rec, interval, open_ids)
                    db.commit()
                    handled += 1
                else:
                    if rec.realized_pnl is None:
                        # 回填：部署前已升到 TP1_HIT 的旧单，TP1_FILL 事件未记已止盈金额
                        _record_realized(db, trader, rec)
                    # 保本兜底（幂等）：部署前已处于 TP1_HIT 的旧单、或上次保本移动失败的补偿
                    _move_sl_breakeven(db, trader, rec, open_ids)
                    if not rec.tp2:
                        # 无 TP2：与 TP2 后同规则，每次巡检跟进止损（只收紧不放松）
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
    # ③ 孤儿条件单清扫（每轮兜底：无持仓、无在跑记录的在挂委托一律撤销）
    _sweep_orphan_algo_orders(trader, active_syms, positions)
    return handled
