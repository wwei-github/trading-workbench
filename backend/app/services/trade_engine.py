"""自动交易引擎（docs/06、docs/10）

开仓（唯一通道 = 即时开仓，2026-09-14 起不再有 :42 批量开仓）：
    AI 结论 suggest 且推荐度 ≥60 → 分析落库即分发 open_trade_for_analysis
    → try_open_for_analysis 过闸门（在跑单上限含 PENDING / 余额 70% 规则 /
    同币无仓无挂单 / 策略开关）→ 按 AI order_type 分流（docs/10，返回即指令）：
      market → 固定亏损法定仓位（基数分档 ×3% ÷ 止损距离）
               → 方向价格复验 + 现价 RR 复验 → 市价开仓 + 挂 SL/TP1/TP2 → OPENED
      limit  → OTOCO 一体挂单（working 限价入场 + pending TP1 + pending SL，
               成交瞬间交易所自动激活 TP1/SL）→ PENDING 记录，12h 过期撤销

结算（settle_trades，每小时 :10 巡检）：
    ① PENDING 限价单巡检（docs/10 §3）：过期/前提失效撤销、部分成交撤平、
       满额成交转 OPENED（捕获 OTOCO 激活的 TP1/SL 子单 algoId）；
    ② 持仓结算：以交易所为真源（持仓量/挂单/已实现盈亏）推断成交事件：
       仓位归零 → 结算收益（正/负值）；TP1/TP2 成交 → 状态迁移；
       止损挂单消失（被撤/交易所异常）→ 按当前止损价补挂（裸仓保护；
       限价单 TP1 成交时 OCO 自动撤 SL 属预期，由保本移动补挂）；
       TP1 成交 → 止损移至成本价（保本，幂等；限价单随后补挂 TP2）；
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
        "order_type": a.order_type or "market",
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
        # 委托方式路由（docs/10 §1，AI 返回即指令）：limit → 直接挂 OTOCO 等回踩，
        # 不做方向/偏离/现价 RR 校验——三道天然保障：限价成交价 ≤ 委托价（价格保护）、
        # 交易所 -2021 几何拒收、TTL+前提失效兜底。总开关关闭时回落市价路径（现行闸门全保留）
        if (a.order_type or "market") == "limit" and cfg.limit_order_enabled:
            return _place_limit_otoco(
                db, trader, a, direction, entry_ai, sl, tp1, tp2,
                interval, kline_window, wallet, positions,
            )
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
        # （口径与 risk_guard.liquidation_gate_pct 共用；risk_guard 已按 0.9 倍前置拦截）
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


def _place_limit_otoco(
    db: Session, trader: BinanceTrader, a: AIAnalysis,
    direction: str, entry: float, sl: float, tp1: float, tp2: float,
    interval: str, kline_window: int, wallet: float, positions: dict,
) -> bool:
    """限价委托路径（docs/10 §2）：OTOCO 一体挂出——working 限价入场 + pending TP1 +
    pending SL，入场满额成交瞬间交易所自动激活 TP1/SL（OCO，无裸仓窗口）。

    落 PENDING 记录（entry_price=委托价、expires_at=now+TTL），成交转 OPENED 由
    每小时 :10 巡检的 PENDING 块完成。AI 返回即指令：不校验方向/偏离/现价 RR，
    通用闸门（策略开关/EMA/强平/精度/min_qty/保证金）照常。
    挂单失败异常向上抛（-1121 由 _attempt_open 落 FAILED，其余回滚记日志）。
    """
    symbol = a.symbol
    trade_type = a.trade_type or ""
    # EMA 排列必须条件（顺势交易专属）——限价回踩入场同样适用
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
    # 强平闸门：止损距离以委托价计（固定亏损法按委托价成交口径）
    stop_pct = abs(entry - sl) / entry
    liq_gate = liquidation_gate_pct()
    if stop_pct >= liq_gate:
        logger.info(
            "开仓跳过 %s：止损距离 %.2f%% 达到强平闸门 %.2f%%（1/%s 杠杆−维持保证金）",
            symbol, stop_pct * 100, liq_gate * 100, settings.TRADING_LEVERAGE,
        )
        return False
    # 固定亏损仓位：基数分档 × 3% ÷ 止损距离，数量按委托价折算
    risk_budget = _capital_base(wallet) * settings.TRADING_RISK_PCT / 100
    notional = risk_budget / stop_pct
    qty = trader.round_qty(symbol, notional / entry)
    f = trader.filters(symbol)
    if qty < f["min_qty"] or qty * entry < f["min_notional"]:
        logger.info(
            "开仓放弃 %s：计算数量 %s 低于交易所最小规则（min_qty=%s, min_notional=%s）",
            symbol, qty, f["min_qty"], f["min_notional"],
        )
        return False
    if qty > f["max_qty"]:
        qty = trader.round_qty(symbol, f["max_qty"])
    occupied = sum(p["initial_margin"] for p in positions.values())
    leverage = settings.TRADING_LEVERAGE
    margin_used = qty * entry / leverage
    if margin_used > wallet - occupied:
        logger.info("开仓放弃 %s：所需保证金 %.2f 超过可用 %.2f",
                    symbol, margin_used, wallet - occupied)
        return False
    # TP1 分批数量预检（docs/07 §8-A1，下单前）：低于 min_qty 整单放弃
    q1, q2 = _tp_quantities(qty, tp2)
    qty_tp1 = trader.round_qty(symbol, q1)
    qty_tp2 = trader.round_qty(symbol, q2)
    if qty_tp1 < f["min_qty"]:
        logger.info("开仓放弃 %s：止盈一数量 %s 低于交易所最小数量 %s，分批止盈无法挂单",
                    symbol, qty_tp1, f["min_qty"])
        return False
    entry_r = trader.round_price(symbol, entry)
    sl_r = trader.round_price(symbol, sl)
    tp1_r = trader.round_price(symbol, tp1)
    side = "BUY" if direction == "long" else "SELL"
    trader.setup_leverage(symbol, leverage)
    # OTOCO 一体挂出：SL/TP1 触发价在现价错误一侧（会立即触发）时交易所 -2021 拒收
    parent = trader.limit_order_otoco(
        symbol, side, qty, entry_r, tp1_r, qty_tp1, sl_r)
    parent_id = parent.get("algoId") or parent.get("orderId")
    expires = datetime.utcnow() + timedelta(hours=settings.LIMIT_ORDER_TTL_HOURS)
    risk_amount = round(qty * abs(entry_r - sl), 4)
    try:
        rec = TradeRecord(
            symbol=symbol,
            direction=direction,
            scan_result_id=a.scan_result_id,
            ai_analysis_id=a.id,
            recommendation=float(a.recommendation) if a.recommendation is not None else None,
            testnet=settings.TRADING_TESTNET,
            ai_snapshot=_ai_snapshot(a),
            entry_price=entry_r,  # 委托价（成交均价在转 OPENED 时以交易所为准回填）
            qty=qty,
            notional=round(qty * entry_r, 2),
            leverage=leverage,
            margin_mode="isolated",
            margin_used=round(qty * entry_r / leverage, 4),
            risk_amount=risk_amount,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            status="PENDING",
            order_type="limit",
            expires_at=expires,
            opened_at=None,
            raw={
                # OTOCO 父单 algoId（复用入场委托 id 字段）；TP1/SL 子单 algoId 在
                # 成交转 OPENED 时由巡检捕获回填（_capture_otoco_children）
                "entry_order_id": parent_id,
                "qty_tp1": qty_tp1,
                "qty_tp2": qty_tp2,
                "capital_base": _capital_base(wallet),
                "wallet": wallet,
                "realized_bookmark": 0,
            },
        )
        db.add(rec)
        db.flush()
        _add_event(db, rec, "LIMIT_PLACED", {
            "price": entry_r, "qty": qty, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
            "risk_amount": risk_amount, "entry_order_id": parent_id,
            "expires_at": expires.isoformat(),
            "message": "限价委托已挂出（OTOCO：入场+TP1+SL 一体，12h 未成交自动撤销）",
        })
        db.commit()
    except Exception:
        # 落库失败：尚无仓位可平，撤掉 OTOCO 父单防孤儿挂单
        try:
            if parent_id:
                trader.cancel_order(symbol, int(parent_id))
        except Exception as cancel_err:
            logger.error("PENDING 落库失败后撤父单失败 %s: %s", symbol, cancel_err)
        raise
    logger.info(
        "限价委托已挂出 %s %s qty=%s @%s（sl=%s tp1=%s tp2=%s，%dh 未成交过期）",
        symbol, direction, qty, entry_r, sl, tp1, tp2, settings.LIMIT_ORDER_TTL_HOURS,
    )
    return True


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
    # PENDING 限价挂单同样占名额与同币唯一（docs/10 §5：委托锁定保证金）
    pend = db.execute(
        select(TradeRecord.symbol, TradeRecord.margin_used).where(
            TradeRecord.status == "PENDING",
            TradeRecord.testnet == settings.TRADING_TESTNET,
        )
    ).all()
    pending_syms = {r[0] for r in pend}
    pending_margin = sum(float(r[1] or 0) for r in pend)
    if a.symbol in pending_syms:
        logger.info("即时开仓跳过 %s：该币已有限价挂单在等待（一个币一个计划）", a.symbol)
        return False
    in_run = len(positions) + len(pending_syms)
    if in_run >= cfg.max_open_trades:
        logger.info("即时开仓跳过 %s：在跑单子+挂单已达上限 %d", a.symbol, cfg.max_open_trades)
        return False
    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    occupied = sum(p["initial_margin"] for p in positions.values()) + pending_margin
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
    - 限价单（docs/10）：OPENED 状态下 TP1 挂单消失 = OCO 在 TP1 成交时自动撤 SL，
      属预期行为——保本 SL 由本轮 TP1_HIT 分支补挂，此处不补（否则把原始止损价挂回去）；
      TP1_HIT 及之后状态正常巡检。
    """
    raw = rec.raw or {}
    if rec.order_type == "limit" and rec.status == "OPENED":
        tp1_id = raw.get("tp1_order_id")
        if tp1_id and int(tp1_id) not in open_ids:
            return
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
    即该档成交自身的价差收益；书签推进后写回 rec.raw。

    以 rec.raw 当前值为基准（而非调用方的 raw 快照）：同轮 _rehang_lost_sl /
    子单捕获可能已更新过 raw，用旧快照回写会把那些字段退回旧值。"""
    since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
    cum = trader.realized_price_pnl_since(rec.symbol, since_ms)
    cur_raw = rec.raw or raw
    tranche = round(cum - float(cur_raw.get("realized_bookmark") or 0), 6)
    rec.raw = {**cur_raw, "realized_bookmark": round(cum, 6)}
    return tranche


# ── 限价挂单巡检（docs/10 §3，并入每小时 :10 结算任务） ──────────


def _capture_otoco_children(trader: BinanceTrader, rec: TradeRecord) -> None:
    """成交后捕获 OTOCO 自动激活的 TP1/SL 子单 algoId（幂等，逐轮补齐）。

    按 side=平仓方向 + 类型匹配（TAKE_PROFIT→TP1、STOP→SL）；捕获失败不阻塞——
    raw 缺 tp1_order_id 时 tp1_gone 恒为 False（不会误判 TP1 成交），缺 sl_order_id
    时 _rehang_lost_sl 会按当前止损价补挂（-4130 清理兜底），下一轮继续尝试捕获。
    """
    raw = rec.raw or {}
    if raw.get("tp1_order_id") and raw.get("sl_order_id"):
        return
    close_side = _close_side(rec.direction)
    upd: dict = {}
    try:
        for o in trader.open_algo_orders(rec.symbol):
            oid = o.get("algoId")
            if oid is None or str(o.get("side")) != close_side:
                continue
            otype = str(o.get("type") or o.get("algoType") or "").upper()
            if "TAKE_PROFIT" in otype and not raw.get("tp1_order_id"):
                upd["tp1_order_id"] = int(oid)
            elif "STOP" in otype and not raw.get("sl_order_id"):
                upd["sl_order_id"] = int(oid)
    except Exception as e:
        logger.warning("OTOCO 子单捕获失败 %s: %s", rec.symbol, e)
        return
    if upd:
        # 必须赋新 dict（SQLAlchemy 变更检测，docs/07 §8-A2）
        rec.raw = {**raw, **upd}
        logger.info("OTOCO 子单捕获 %s：%s", rec.symbol, upd)


def _promote_pending(db: Session, trader: BinanceTrader, rec: TradeRecord,
                     pos: Optional[dict], st: Optional[dict]) -> None:
    """限价单满额成交 → OPENED（docs/10 §4）：成交价/量以交易所持仓为真相
    （父单成交回报兜底，插针平掉后持仓消失时用），回填 raw 子单 id，之后
    既有结算逻辑（tp1_gone 推断/SL 补挂/保本移动）直接接管。"""
    symbol = rec.symbol
    raw = rec.raw or {}
    placed = float(rec.entry_price or 0)
    qty_planned = float(rec.qty or 0)
    fill_price = fill_qty = 0.0
    if pos:
        fill_price = float(pos.get("entry_price") or 0)
        fill_qty = abs(float(pos.get("amt") or 0))
    if st:
        if fill_price <= 0:
            try:
                fill_price = float(st.get("avgPrice") or 0)
            except (TypeError, ValueError):
                pass
        if fill_qty <= 0:
            try:
                fill_qty = float(st.get("executedQty") or 0)
            except (TypeError, ValueError):
                fill_qty = 0.0
    if fill_price <= 0:
        fill_price = placed
    if fill_qty <= 0:
        fill_qty = qty_planned
    close_side = _close_side(rec.direction)
    now = datetime.utcnow()

    # 插针兜底（docs/10 §4-5）：成交价已越过 SL 而仓位仍存活（SL 激活异常未触发）
    # → 立即市价平仓结算，不留无保护仓位
    sl = float(rec.stop_loss or 0)
    breached = False
    if sl > 0 and fill_price > 0:
        price_now = trader.price(symbol)
        breached = (rec.direction == "long" and price_now <= sl) or \
                   (rec.direction == "short" and price_now >= sl)
    if breached and pos:
        try:
            trader.market_order(symbol, close_side, fill_qty, reduce_only=True)
            trader.cancel_all_algo(symbol)
        except Exception as e:
            logger.error("插针兜底平仓失败 %s: %s", symbol, e)
        pnl = _pnl_since_created(trader, rec)
        rec.entry_price = fill_price
        rec.qty = fill_qty
        rec.notional = round(fill_qty * fill_price, 2)
        rec.margin_used = round(fill_qty * fill_price / (rec.leverage or 20), 4)
        rec.realized_pnl = pnl
        rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
        rec.status = "CLOSED"
        rec.closed_at = now
        rec.exit_reason = "sl"
        _add_event(db, rec, "LIMIT_FILLED", {
            "fill_price": fill_price, "qty": fill_qty,
            "entry_order_id": raw.get("entry_order_id"),
        })
        _add_event(db, rec, "SETTLE", {
            "realized_pnl": pnl, "pnl_pct": rec.pnl_pct, "exit_reason": "sl",
            "message": "成交价已越过止损价（SL 激活异常），插针兜底平仓",
        })
        db.commit()
        logger.warning("限价单插针兜底 %s：成交 %s 已越 SL %s，平仓结算", symbol, fill_price, sl)
        return

    rec.entry_price = fill_price
    rec.qty = fill_qty
    rec.notional = round(fill_qty * fill_price, 2)
    rec.margin_used = round(fill_qty * fill_price / (rec.leverage or 20), 4)
    rec.risk_amount = round(fill_qty * abs(fill_price - sl), 4)
    rec.opened_at = now
    rec.status = "OPENED"
    _add_event(db, rec, "LIMIT_FILLED", {
        "fill_price": fill_price, "qty": fill_qty,
        "entry_order_id": raw.get("entry_order_id"),
        "slippage_pct": round((fill_price / placed - 1) * 100, 3) if placed > 0 else None,
        "message": "限价委托成交，TP1/SL 已由交易所自动激活",
    })
    _capture_otoco_children(trader, rec)
    db.commit()
    logger.info("限价单成交转 OPENED %s：成交 %s ×%s（委托价 %s）",
                symbol, fill_price, fill_qty, placed)


def _pnl_since_created(trader: BinanceTrader, rec: TradeRecord) -> Optional[float]:
    """自记录创建时起（PENDING 期间入场成交已发生）的已实现净盈亏；起点回拨 5s 同 _record_realized"""
    try:
        created = rec.created_at or datetime.utcnow()
        since_ms = int(created.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
        return round(trader.realized_pnl_since(rec.symbol, since_ms), 6)
    except Exception:
        return None


def _close_partial_pending(db: Session, trader: BinanceTrader, rec: TradeRecord,
                           exec_qty: float) -> None:
    """部分成交处置（docs/10 §0-⑦）：撤父单 + 市价平已成交部分 → CLOSED(limit_partial)。
    OTOCO 满额成交才激活 TP/SL，部分仓位无保护，风险必须有界（发现延迟 ≤1h）。"""
    symbol = rec.symbol
    parent_id = (rec.raw or {}).get("entry_order_id")
    if parent_id:
        try:
            trader.cancel_order(symbol, int(parent_id))
        except Exception as e:
            logger.warning("部分成交撤父单失败 %s: %s", symbol, e)
    try:
        trader.market_order(symbol, _close_side(rec.direction), exec_qty, reduce_only=True)
    except Exception as e:
        logger.error("部分成交平仓失败 %s qty=%s: %s", symbol, exec_qty, e)
    pnl = _pnl_since_created(trader, rec)
    rec.qty = exec_qty  # 已成交部分为实际仓位口径
    rec.realized_pnl = pnl
    rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if pnl is not None and rec.risk_amount else None
    rec.status = "CLOSED"
    rec.closed_at = datetime.utcnow()
    rec.exit_reason = "limit_partial"
    _add_event(db, rec, "LIMIT_PARTIAL", {
        "qty": exec_qty, "realized_pnl": pnl,
        "message": "限价单部分成交：撤父单并市价平已成交部分（TP/SL 未激活，风险有界）",
    })
    _add_event(db, rec, "SETTLE", {
        "realized_pnl": pnl, "pnl_pct": rec.pnl_pct, "exit_reason": "limit_partial",
    })
    db.commit()
    logger.warning("限价单部分成交已处置 %s：exec=%s → CLOSED(limit_partial)", symbol, exec_qty)


def _reconcile_cancelled(db: Session, rec: TradeRecord, status_str: str) -> bool:
    """父单已在交易所侧终态（人工撤销/过期/拒绝/消失）：对账落 CANCELLED"""
    exit_reason = {
        "EXPIRED": "limit_expired",
        "REJECTED": "error",
    }.get(status_str, "manual")
    rec.status = "CANCELLED"
    rec.closed_at = datetime.utcnow()
    rec.exit_reason = exit_reason
    _add_event(db, rec, "LIMIT_CANCELLED", {
        "status": status_str, "reason": exit_reason,
        "message": "挂单已在交易所侧撤销/失效，对账落库",
    })
    db.commit()
    logger.info("限价单对账撤销 %s：交易所状态 %s", rec.symbol, status_str)
    return True


def _cancel_pending(db: Session, trader: BinanceTrader, rec: TradeRecord,
                    exit_reason: str, msg: str) -> bool:
    """撤销 PENDING OTOCO 父单并落 CANCELLED；撤单-成交竞态按成交处理
    （宁可开成不可开漏，docs/10 §3）。"""
    symbol = rec.symbol
    qty = float(rec.qty or 0)
    parent_id = (rec.raw or {}).get("entry_order_id")
    # 撤单请求前最后一刻成交的竞态：仓位为准
    pos = trader.positions().get(symbol)
    amt = abs(pos["amt"]) if pos else 0.0
    if qty > 0 and amt >= qty * 0.95:
        _promote_pending(db, trader, rec, pos, None)
        return True
    if parent_id:
        # cancel_order 对已成交/已撤销（-2011）静默忽略——下方复查兜底
        trader.cancel_order(symbol, int(parent_id))
    # 撤后复查：竞态期间成交 → 按成交/部分成交处理
    pos = trader.positions().get(symbol)
    amt = abs(pos["amt"]) if pos else 0.0
    if qty > 0 and amt >= qty * 0.95:
        _promote_pending(db, trader, rec, pos, None)
        return True
    if 0 < amt < qty * 0.95:
        _close_partial_pending(db, trader, rec, amt)
        return True
    rec.status = "CANCELLED"
    rec.closed_at = datetime.utcnow()
    rec.exit_reason = exit_reason
    _add_event(db, rec, "LIMIT_CANCELLED", {
        "reason": exit_reason, "message": msg, "entry_order_id": parent_id,
    })
    db.commit()
    logger.info("限价单撤销 %s：%s（%s）", symbol, exit_reason, msg)
    return True


def _handle_pending(db: Session, trader: BinanceTrader, rec: TradeRecord) -> bool:
    """单条 PENDING 限价单巡检（docs/10 §3）：先查单再决策，交易所为真相。

    返回是否处置（转 OPENED / 撤销 / 部分平仓）；False=继续等待。
    """
    raw = rec.raw or {}
    symbol = rec.symbol
    qty = float(rec.qty or 0)
    parent_id = raw.get("entry_order_id")

    # ① TTL 过期（小时粒度巡检 → 实际寿命 12~13h）
    if rec.expires_at and datetime.utcnow() >= rec.expires_at:
        return _cancel_pending(db, trader, rec, "limit_expired", "挂单时效（TTL）到期自动撤销")

    # ② 交易所仓位为真相：≥95% 委托量 = 满额成交
    pos = trader.positions().get(symbol)
    amt = abs(pos["amt"]) if pos else 0.0
    if qty > 0 and amt >= qty * 0.95:
        _promote_pending(db, trader, rec, pos, None)
        return True

    # ③ 查父单终态/成交明细（working 中的单查不到时以挂单列表与仓位判断）
    st = None
    if parent_id:
        try:
            st = trader.algo_order(symbol, int(parent_id))
        except Exception:
            st = None
    exec_qty = 0.0
    status_str = ""
    if st:
        try:
            exec_qty = float(st.get("executedQty") or 0)
        except (TypeError, ValueError):
            exec_qty = 0.0
        status_str = str(st.get("algoStatus") or st.get("status") or "").upper()

    # ④ 父单满额成交（仓位可能已被插针 SL 打掉——仍转 OPENED，结算路径兜底）
    if status_str in ("FINISHED", "FILLED"):
        _promote_pending(db, trader, rec, pos, st)
        return True

    # ⑤ 部分成交（pending 未激活、无止损保护）：立即撤父单 + 平已成交部分
    if 0 < exec_qty < qty * 0.95:
        _close_partial_pending(db, trader, rec, exec_qty)
        return True

    # ⑥ 前提失效：未成交而现价已触及 TP1——行情已走完，不追
    price = trader.price(symbol)
    tp1 = float(rec.tp1 or 0)
    touched_tp1 = tp1 > 0 and (
        (rec.direction == "long" and price >= tp1)
        or (rec.direction == "short" and price <= tp1)
    )
    if touched_tp1:
        return _cancel_pending(db, trader, rec, "limit_premise",
                               f"现价 {price} 已触及 TP1 {tp1}，等回踩/反抽前提失效")

    # ⑦ 外部撤销/过期/拒绝（人工撤单或交易所侧清理）：对账
    if status_str in ("CANCELLED", "CANCELED", "EXPIRED", "REJECTED"):
        return _reconcile_cancelled(db, rec, status_str)

    # ⑧ 父单查询不到且不在挂单列表：交易所侧已消失，对账（连续两轮 API 异常才可能误判）
    if st is None and parent_id is not None:
        try:
            working_ids = trader.open_order_ids(symbol)
        except Exception:
            working_ids = None
        if working_ids is not None and parent_id not in working_ids:
            return _reconcile_cancelled(db, rec, "GONE")

    # ⑨ working 无成交，继续等待
    return False


def _place_late_tp2(db: Session, trader: BinanceTrader, rec: TradeRecord) -> None:
    """限价单 TP2 补挂（docs/10 §4）：OTOCO 两个 pending 位被 TP1/SL 占满，TP2
    （25% 原仓位，AI 给过 TP2 才有）在 TP1 成交后由巡检补挂；数量不足 min_qty 时
    放弃该档（tp2 置 0 → 剩余仓位走跟进止损）。失败不阻断结算（同样回退跟进止损）。"""
    try:
        f = trader.filters(rec.symbol)
        qty_tp2 = trader.round_qty(rec.symbol, float(rec.qty) * 0.25)
        if qty_tp2 < f["min_qty"]:
            rec.tp2 = 0
            logger.info("限价单 %s：TP2 数量 %s 低于最小数量 %s，放弃该档走跟进止损",
                        rec.symbol, qty_tp2, f["min_qty"])
            return
        tp2_order = trader.take_profit_reduce(
            rec.symbol, _close_side(rec.direction), qty_tp2,
            trader.round_price(rec.symbol, float(rec.tp2)))
        rec.raw = {
            **(rec.raw or {}),
            "tp2_order_id": tp2_order.get("algoId") or tp2_order.get("orderId"),
            "qty_tp2": qty_tp2,
        }
        logger.info("限价单 %s：TP2 已补挂 %s qty=%s", rec.symbol, rec.tp2, qty_tp2)
    except Exception as e:
        logger.warning("限价单 %s TP2 补挂失败: %s（剩余仓位改走跟进止损）", rec.symbol, e)
        rec.tp2 = 0


def settle_trades(db: Session, trader: BinanceTrader) -> int:
    """对非终态交易：以交易所为准推断事件、跟进止损、结算收益。返回处理笔数。

    先处理 PENDING 限价挂单（docs/10 §3：查单/撤销/成交转 OPENED），再走持仓
    结算循环——同一任务同一 Redis 锁，先挂单后持仓。
    """
    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    handled = 0
    # ① 限价挂单巡检（docs/10 §3）
    pending = db.execute(
        select(TradeRecord).where(
            TradeRecord.status == "PENDING",
            TradeRecord.testnet == settings.TRADING_TESTNET,
        )
    ).scalars().all()
    for rec in pending:
        try:
            if _handle_pending(db, trader, rec):
                handled += 1
        except Exception as e:
            db.rollback()
            logger.warning("限价挂单巡检 %s 失败: %s", rec.symbol, e)
    # ② 持仓结算（OPENED/TP1_HIT/TP2_HIT；刚转 OPENED 的限价单同轮直接接管）
    records = db.execute(
        select(TradeRecord).where(TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")))
    ).scalars().all()
    if not records:
        return handled
    positions = trader.positions()

    for rec in records:
        try:
            qty = float(rec.qty or 0)
            raw = rec.raw or {}
            pos = positions.get(rec.symbol)
            amt = abs(pos["amt"]) if pos else 0.0
            open_ids = trader.open_order_ids(rec.symbol)
            # 限价单（docs/10）：OTOCO 子单 algoId 逐轮补齐（成交时捕获失败/字段缺失的兜底）
            if rec.order_type == "limit" and (not raw.get("tp1_order_id") or not raw.get("sl_order_id")):
                _capture_otoco_children(trader, rec)
                raw = rec.raw or {}
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
                    if rec.order_type == "limit" and rec.tp2 and not raw.get("tp2_order_id"):
                        # 限价单（docs/10）：OTOCO 两个 pending 位被 TP1/SL 占满，
                        # TP2 在 TP1 成交后由巡检补挂（市价路径开仓时已挂，不进此分支）
                        _place_late_tp2(db, trader, rec)
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
    return handled
