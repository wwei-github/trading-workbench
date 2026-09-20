"""自动交易引擎（docs/06）

开仓（唯一通道 = 即时开仓，2026-09-14 起不再有 :42 批量开仓）：
    AI 结论 suggest 且推荐度 ≥60 → 分析落库即分发 open_trade_for_analysis
    → try_open_for_analysis 过闸门（在跑单上限 / 余额风控(可配置) / 同币无仓 /
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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import AccountTransfer, TradeEvent, TradeRecord
from app.services.binance_trader import BinanceTrader, BinanceTradeError
from app.services.exchange_pool import ExchangePool
from app.services.risk_guard import (
    liquidation_gate_pct,
    LIQ_GATE_BUFFER,
    LIQ_GATE_MMR_FALLBACK,
    PULLBACK_MARKET_PCT,
    MAX_SLIPPAGE_PCT,
)

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


def _adaptive_leverage(stop_pct: float, mmr: float, buffer: float) -> int:
    """按止损距离反推逐仓安全杠杆（2026-09-18 动态杠杆）。

    固定亏损法下止损亏损金额 = 名义×stop_pct，与杠杆无关；杠杆只决定
    保证金占用（名义/杠杆）与强平距离（≈1/杠杆−MMR）。从配置上限
    TRADING_LEVERAGE 向下搜索首个「强平距离 > 止损距离」的杠杆，止损计划
    （止损价/仓位/风险金额）保持不变，仅多占保证金换取强平不可达。
    返回 0 表示降到 1× 仍兜不住（stop_pct ≥ 1−MMR−缓冲，实际由下限闸门拦截）。
    """
    for lev in range(settings.TRADING_LEVERAGE, 0, -1):
        if stop_pct < liquidation_gate_pct(leverage=lev, mmr=mmr, buffer=buffer):
            return lev
    return 0


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
    a: AIAnalysis,
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
        _mark_open_block(db, a, "同币种已有在跑持仓")
        return False
    # 黑名单兜底（2026-09-18）：扫描宇宙过滤（主过滤）与开仓之间的时间差内，
    # 币可能被手动拉黑或刚触发自动拉黑——开仓前最后核对一次（D 双保险）
    from app.services.blacklist_service import active_symbols
    if symbol in active_symbols(db):
        logger.info("开仓跳过 %s：币种在黑名单（手动/连续止损自动）", symbol)
        _mark_open_block(db, a, "币种在黑名单（手动添加或连续止损自动冷却中）")
        return False
    # 下单环境可交易兜底（2026-09-19，ARX 案例）：扫描宇宙已过滤，这里防
    # 缓存到期瞬间/扫描并发窗口内的新候选。空集合（拉取失败）放行不阻断
    from app.services.binance_trader import BinanceTrader
    listed = BinanceTrader().listed_symbols()
    if listed and symbol not in listed:
        logger.info("开仓跳过 %s：下单环境未上架该合约（exchangeInfo 校验）", symbol)
        _mark_open_block(db, a, "下单环境未上架该合约（无法交易）")
        return False
    # 开单类型仅作记录（2026-09-15 拍板）：策略开关不再拦截开仓（未知类型也不拦）
    # EMA 排列前提随指标多数决重设计移除（2026-09-17）：方向判定权已移交指标多数决
    # （|net|≥3 才成候选），与 AI 提示词、扫描筛选同一把尺子，开仓侧不再二次设限
    trade_type = a.trade_type or ""
    direction = a.direction
    entry_ai = float(a.entry_price)
    sl = float(a.stop_loss)
    tp1 = float(a.take_profit_1)
    tp2 = float(a.take_profit_2 or 0)
    try:
        price = trader.price(symbol)
        # 入场偏离容差（pullback_market_pct 补丁参数，NULL=默认 1%）：现价对 AI
        # 入场价的最大偏离。一兼两职：① 普通单的开仓漂移上限；② 待回踩单的
        # 市价入场判定——回踩距离 ≤ 容差时不再干等，市价进场（RR 按现价复算
        # 见下方闸门），超容差维持「待回踩不下单」
        dev_tol = (
            float(cfg.pullback_market_pct)
            if cfg.pullback_market_pct is not None else PULLBACK_MARKET_PCT
        )
        deviation = abs(price - entry_ai) / entry_ai
        if deviation > dev_tol:
            if a.pullback_wait:
                logger.info(
                    "开仓跳过 %s：待回踩距离 %.2f%% 超容差 %.2f%%（pullback_market_pct），"
                    "市价进不满足盈亏计划，维持待回踩不下单",
                    symbol, deviation * 100, dev_tol * 100,
                )
                _mark_open_block(
                    db, a,
                    f"待回踩距离 {deviation * 100:.2f}% 超容差 {dev_tol * 100:.2f}%"
                    f"（pullback_market_pct），市价进不满足盈亏计划，维持待回踩",
                )
            else:
                logger.info("开仓作废 %s：现价偏离 AI 入场价超 %.2f%%", symbol, dev_tol * 100)
                _mark_open_block(db, a, f"现价偏离 AI 入场价超 {dev_tol * 100:.2f}%"
                                        f"（入场 {entry_ai}，开仓时现价 {price}）")
            return False
        if a.pullback_wait:
            logger.info(
                "开仓 %s：待回踩距离 %.2f%% ≤ 容差 %.2f%%，市价入场（RR 按现价复算把关）",
                symbol, deviation * 100, dev_tol * 100,
            )
        # 方向-价格可挂性复核（2026-09-16 恢复，交易所侧最后一道防线）：
        # SL/TP1 触发价必须在现价的正确侧，否则条件单被拒单（-2021 would
        # immediately trigger）→ 开完仓挂 SL 必败走紧急平仓空转。risk_guard
        # 已按 AI 三价拦反向止损，此处按现价兜底：AI 报价到开仓之间的漂移，
        # 以及前序 1% 偏离闸门内 TP1 落到现价错误侧的边缘情况
        placeable = (sl < price < tp1) if direction == "long" else (tp1 < price < sl)
        if not placeable:
            logger.info(
                "开仓跳过 %s：SL/TP 与现价矛盾不可挂单（方向 %s，SL=%s，入场=%s，TP1=%s，现价 %s）",
                symbol, direction, sl, entry_ai, tp1, price,
            )
            _mark_open_block(
                db, a,
                f"SL/TP 与现价矛盾不可挂单（方向 {direction}，SL={sl}，入场={entry_ai}，"
                f"TP1={tp1}，现价 {price}）",
            )
            return False
        # 盈亏比按现价复算（2026-09-18 补上，此前仅 AI 入场价口径校验）：市价成交
        # 价就是现价，待回踩单/漂移单偏离 AI 入场价后风险变宽、盈利变窄，RR 随之
        # 退化——按现价复算仍须 ≥AI_RR_MIN 铁律，否则市价进不划算
        if sl != price:
            rr_now = abs(tp1 - price) / abs(price - sl)
            if rr_now < settings.AI_RR_MIN:
                logger.info(
                    "开仓跳过 %s：盈亏比按现价复算 %.2f < %s（入场 %s，现价 %s）",
                    symbol, rr_now, settings.AI_RR_MIN, entry_ai, price,
                )
                _mark_open_block(
                    db, a,
                    f"盈亏比按现价复算 {rr_now:.2f} < {settings.AI_RR_MIN}"
                    f"（入场 {entry_ai}，现价 {price}），市价进不满足盈亏计划",
                )
                return False
        # 固定亏损仓位：基数分档 × 3% ÷ 止损距离
        stop_pct = abs(price - sl) / price
        if stop_pct <= 0:
            # 原"方向-价格一致性/RR 复验"闸门（2026-09-15 移除）曾顺带拦住 sl==现价；
            # 现在显式防零：止损距离为 0 无法按固定亏损法计算仓位
            logger.info("开仓放弃 %s：止损距离为 0（sl=%s，现价 %s）", symbol, sl, price)
            _mark_open_block(db, a, f"止损距离为 0（sl={sl}，现价 {price}），无法计算仓位")
            return False
        # 止损宽度硬上限（docs/12 §10 规则补丁参数 max_stop_pct）：
        # 「止损过宽」标签的补丁落点——AI 复盘产出建议值写入 system_config，
        # NULL=不限制（默认）。策略性闸门，先于强平可行性闸门执行
        if cfg.max_stop_pct is not None:
            max_stop = float(cfg.max_stop_pct)
            if stop_pct >= max_stop:
                logger.info(
                    "开仓跳过 %s：止损距离 %.2f%% 达到上限 %.2f%%（max_stop_pct 补丁参数）",
                    symbol, stop_pct * 100, max_stop * 100,
                )
                _mark_open_block(
                    db, a,
                    f"止损距离 {stop_pct * 100:.2f}% 达到配置上限 {max_stop * 100:.2f}%"
                    f"（max_stop_pct，复盘补丁参数）",
                )
                return False
        # 止损宽度闸门（防强平）→ 动态杠杆适配（2026-09-18）：止损距离超过
        # 上限杠杆的强平闸门时不再直接放弃，而是按 _adaptive_leverage 反推
        # 安全杠杆继续开单；低于下限 TRADING_LEVERAGE_MIN 才放弃（止损过宽
        # 到连 3× 逐仓都先强平）。这类单子固定亏损法本无解（预算放不进保证金
        # 里），此前一律跳过（BATUSDT 案例：止损 6.4% 被强平→20× 下加闸门；
        # 4USDT 案例：写死 MMR 0.5% 低估小币 1.5% 实际值→MMR 按币种实际档位取）。
        # 两段校验：① 粗筛——qty 未定，按第 1 档 MMR；② 复验——notional 确定
        # 后按实际档位（跨高档位 MMR 更高时杠杆再次下调）。
        # 缓冲可由补丁参数 liq_gate_buffer 覆盖（「执行偏差」标签落点，NULL=默认值）
        liq_buf = (
            float(cfg.liq_gate_buffer)
            if cfg.liq_gate_buffer is not None else LIQ_GATE_BUFFER
        )
        mmr_1 = trader.bracket_mmr(symbol)
        mmr_eff = mmr_1 if mmr_1 is not None else LIQ_GATE_MMR_FALLBACK
        # 限制层（2026-09-18）：动态杠杆下限，页面补丁参数 min_adapt_leverage 可覆盖
        # （NULL=默认 config.TRADING_LEVERAGE_MIN）——不是所有宽止损单都算完就开，
        # 安全杠杆低于下限的直接放弃
        lev_min = (
            int(cfg.min_adapt_leverage)
            if cfg.min_adapt_leverage is not None else settings.TRADING_LEVERAGE_MIN
        )
        leverage = _adaptive_leverage(stop_pct, mmr_eff, liq_buf)
        if leverage < lev_min:
            liq_gate_min = liquidation_gate_pct(leverage=lev_min, mmr=mmr_eff, buffer=liq_buf)
            logger.info(
                "开仓跳过 %s：止损距离 %.2f%% 过宽，动态安全杠杆 %s× 低于下限 %s×"
                "（下限闸门 %.2f%%，维持保证金率 %.2f%% + 缓冲），min_adapt_leverage 限制",
                symbol, stop_pct * 100, leverage, lev_min,
                liq_gate_min * 100, mmr_eff * 100,
            )
            _mark_open_block(
                db, a,
                f"止损距离 {stop_pct * 100:.2f}% 过宽，动态安全杠杆 {leverage}×"
                f" 低于下限 {lev_min}×（min_adapt_leverage 限制），放弃开单",
            )
            return False
        if leverage < settings.TRADING_LEVERAGE:
            liq_gate = liquidation_gate_pct(leverage=leverage, mmr=mmr_eff, buffer=liq_buf)
            logger.info(
                "开仓 %s：止损距离 %.2f%% 超 %s× 强平闸门 %.2f%%（维持保证金率 %.2f%%"
                "+ 缓冲），动态降杠杆至 %s×（强平距离 %.2f%%）",
                symbol, stop_pct * 100, settings.TRADING_LEVERAGE,
                liquidation_gate_pct(mmr=mmr_eff, buffer=liq_buf) * 100,
                mmr_eff * 100, leverage, liq_gate * 100,
            )
        risk_budget = _capital_base(wallet) * settings.TRADING_RISK_PCT / 100
        notional = risk_budget / stop_pct
        # 复验按名义所在档位精确取（2026-09-18 -2027 修复）：MMR / 档位最大杠杆 /
        # 档位名义上限。杠杆超档位 initialLeverage 会被交易所拒单（-2027 Exceeded
        # the maximum allowable position at current leverage——固定 20× 且名义落
        # 入低杠杆档时必现），此处封顶；名义超最大档 notionalCap 同样被拒，放弃
        info = trader.bracket_info(symbol, notional)
        if info is not None:
            mmr_exact, lev_max, notional_cap = info
        else:
            mmr_exact = trader.bracket_mmr(symbol, notional) or mmr_eff
            lev_max = notional_cap = None
        if notional_cap is not None and notional > notional_cap:
            logger.info(
                "开仓放弃 %s：名义 %.0f 超该币最大档名义上限 %.0f（交易所仓位限制）",
                symbol, notional, notional_cap,
            )
            _mark_open_block(
                db, a,
                f"名义 {notional:.0f} 超该币最大档名义上限 {notional_cap:.0f}"
                f"（交易所仓位限制）",
            )
            return False
        # 杠杆上限取三者最小：配置上限 / 档位最大杠杆 / 精确档 MMR 下的强平安全杠杆；
        # 低于下限 lev_min 仍放弃（限制层不变）
        cap_reason = None
        if lev_max is not None and leverage > lev_max:
            cap_reason = f"名义 {notional:.0f} 档位最大杠杆 {lev_max}×"
            leverage = lev_max
        lev_exact = _adaptive_leverage(stop_pct, mmr_exact, liq_buf)
        if lev_exact < leverage:
            cap_reason = f"精确档维持保证金率 {mmr_exact * 100:.2f}% 的强平闸门"
            leverage = lev_exact
        if leverage < lev_min:
            logger.info(
                "开仓跳过 %s：止损距离 %.2f%%，复验后杠杆 %s× 低于下限 %s×（%s）",
                symbol, stop_pct * 100, leverage, lev_min, cap_reason or "强平闸门",
            )
            _mark_open_block(
                db, a,
                f"止损距离 {stop_pct * 100:.2f}%，复验后杠杆 {leverage}×"
                f" 低于下限 {lev_min}×（{cap_reason or '强平闸门'}；min_adapt_leverage 限制）",
            )
            return False
        if cap_reason:
            logger.info(
                "开仓 %s：杠杆下调至 %s×（%s，强平距离 %.2f%%）",
                symbol, leverage, cap_reason,
                liquidation_gate_pct(leverage=leverage, mmr=mmr_exact, buffer=liq_buf) * 100,
            )
        qty = trader.round_qty(symbol, notional / price)
        f = trader.filters(symbol)
        if qty < f["min_qty"] or qty * price < f["min_notional"]:
            logger.info(
                "开仓放弃 %s：计算数量 %s 低于交易所最小规则（min_qty=%s, min_notional=%s）",
                symbol, qty, f["min_qty"], f["min_notional"],
            )
            _mark_open_block(
                db, a,
                f"计算数量 {qty} 低于交易所最小规则（min_qty={f['min_qty']}, min_notional={f['min_notional']}）",
            )
            return False
        # 市价入场按 MARKET_LOT_SIZE 校验（常比 LOT_SIZE 严得多，BBUSDT -4005 案例：
        # LOT_SIZE 100万 / MARKET_LOT_SIZE 5万），条件单按 LOT_SIZE；取两者更严的上限
        max_qty = min(f["max_qty"], f.get("market_max_qty", f["max_qty"]))
        if qty > max_qty:
            logger.info("开仓 %s：数量 %s 超交易所单笔上限 %s，按上限缩减（实际风险低于 %.0f%% 预算）",
                        symbol, qty, max_qty, settings.TRADING_RISK_PCT)
            qty = trader.round_qty(symbol, max_qty)
        margin_used = qty * price / leverage
        if margin_used > wallet - occupied:
            logger.info("开仓放弃 %s：所需保证金 %.2f 超过可用 %.2f", symbol, margin_used, wallet - occupied)
            _mark_open_block(db, a, f"所需保证金 {margin_used:.2f} 超过可用 {wallet - occupied:.2f}")
            return False
        # 盘口滑点实测闸门（GRIFFAIN 案例，2026-09-18）：按本单数量走订单簿逐层吃，
        # 预期滑点超 max_slippage_pct（补丁参数，NULL=默认 0.3%）放弃——簿薄的小币
        # 吃单会撑宽止损距离、实际风险超预算（0.637% 滑点 → 风险多 17%）。成交额
        # 门槛拦不住这类币（拉盘时 24h 成交额照样过线）。簿异常 None 不阻断
        slip_max = (
            float(cfg.max_slippage_pct)
            if getattr(cfg, "max_slippage_pct", None) is not None else MAX_SLIPPAGE_PCT
        )
        if slip_max > 0:
            est_slip = trader.market_slippage_estimate(
                symbol, "BUY" if direction == "long" else "SELL", qty
            )
            if est_slip is not None and est_slip > slip_max:
                logger.info(
                    "开仓放弃 %s：盘口滑点实测 %.2f%% 超上限 %.2f%%（max_slippage_pct，"
                    "数量 %s 走簿计算）——盘口过薄，实际风险将超预算",
                    symbol, est_slip * 100, slip_max * 100, qty,
                )
                _mark_open_block(
                    db, a,
                    f"盘口滑点实测 {est_slip * 100:.2f}% 超上限 {slip_max * 100:.2f}%"
                    f"（max_slippage_pct，按本单数量 {qty} 走簿计算），盘口过薄放弃开单",
                )
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
            _mark_open_block(
                db, a,
                f"止盈一数量 {qty_tp1_pre} 低于交易所最小数量 {f['min_qty']}，分批止盈无法挂单",
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
        else:
            # 非 -1121 的开仓失败（如 testnet 对未上架币返回空 ticker 而非 -1121，
            # PUMPUSDT 案例）：同样不留"既没开单也没原因"的悬案，落拒开原因
            _mark_open_block(db, a, f"开仓失败：{str(e)[:200]}")
        return False


def _mark_open_block(db: Session, a: AIAnalysis, reason: str) -> None:
    """开仓闸门拒开原因落库（ai_analyses.open_block_reason，前端 AI 分析卡展示）。

    仅覆盖进入下单序列之前的拦截；下单序列内的成败由 trade_records 呈现。
    写库失败不阻断流程（原因展示是尽力而为，闸门决策以日志为准）。
    """
    try:
        a.open_block_reason = reason[:250]
        db.commit()
    except Exception:
        db.rollback()


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
        _mark_open_block(db, a, "分析已超过 1 小时（信号过期）")
        return False
    if db.execute(
        select(TradeRecord.id).where(TradeRecord.ai_analysis_id == a.id)
    ).first():
        return False  # 该分析已开过仓
    # 待回踩单不再在此跳过（2026-09-18）：回踩距离 ≤ pullback_market_pct 容差
    # 时市价入场、超容差维持不下单——判定移入 _attempt_open 偏离闸门
    positions = trader.positions()
    # 市场风险防护（docs/15）：L2/L3 暂停全部开仓
    if settings.MARKET_GUARD_ENABLED:
        from app.services.risk_guard import get_current_level
        lvl = get_current_level(db)
        if lvl >= 2:
            logger.info("即时开仓跳过 %s：市场防护 L%d 暂停开仓", a.symbol, lvl)
            _mark_open_block(db, a, f"市场防护 L{lvl} 暂停开仓")
            return False
    # 单数上限（0 = 不限单数，仅靠余额风控兜底）
    if cfg.max_open_trades > 0 and len(positions) >= cfg.max_open_trades:
        logger.info("即时开仓跳过 %s：在跑单子已达上限 %d", a.symbol, cfg.max_open_trades)
        _mark_open_block(db, a, f"在跑单子已达上限 {cfg.max_open_trades}")
        return False
    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    occupied = sum(p["initial_margin"] for p in positions.values())
    # 余额风控（2026-09-15 起可配置）：开仓最低可用余额占钱包比例，默认 50%（原固定 70%）
    min_free = (
        settings.TRADING_MIN_FREE_PCT
        if cfg is None or cfg.trading_min_free_pct is None
        else float(cfg.trading_min_free_pct)
    )
    if wallet > 0 and wallet - occupied < wallet * min_free:
        logger.info("即时开仓跳过 %s：可用余额低于总资金 %.0f%%", a.symbol, min_free * 100)
        _mark_open_block(db, a, f"可用余额低于总资金 {min_free * 100:.0f}%")
        return False
    if _capital_base(wallet) <= 0:
        logger.info("即时开仓跳过 %s：钱包余额低于最小风险档位", a.symbol)
        _mark_open_block(db, a, "钱包余额低于最小风险档位")
        return False
    # 即将进入闸门序列：清掉沿用/上一轮尝试残留的拒开原因，本次成败由闸门重新裁定
    if a.open_block_reason:
        try:
            a.open_block_reason = None
            db.commit()
        except Exception:
            db.rollback()
    return _attempt_open(db, trader, cfg, a, wallet, positions)


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


def sync_account_transfers(db: Session, trader: BinanceTrader) -> int:
    """同步合约账户资金划转记录（income TRANSFER）到 account_transfers 表。

    增量拉取：从库内最新 transfer_time 前移 60s 重叠窗口开始（首次从 7 天前起），
    tran_id 唯一约束 + on_conflict_do_nothing 幂等去重。返回新插入条数。
    """
    last = db.execute(
        select(AccountTransfer.transfer_time)
        .order_by(AccountTransfer.transfer_time.desc())
        .limit(1)
    ).scalar()
    since = last - timedelta(seconds=60) if last else datetime.utcnow() - timedelta(days=7)
    since_ms = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = trader.transfer_rows(since_ms)
    inserted = 0
    for row in rows:
        try:
            amount = float(row.get("income") or 0)
        except (TypeError, ValueError):
            continue
        if amount == 0:
            continue
        t_ms = int(row.get("time") or 0)
        if t_ms <= 0:
            continue
        # 测试网 TRANSFER 的 tranId 恒为 0（注意 0 是 falsy，不能用 `or` 兜底）：
        # tranId 有效时用原值，否则用 时间+资产+金额 组合键做幂等去重
        raw_tran = row.get("tranId")
        tran_id = (
            str(raw_tran)
            if raw_tran not in (None, "", 0, "0")
            else f"{t_ms}:{row.get('asset') or 'USDT'}:{row.get('income')}"
        )
        stmt = pg_insert(AccountTransfer).values(
            tran_id=tran_id,
            asset=row.get("asset") or "USDT",
            amount=amount,
            transfer_time=datetime.utcfromtimestamp(t_ms / 1000),
            raw=row,
        ).on_conflict_do_nothing(index_elements=["tran_id"])
        res = db.execute(stmt)
        inserted += res.rowcount or 0
    db.commit()
    return inserted


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
                # 费用拆分（复盘归因 2026-09-18）：手续费与资金费单列，
                # 供「计划 vs 实际」偏差分析（4USDT 案例：费用+资金费吃掉超预算 10%）
                fee_total = sum(
                    float(r.get("income") or 0)
                    for r in rows if r.get("incomeType") == "COMMISSION"
                )
                funding_total = sum(
                    float(r.get("income") or 0)
                    for r in rows if r.get("incomeType") == "FUNDING_FEE"
                )
                rec.fee_total = round(fee_total, 6)
                rec.funding_total = round(funding_total, 6)
                pnl = round(trader.sum_income(rows, trader.INCOME_KEEP), 6)
                rec.realized_pnl = pnl
                rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
                if (rec.raw or {}).get("guard_closed"):
                    exit_reason = "market_guard"
                elif insurance_clear != 0:
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
                    "fee_total": rec.fee_total, "funding_total": rec.funding_total,
                })
                db.commit()
                handled += 1
                logger.info("交易结算 %s %s：pnl=%s（%s%%）", rec.symbol, rec.direction,
                            rec.realized_pnl, rec.pnl_pct)
                # 黑名单自动登记（D，2026-09-18）：实际止损金额严重超出计划风险预算
                # （≥1.2 倍）或强平 → 自动拉黑冷却 30 天。失败不阻断结算
                try:
                    from app.services.blacklist_service import register_settle
                    register_settle(db, rec.symbol, rec.risk_amount,
                                    rec.realized_pnl, rec.exit_reason or "")
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning("黑名单登记失败 %s: %s", rec.symbol, e)
                continue

            ratio = amt / qty
            # 止损挂单存活巡检：SL 单被撤/丢失时补挂（裸仓保护，docs/07 §8-A2）
            _rehang_lost_sl(db, trader, rec, open_ids)
            if rec.status == "OPENED":
                if tp2_gone and ratio <= 0.35:
                    # 同一小时两档止盈都成交：从交易所成交明细按时间序+数量精确拆分
                    # 各档价差盈亏（与常规路径的 _tranche_pnl 书签口径同义，但两档同轮
                    # 成交无法用书签差值区分；TP 条件单触发后换新单号，无法按单号归集）。
                    # 拆分失败回退旧口径——两档合计只记在 TP2 上，TP1 不显示金额
                    realized = _record_realized(db, trader, rec)
                    qty_tp1, qty_tp2 = float(raw.get("qty_tp1") or 0), float(raw.get("qty_tp2") or 0)
                    tranche1 = tranche2 = None
                    if qty_tp1 > 0 and qty_tp2 > 0:
                        try:
                            since_ms = int(rec.opened_at.replace(
                                tzinfo=timezone.utc).timestamp() * 1000) - 5_000
                            tranche1, tranche2 = trader.fill_tranche_pnls(
                                rec.symbol, since_ms, qty_tp1, qty_tp2)
                        except Exception as e:
                            logger.warning("拆分双档止盈金额失败 %s: %s（回退合计口径）",
                                           rec.symbol, e)
                            tranche1 = tranche2 = None
                    tp1_detail = {"qty": raw.get("qty_tp1")}
                    if tranche1 is not None:
                        tp1_detail["pnl"] = tranche1
                    _add_event(db, rec, "TP1_FILL", tp1_detail)
                    rec.status = "TP1_HIT"
                    tp2_detail = {"qty": raw.get("qty_tp2")}
                    if tranche2 is not None:
                        tp2_detail.update({"pnl": tranche2, "cum_pnl": realized})  # 两档累计
                    else:
                        tp2_detail["realized_pnl"] = realized  # 两档累计（旧口径）
                    _add_event(db, rec, "TP2_FILL", tp2_detail)
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
