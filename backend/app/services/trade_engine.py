"""自动交易引擎（docs/06）

开仓（唯一通道 = 即时开仓，2026-09-14 起不再有 :42 批量开仓）：
    AI 结论 suggest 且推荐度 ≥60 → 分析落库即分发 open_trade_for_analysis
    → try_open_for_analysis 过闸门（在跑单上限 / 余额风控(可配置) / 同币无仓 /
    方向价格复验）→ 固定亏损法定仓位（基数分档 ×3% ÷ 止损距离）
    → 市价开仓 + 挂 SL/TP1/TP2 → 入库；闸门未过即放弃，无批次重试

结算（settle_trades，每小时 :42 巡检）：
    以交易所为真源（持仓量/挂单/已实现盈亏）推断成交事件：
    仓位归零 → 结算收益（正/负值）；外部平仓识别（actualOrderId 单链，
    2026-10-02）→ exit_reason=manual 不误标止损类；TP1/TP2 成交 → 状态迁移；
    止损挂单消失（被撤/交易所异常）→ 按当前止损价补挂（裸仓保护）；
    TP1 成交 → 止损移至成本价 ± 往返费缓冲（保本，幂等，docs/24 A1''）；
    无 TP2 的单：TP1 止盈 80%，剩余 20% 从 TP1 起跟进止损（2026-10-09 调整）；
    TP2 后每次巡检跟进止损（最近3根已收盘K线极值，只收紧不放松）
"""
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace
from uuid import UUID
from typing import Optional

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import AccountTransfer, TradeEvent, TradeRecord
from app.services.binance_trader import BinanceTrader, BinanceTradeError
from app.services.exchange_pool import ExchangePool
from app.services.position_advisor import advice_due
from app.services.risk_guard import (
    liquidation_gate_pct,
    LIQ_GATE_BUFFER,
    LIQ_GATE_MMR_FALLBACK,
    PULLBACK_MARKET_PCT,
    MAX_SLIPPAGE_PCT,
)
from app.services.task_log import close_task, open_task

import numpy as np
from app.services.strategy.ema_gate import ema_series

logger = logging.getLogger(__name__)


def _capital_base(wallet: float) -> float:
    """风险基数：按钱包余额首位数取整，下限 100、上限 5000（2026-09-21 拍板）：
    首位 digit = int(wallet / 10^n)，基数 = digit × 10^n，超 5000 封顶、
    不足 100 按 100 兜底。
    例：4800 → 4000、980 → 900、65 → 100、5200/15000 → 5000"""
    if wallet <= 0:
        return 0.0
    magnitude = 10 ** int(math.log10(wallet))
    digit = int(wallet / magnitude)
    return float(min(max(digit * magnitude, 100), 5000))


def _adaptive_leverage(stop_pct: float, mmr: float, buffer: float,
                       lev_cap: Optional[int] = None) -> int:
    """按止损距离反推逐仓安全杠杆（2026-09-18 动态杠杆）。

    固定亏损法下止损亏损金额 = 名义×stop_pct，与杠杆无关；杠杆只决定
    保证金占用（名义/杠杆）与强平距离（≈1/杠杆−MMR）。从配置上限向下搜索
    首个「强平距离 > 止损距离」的杠杆，止损计划（止损价/仓位/风险金额）保持
    不变，仅多占保证金换取强平不可达。
    lev_cap（2026-10-02 上页面）：杠杆上限（system_config.leverage_max ?? env），
    由调用方解析传入；None 回落 settings.TRADING_LEVERAGE。
    返回 0 表示降到 1× 仍兜不住（stop_pct ≥ 1−MMR−缓冲，实际由下限闸门拦截）。
    """
    for lev in range(int(lev_cap or settings.TRADING_LEVERAGE), 0, -1):
        if stop_pct < liquidation_gate_pct(leverage=lev, mmr=mmr, buffer=buffer):
            return lev
    return 0


def _add_event(db: Session, rec: TradeRecord, event_type: str, detail: Optional[dict] = None) -> None:
    db.add(TradeEvent(trade_record_id=rec.id, event_type=event_type, detail=detail or {}))


def _close_side(direction: str) -> str:
    return "SELL" if direction == "long" else "BUY"


def _tp_ratio_cfg(cfg) -> tuple[float, float, float]:
    """TP 分批比例解析（% 值，2026-10-09 配置化）：system_config 页面值优先，
    NULL/无 cfg 回落 env 默认（settings.TP*_QTY_PCT，60/20/80）；
    非法值（越界/两档合计>95%）回落 env 默认——API 层已拦，此处为引擎侧防线。
    返回 (有TP2时TP1%, TP2%, 无TP2时TP1%)，剩余额一律由跟进止损退出。"""
    def _pct(v, env_key: str) -> float:
        if v is not None:
            try:
                f = float(v)
                if 0 < f < 100:
                    return f
            except (TypeError, ValueError):
                pass
        return float(getattr(settings, env_key))
    p1 = _pct(cfg.tp1_qty_pct_with_tp2 if cfg is not None else None,
              "TP1_QTY_PCT_WITH_TP2")
    p2 = _pct(cfg.tp2_qty_pct if cfg is not None else None, "TP2_QTY_PCT")
    p1s = _pct(cfg.tp1_qty_pct_no_tp2 if cfg is not None else None,
               "TP1_QTY_PCT_NO_TP2")
    if p1 + p2 > 95.0:
        p1 = float(settings.TP1_QTY_PCT_WITH_TP2)
        p2 = float(settings.TP2_QTY_PCT)
    if p1s > 95.0:
        p1s = float(settings.TP1_QTY_PCT_NO_TP2)
    return p1, p2, p1s


def _tp_quantities(qty: float, tp2: float,
                   p1: float = 60.0, p2: float = 20.0, p1s: float = 80.0) -> tuple[float, float]:
    """TP1/TP2 分配数量（比例 % 值由 _tp_ratio_cfg 从 system_config/env 解析）：
    AI 给出 TP2 → TP1 p1% / TP2 p2% 两档分批止盈，剩余额跟进；
    未给出（AI 未报 / 前方无结构位程序回退清零 / 配置关闭 TP2）
    → TP1 一次止盈 p1s%，剩余额从 TP1 成交起由跟进止损退出（与 TP2 后同机制）"""
    if tp2 and tp2 > 0:
        return qty * p1 / 100, qty * p2 / 100
    return qty * p1s / 100, 0.0


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
        # 止损锚类型（docs/23）：structure/near，交易记录随快照自持，复盘分组用
        "stop_anchor": a.stop_anchor,
        # 战法命中标注（2026-10-02，仅借鉴）：随快照自持，删战法不断链，复盘分组用
        "tactic_name": a.tactic_name,
        "tactic_reason": a.tactic_reason,
        # 4h 趋势期别（2026-10-09 r2）：launch/mid/late/exhausted，随快照自持，
        # 复盘「4h趋势期」维度分组用；旧行/无 4h 数据 None→「未知」
        "htf_phase": a.htf_phase,
        # 止盈模式（2026-10-10，docs/19 §3.4）：随快照自持——复盘弹窗 AI 卡
        # 按 trail 显「纯跟踪」Tag 与「止盈1·参考」标签，缺了会错显分批
        "tp_mode": a.tp_mode,
        # 量比（docs/24 A5b）：信号K线量 ÷ 近20根基准量（中位量，2026-09-29 口径），
        # 实盘归因按量比门槛分组对比；旧行/无信号上下文为 None 归「未知」
        "volume_ratio": (
            float(a.scan_result.volume)
            if a.scan_result is not None and a.scan_result.volume is not None
            else None
        ),
        # 回踩触点（docs/22）：retest/swing/ema21/55/144，实盘归因按触点分组
        # 对比胜率/EV（2026-10-08）；开单时点快照自持，旧行/无信号上下文为 None
        "touch_ref": (
            a.scan_result.touch_ref if a.scan_result is not None else None
        ),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "scan_result_id": None,
    }


# ── 开仓 ──────────────────────────────────────────────────────


def _near_discounted_budget(risk_budget: float, stop_anchor: Optional[str],
                            factor: Optional[float] = None) -> float:
    """近锚单风险折价（docs/23，2026-09-29）。

    止损贴突破K极值的单被扫率未知（观察期敞口折价），且止损窄 → 名义仓位放大
    → 手续费/滑点磨损绝对额放大；risk_amount 随 qty 同步变小，复盘 R 口径自洽。
    仅 AI 显式声明 near（risk_guard 已校验止损确贴突破K极值）时生效；
    折价不改变 EV/R 符号只缩敞口，复盘胜率验证后可在页面把系数调回 1.0 放开
    （system_config.near_anchor_factor，2026-10-02 上页面；None 回落 env 默认）。"""
    if stop_anchor == "near":
        _f = settings.NEAR_ANCHOR_RISK_FACTOR if factor is None else float(factor)
        return risk_budget * _f
    return risk_budget


def _attempt_open(
    db: Session, trader: BinanceTrader, cfg: SystemConfig,
    a: AIAnalysis,
    wallet: float, positions: dict,
    force_market: bool = False,
    beta: Optional[float] = None,
) -> bool:
    """对单条分析过全部开仓闸门并尝试开仓（docs/06 §3 逐项检查）。

    返回是否成功开仓；成功时更新 positions（供调用方累计在跑数与占用保证金）。
    即时开仓唯一入口（try_open_for_analysis，分析完成即分发）调用；
    开仓失败统一 rollback 并落 FAILED 记录（仅 -1121 未上架）。

    force_market（2026-09-28 手动开单语义）：跳过入场偏离/待回踩时机闸门，
    以现价市价立即入场——等待回踩是自动成交的时机安排，手动点击是用户对
    当下价格的明确意图；其余风控闸门（RR 现价复算 / SL-TP 可挂性 / 保证金 /
    滑点 / 上限 / 黑名单）全部保留。
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
    # 缓存到期瞬间/扫描并发窗口内的新候选。用本单账号 trader（账号属性
    # testnet 决定基地址）——2026-10-03 双网同开后各网独立校验。空集合
    # （拉取失败）放行不阻断
    listed = trader.listed_symbols()
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
    # TP2 分批止盈开关（2026-10-08，交易配置页）：默认关闭——AI 报出的 TP2 不挂档，
    # 出场走「仅 TP1 80% + 剩余 20% 跟进」；手动接管单不受此开关限制（显式输入）
    if not cfg.tp2_enabled:
        tp2 = 0
    # 止盈模式（2026-10-10，docs/19 §3.4）：system_config.tp_mode 强制（ladder/trail）
    # 优先；ai/NULL/非法 → 按 AI 分析的选择（ai_analyses.tp_mode，缺省 ladder）。
    # trail：不挂止盈档，浮盈≥1×ATR 武装保本后跟进（巡检 _trail_mode_patrol）
    _cfg_mode = (getattr(cfg, "tp_mode", None) or "").strip()
    tp_mode_eff = (
        _cfg_mode if _cfg_mode in ("ladder", "trail")
        else (a.tp_mode if a.tp_mode in ("ladder", "trail") else "ladder")
    )
    try:
        price = trader.price(symbol)
        # 风控参数解析（2026-10-02 上页面）：system_config 列可页面覆盖，
        # NULL（页面留空）= 回落 env 默认；本单内取值一致
        rr_min = (
            float(cfg.ai_rr_min) if cfg.ai_rr_min is not None
            else float(settings.AI_RR_MIN)
        )
        lev_cap = (
            int(cfg.leverage_max) if cfg.leverage_max is not None
            else int(settings.TRADING_LEVERAGE)
        )
        risk_pct = (
            float(cfg.risk_pct) if cfg.risk_pct is not None
            else float(settings.TRADING_RISK_PCT)
        )
        near_factor = (
            float(cfg.near_anchor_factor) if cfg.near_anchor_factor is not None
            else float(settings.NEAR_ANCHOR_RISK_FACTOR)
        )
        # 入场偏离容差（pullback_market_pct 补丁参数，NULL=默认 1%）：现价对 AI
        # 入场价的最大偏离。一兼两职：① 普通单的开仓漂移上限；② 待回踩单的
        # 市价入场判定——回踩距离 ≤ 容差时不再干等，市价进场（RR 按现价复算
        # 见下方闸门），超容差维持「待回踩不下单」
        dev_tol = (
            float(cfg.pullback_market_pct)
            if cfg.pullback_market_pct is not None else PULLBACK_MARKET_PCT
        )
        deviation = abs(price - entry_ai) / entry_ai
        if force_market:
            logger.info(
                "开仓 %s：手动开单市价入场（现价 %s 对 AI 入场 %s 偏离 %.2f%%，"
                "跳过偏离/待回踩判定）",
                symbol, price, entry_ai, deviation * 100,
            )
        elif deviation > dev_tol:
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
        if a.pullback_wait and not force_market:
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
        # 退化——按现价复算仍须 ≥盈亏比铁律下限（ai_rr_min 可页面覆盖），否则市价进不划算
        if sl != price:
            rr_now = abs(tp1 - price) / abs(price - sl)
            if rr_now < rr_min:
                logger.info(
                    "开仓跳过 %s：盈亏比按现价复算 %.2f < %s（入场 %s，现价 %s）",
                    symbol, rr_now, rr_min, entry_ai, price,
                )
                _mark_open_block(
                    db, a,
                    f"盈亏比按现价复算 {rr_now:.2f} < {rr_min}"
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
        leverage = _adaptive_leverage(stop_pct, mmr_eff, liq_buf, lev_cap=lev_cap)
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
        if leverage < lev_cap:
            liq_gate = liquidation_gate_pct(leverage=leverage, mmr=mmr_eff, buffer=liq_buf)
            logger.info(
                "开仓 %s：止损距离 %.2f%% 超 %s× 强平闸门 %.2f%%（维持保证金率 %.2f%%"
                "+ 缓冲），动态降杠杆至 %s×（强平距离 %.2f%%）",
                symbol, stop_pct * 100, lev_cap,
                liquidation_gate_pct(mmr=mmr_eff, buffer=liq_buf, lev_cap=lev_cap) * 100,
                mmr_eff * 100, leverage, liq_gate * 100,
            )
        # 止损金额模式（2026-09-27）：pct=风险基数分档×风险比例%（现行默认，
        # 比例可页面覆盖，随钱包首位取整缩放）；fixed=固定 USDT 金额
        # （risk_fixed_amount），不随钱包缩放，仓位=金额÷止损距离
        # （钱包 ≤0 仍由闸门拦截，固定金额不放大透支风险）
        if (cfg.risk_budget_mode or "pct") == "fixed":
            risk_budget = float(
                cfg.risk_fixed_amount if cfg.risk_fixed_amount is not None else 3.0
            )
        else:
            risk_budget = _capital_base(wallet) * risk_pct / 100
        risk_budget = _near_discounted_budget(risk_budget, a.stop_anchor,
                                              factor=near_factor)
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
        lev_exact = _adaptive_leverage(stop_pct, mmr_exact, liq_buf, lev_cap=lev_cap)
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
                        symbol, qty, max_qty, risk_pct)
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
        # 盘口价差预检（docs/24 A4'）：bookTicker 的 bid-ask 价差/中间价超
        # SPREAD_MAX_PCT（补丁前默认 0.3%）放弃开单——点差是市价单越不过去的
        # 固定成本，过宽说明流动性枯竭/行情剧烈（走簿滑点实测以最优价为基准，
        # 对点差本身不敏感）。查询异常不拦截（放行，走原有滑点实测兜底）；0=关闭
        if settings.SPREAD_MAX_PCT > 0:
            try:
                bid, ask = trader.book_ticker(symbol)
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 0.0
                if spread_pct > settings.SPREAD_MAX_PCT:
                    logger.info(
                        "开仓放弃 %s：盘口价差 %.2f%% 超上限 %.2f%%"
                        "（SPREAD_MAX_PCT，bid=%s ask=%s）",
                        symbol, spread_pct * 100, settings.SPREAD_MAX_PCT * 100, bid, ask,
                    )
                    _mark_open_block(
                        db, a,
                        f"盘口价差过宽 {spread_pct * 100:.2f}%"
                        f"（上限 {settings.SPREAD_MAX_PCT * 100:.2f}%，SPREAD_MAX_PCT，"
                        f"bid={bid} ask={ask}）",
                    )
                    return False
            except Exception as spread_err:
                logger.warning(
                    "开仓 %s：盘口价差预检查询失败，放行（走滑点实测兜底）: %s",
                    symbol, spread_err,
                )
        # TP1 分批数量预检（下单前）：低于交易所最小数量时整单放弃——
        # 开完仓才挂 TP1 被交易所拒单，会走紧急撤单+平仓，且该失败不落记录，
        # 候选每小时重试"开仓→拒单→平仓"空转（docs/07 §8-A1）。TP2 本就有
        # min_qty 检查（不足时静默跳过该档），TP1 是主止盈档、缺它无意义。
        # 纯跟踪单不挂 TP，跳过本预检
        p1, p2, p1s = _tp_ratio_cfg(cfg)
        q1_pre, _ = _tp_quantities(qty, tp2, p1, p2, p1s)
        qty_tp1_pre = trader.round_qty(symbol, q1_pre)
        if tp_mode_eff != "trail" and qty_tp1_pre < f["min_qty"]:
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
            rec_tp1 = tp1
            entry_atr = None
            if tp_mode_eff == "trail":
                # 纯跟踪（docs/19 §3.4 V1）：不挂止盈档，tp1 仅 RR 校验参考价；
                # 全仓由「浮盈≥1×ATR 武装保本 → 跟进止损」退出（巡检负责）。
                # 开仓时算好入场 ATR(22,1d) 快照存 raw（巡检武装线的分母，与
                # _chandelier_sl 同口径）；缺失时巡检退化 1R 武装线
                qty_tp1 = qty_tp2 = 0.0
                tp1_order = tp2_order = None
                rec_tp1 = None
                try:
                    from app.services.strategy.technical import atr_series
                    _d1 = ExchangePool().get_klines(symbol, "1d", 30)
                    _atr_s = atr_series(_d1, period=22)
                    if (_atr_s is not None and len(_atr_s)
                            and math.isfinite(float(_atr_s[-1]))
                            and float(_atr_s[-1]) > 0):
                        entry_atr = round(float(_atr_s[-1]), 8)
                except Exception as atr_err:
                    logger.warning("开仓 %s：entry ATR 快照失败（纯跟踪退化 1R 武装线）: %s",
                                   symbol, atr_err)
            else:
                q1, q2 = _tp_quantities(qty, tp2, p1, p2, p1s)
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
                signal_type=(a.scan_result.signal_type if a.scan_result is not None else None),
                pattern=(a.scan_result.pattern if a.scan_result is not None else None),
                rule_version=settings.RULE_VERSION,
                testnet=trader.testnet,
                account_key=trader.account_key,
                ai_snapshot=_ai_snapshot(a),
                entry_price=fill_price,
                qty=qty,
                notional=round(qty * fill_price, 2),
                leverage=leverage,
                margin_mode="isolated",
                margin_used=round(qty * fill_price / leverage, 4),
                risk_amount=round(risk_amount, 4),
                risk_budget_mode=(cfg.risk_budget_mode or "pct"),  # 开仓时刻模式快照（追溯口径）
                beta=(round(beta, 4) if beta is not None else None),  # 开仓时 β 快照（docs/13 #1 热度加权；None=不可得按 1.0 计）
                stop_loss=sl,
                tp1=rec_tp1,  # 纯跟踪单 None（tp1 仅 RR 参考价，不挂档不落现值）
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
                    # 止盈模式快照（docs/19 §3.4）：ladder/trail + 纯跟踪的武装线分母
                    "tp_mode": tp_mode_eff,
                    "entry_atr": entry_atr,
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
                "tp_mode": tp_mode_eff, "entry_atr": entry_atr,
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
                        signal_type=(a.scan_result.signal_type if a.scan_result is not None else None),
                        pattern=(a.scan_result.pattern if a.scan_result is not None else None),
                        rule_version=settings.RULE_VERSION,
                        testnet=trader.testnet,
                        account_key=trader.account_key,
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


def _closed_pnl_since(db: Session, account_key: Optional[str], testnet: bool,
                      since: datetime) -> float:
    """本账号自 since 起已平仓净盈亏合计（分层熔断用，docs/13 #2）。

    账号口径复用 _adopt_acc_cond（NULL 旧行按 testnet 环境回落，与在跑单检查同源）。
    """
    return float(db.execute(
        select(func.coalesce(func.sum(TradeRecord.realized_pnl), 0.0)).where(
            _adopt_acc_cond(SimpleNamespace(account_key=account_key, testnet=testnet)),
            TradeRecord.status == "CLOSED",
            TradeRecord.closed_at >= since,
        )
    ).scalar_one())


def _daily_closed_pnl(db: Session, account_key: Optional[str], testnet: bool) -> float:
    """本账号 UTC 当日已平仓净盈亏合计（当日亏损熔断用，docs/20 复盘 C 项）。"""
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return _closed_pnl_since(db, account_key, testnet, day_start)


def _daily_loss_cap(wallet: float, pct: Optional[float] = None) -> float:
    """当日亏损熔断阈值 = 风险基数 × 熔断阈值%（2026-09-29 由写死
    30 USDT 改按账户动态：1000 账户 90、5000 账户 450；9% ≈ 3 笔满损）。
    pct（2026-10-02 上页面）：system_config.daily_loss_stop_pct 覆盖时传入；
    None 回落 env 默认。"""
    _pct = settings.DAILY_LOSS_STOP_PCT if pct is None else float(pct)
    return _capital_base(wallet) * _pct / 100.0


# 钱包余额缓存（熔断状态卡片用）：account_key → (wallet, ts)，5 分钟 TTL——
# scan_status 前端 30s 轮询，逐次查币安会把 GET /fapi/v2/account 打成高频
_wallet_cache: dict[str, tuple[float, float]] = {}


def breaker_status_by_net(db: Session, cfg) -> dict:
    """各网当日熔断状态（顶部卡片「熔断状态」列，2026-10-04）：
    {"testnet": {...}, "live": {...}}，每网：
    - enabled：熔断生效前提（开关开 && 阈值>0 && 该网有自动交易账号）
    - active：True=已熔断 / False=未熔断 / None=无法判定（钱包查询失败或未启用）
    - day_pnl：该网自动交易账号 UTC 当日已平仓净亏合计
    - cap：熔断阈值（多账号取最小=最保守）
    - week/month（docs/13 #2 分层，2026-10-04）：周/月回撤状态
      week: {active, hit, acked, pnl, cap}；month: {active, hit, acked, pnl, cap}
      month.active = 触发且未人工确认（ack 在当前 UTC 月内 = 已确认放行）
      week.active = 触发且未人工确认（ack 在当前 UTC 周内 = 已确认放行，
      2026-10-07 对称月熔断补人工通道）
    钱包余额 5 分钟进程内缓存；这是展示口径，开仓拦截仍以 open gate 实时判定为准。"""
    from app.services.account_registry import auto_trade_accounts

    enabled_cfg = (
        bool(cfg.daily_loss_stop_enabled) if cfg.daily_loss_stop_enabled is not None
        else settings.DAILY_LOSS_STOP_ENABLED
    )
    pct = (
        float(cfg.daily_loss_stop_pct) if cfg.daily_loss_stop_pct is not None
        else float(settings.DAILY_LOSS_STOP_PCT)
    )
    breaker_on = enabled_cfg and pct > 0
    week_pct = (
        float(cfg.weekly_dd_stop_pct) if cfg.weekly_dd_stop_pct is not None
        else float(settings.WEEKLY_DD_STOP_PCT)
    )
    month_pct = (
        float(cfg.monthly_dd_stop_pct) if cfg.monthly_dd_stop_pct is not None
        else float(settings.MONTHLY_DD_STOP_PCT)
    )

    by_net: dict[bool, list] = {True: [], False: []}
    for acc in auto_trade_accounts(db):
        by_net[acc.testnet].append(acc)

    now = time.time()
    now_utc = datetime.utcnow()
    month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = (now_utc - timedelta(days=now_utc.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out: dict = {}
    for net, net_key in ((True, "testnet"), (False, "live")):
        accs = by_net[net]
        day_pnl = sum(_daily_closed_pnl(db, a.key, a.testnet) for a in accs)
        caps: list[float] = []
        bases: list[float] = []
        any_active = False
        need_wallet = (breaker_on and accs) or ((week_pct > 0 or month_pct > 0) and accs)
        if need_wallet:
            for a in accs:
                cached = _wallet_cache.get(a.key)
                wallet = cached[0] if cached and now - cached[1] < 300 else None
                if wallet is None:
                    try:
                        from app.services.binance_trader import BinanceTrader
                        wallet = BinanceTrader(a.key).wallet_balance()["wallet"]
                        _wallet_cache[a.key] = (wallet, now)
                    except Exception as e:
                        logger.warning("熔断状态取钱包失败（%s）：%s", a.key, e)
                        if cached:
                            wallet = cached[0]  # 过期缓存兜底展示，总比未知好
                if wallet is None:
                    continue
                cap = _daily_loss_cap(wallet, pct=pct)
                caps.append(cap)
                bases.append(_capital_base(wallet))
                if breaker_on and day_pnl <= -cap:  # 多账号共用日账，任一账号口径触发即标熔断
                    any_active = True
        # 周/月分层状态（docs/13 #2）：基数多账号取最小 = 最保守，与闸门同源
        week_st: Optional[dict] = None
        month_st: Optional[dict] = None
        if accs and bases:
            base = min(bases)
            if week_pct > 0:
                week_pnl = sum(_closed_pnl_since(db, a.key, a.testnet, week_start) for a in accs)
                week_cap = base * week_pct / 100.0
                week_hit = week_pnl <= -week_cap
                week_acked = (cfg.weekly_breaker_ack_at is not None
                              and cfg.weekly_breaker_ack_at >= week_start)
                week_st = {"active": week_hit and not week_acked, "hit": week_hit,
                           "acked": week_acked,
                           "pnl": round(week_pnl, 2), "cap": round(week_cap, 2)}
            if month_pct > 0:
                month_pnl = sum(_closed_pnl_since(db, a.key, a.testnet, month_start) for a in accs)
                month_cap = base * month_pct / 100.0
                hit = month_pnl <= -month_cap
                acked = (cfg.monthly_breaker_ack_at is not None
                         and cfg.monthly_breaker_ack_at >= month_start)
                month_st = {"active": hit and not acked, "hit": hit, "acked": acked,
                            "pnl": round(month_pnl, 2), "cap": round(month_cap, 2)}
        out[net_key] = {
            "enabled": breaker_on and bool(accs),
            "active": (any_active if (breaker_on and accs and caps) else None),
            "day_pnl": round(day_pnl, 2),
            "cap": round(min(caps), 2) if caps else None,
            "week": week_st,
            "month": month_st,
        }
    return out


def _consecutive_losses_today(db: Session, account_key: Optional[str], testnet: bool) -> int:
    """本账号 UTC 当日从最近一笔平仓起连续亏损的笔数（连损降速软熔断用，docs/24 A3）。

    查本账号当日已平仓记录（status=CLOSED，closed_at 在今日 UTC），按 closed_at
    倒序数 realized_pnl<0 的连续笔数，中途一笔盈利即停（pnl=0/NULL 不算亏损、
    同样截断）。账号口径复用 _adopt_acc_cond（NULL 旧行按 testnet 环境回落，
    与 _daily_closed_pnl / 在跑单检查同源）。
    """
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    pnls = db.execute(
        select(TradeRecord.realized_pnl).where(
            _adopt_acc_cond(SimpleNamespace(account_key=account_key, testnet=testnet)),
            TradeRecord.status == "CLOSED",
            TradeRecord.closed_at >= day_start,
        ).order_by(TradeRecord.closed_at.desc())
    ).scalars().all()
    n = 0
    for pnl in pnls:
        if pnl is not None and float(pnl) < 0:
            n += 1
        else:
            break
    return n


def analysis_open_eligible(a: AIAnalysis, min_rec: float) -> bool:
    """开仓资格快速过滤（suggest / 方向合法 / 三价齐 / 评分达门槛）。

    与 try_open_for_analysis 顶部闸门同源（单一事实源，防两处漂移）；不含
    账号维度闸门（去重/在跑/熔断/降速/环境闸门，按账号实时判定）与 1h 时效。
    正式网自动开单关闭提醒（notify_live_auto_open_off）复用本判定回答
    「这条信号达标可开单吗」。
    """
    return bool(
        a is not None
        and a.trade_decision == "suggest"
        and a.direction in ("long", "short")
        and a.entry_price and a.stop_loss and a.take_profit_1
        and float(a.recommendation or 0) >= min_rec
    )


def try_open_for_analysis(
    db: Session, trader: BinanceTrader, analysis_id,
    force_market: bool = False,
) -> bool:
    """单条分析完成即开仓（2026-09-14：不再等 :42 统一批次，docs/06 §2）。

    闸门与批量开仓完全一致（§3）：上限 / 70% 余额 / 最小资金 / 同币无仓 /
    策略开关 / 方向价格复验 / 盈亏比复验 / EMA / 强平闸门。
    返回是否成功开仓；闸门未过即放弃（无批次重试，等该币下次信号重新分析）。
    force_market=True（2026-09-28 手动开单按钮）：市价即时入场，跳过
    入场偏离/待回踩时机闸门（自动成交路径默认 False，语义不变）。
    """
    a = db.get(AIAnalysis, UUID(str(analysis_id)))
    if a is None:
        return False
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        return False
    # 开仓资格快速过滤（suggest / 方向 / 三价 / 评分门槛 / 1 小时内）
    # 评分门槛（2026-10-02 上页面）：cfg.min_recommendation 覆盖时优先，NULL 回落 env
    min_rec = (
        float(cfg.min_recommendation) if cfg.min_recommendation is not None
        else float(settings.TRADING_MIN_RECOMMENDATION)
    )
    if not analysis_open_eligible(a, min_rec):
        return False
    if a.created_at and a.created_at < datetime.utcnow() - timedelta(hours=1):
        logger.info("即时开仓跳过 %s：分析已超过 1 小时", a.symbol)
        _mark_open_block(db, a, "分析已超过 1 小时（信号过期）")
        return False
    # 该分析在本账号已开过仓（docs/17 P1 扇出：同一 AI 分析允许多账号各开一单，
    # 按账号判断；NULL account_key 旧行按环境回落，与全局口径一致）
    acc_cond = or_(
        TradeRecord.account_key == trader.account_key,
        and_(TradeRecord.account_key.is_(None), TradeRecord.testnet == trader.testnet),
    )
    if db.execute(
        select(TradeRecord.id).where(TradeRecord.ai_analysis_id == a.id).where(acc_cond)
    ).first():
        return False  # 该账号已用此分析开过仓
    # 连损降速软熔断（docs/24 A3）：紧随评分门槛（TRADING_MIN_RECOMMENDATION）的
    # 动态提升——当日连损 ≥3 → 评分 ≥85，连损 2 → ≥75（绝对档位；提升档位低于
    # 现行门槛值时不生效，不会反向放宽）。连损趋势日降低开单频率观望，
    # 软熔断只提门槛不停摆（区别于当日亏损硬熔断）。0 连损/开关关闭不生效
    if settings.CONSECUTIVE_LOSS_DERATE:
        losses = _consecutive_losses_today(db, trader.account_key, trader.testnet)
        need_score = 85 if losses >= 3 else 75 if losses == 2 else 0
        if need_score and float(a.recommendation or 0) < need_score:
            logger.info(
                "即时开仓跳过 %s：连损 %d 笔降速，评分 %s 低于提升后门槛 %d",
                a.symbol, losses, a.recommendation, need_score,
            )
            _mark_open_block(
                db, a,
                f"连损{losses}笔降速：评分门槛提升至{need_score}"
                f"（当前评分 {float(a.recommendation or 0):.0f}）",
            )
            return False
    # BTC 急跌普跌防护（docs/24 A4② 最低成本版）：BTC 1h 近3根已收盘累计跌幅
    # 超 BTC_CRASH_PAUSE_PCT 时暂停山寨开单（BTCUSDT 本身照常，AI 分析照常跑）。
    # 判定带 60s 进程内缓存，查询失败降级放行（不因环境查询失败阻断开仓）
    if a.symbol != "BTCUSDT":
        try:
            from app.services.market_data import btc_crash_active, get_kline_client
            # 急跌阈值（2026-10-02 上页面）：cfg.btc_crash_pause_pct 覆盖时优先
            crash_pct = (
                float(cfg.btc_crash_pause_pct) if cfg.btc_crash_pause_pct is not None
                else None
            )
            if btc_crash_active(get_kline_client(), crash_pct=crash_pct):
                logger.info("即时开仓跳过 %s：BTC 1h 短线急跌，暂停山寨开单", a.symbol)
                _mark_open_block(db, a, "BTC 1h 短线急跌，暂停山寨开单（普跌防护）")
                return False
        except Exception as e:
            logger.debug("BTC 急跌闸门查询失败（放行）: %s", e)
    # 大盘单边环境闸门（N-15，docs/16 §8.3②，双向）：BTC+ETH 24h 涨跌幅同时
    # 超阈值视为单边行情——普跌拦山寨**开多**（10-07 普跌日 11 笔多单全灭）、
    # 普涨拦山寨**开空**（逼空日逆势空单与普跌日逆势多单同构）；顺势方向照常
    # （10-07 归因盈利路径均为普跌日空单）。与上方 1h 急跌闸门（突崩形态、
    # 全方向）互补：急跌抓「1 小时内突崩」，本闸门抓「单边走一整天」。
    # 判定带 60s 进程内缓存，查询失败降级放行
    if a.symbol != "BTCUSDT":
        try:
            from app.services.market_data import market_wide_regime, get_kline_client
            # 单边阈值：cfg.market_wide_pause_pct 覆盖时优先（NULL 回落 env）
            pause_pct = (
                float(cfg.market_wide_pause_pct)
                if cfg.market_wide_pause_pct is not None
                else None
            )
            regime = market_wide_regime(get_kline_client(), pause_pct=pause_pct)
            blocked_side = {"down": "long", "up": "short"}.get(regime)
            if blocked_side is not None and a.direction == blocked_side:
                env_label = (
                    "大盘普跌（BTC/ETH 24h 双跌）" if regime == "down"
                    else "大盘普涨（BTC/ETH 24h 双涨）"
                )
                side_label = "开多" if blocked_side == "long" else "开空"
                logger.info("即时开仓跳过 %s：%s，暂停山寨%s", a.symbol, env_label, side_label)
                _mark_open_block(db, a, f"{env_label}，暂停山寨{side_label}（环境闸门）")
                return False
        except Exception as e:
            logger.debug("大盘单边闸门查询失败（放行）: %s", e)
    # 待回踩单不再在此跳过（2026-09-18）：回踩距离 ≤ pullback_market_pct 容差
    # 时市价入场、超容差维持不下单——判定移入 _attempt_open 偏离闸门
    positions = trader.positions()
    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    # 周回撤减速（2026-10-04 docs/13 #2；2026-10-07 改口径：单笔预算减半 →
    # 在跑上限/同向上限减半）：UTC 周已平仓净亏达基数×阈值且未人工确认时，
    # 本请求的单数上限与同向上限减半（floor，至少 1）。软减速不拦开仓；
    # 确认在当前 UTC 周内 = 本周放行，下周一 0 点自动重新武装。
    # （wallet 提前取：减半后的上限在下方单数/同向闸门即需生效）
    now_utc = datetime.utcnow()
    week_pct = (
        float(cfg.weekly_dd_stop_pct) if cfg.weekly_dd_stop_pct is not None
        else float(settings.WEEKLY_DD_STOP_PCT)
    )
    week_derate = False
    if week_pct > 0:
        week_start = (now_utc - timedelta(days=now_utc.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)  # UTC 周一 0 点
        week_pnl = _closed_pnl_since(db, trader.account_key, trader.testnet, week_start)
        week_cap = _capital_base(wallet) * week_pct / 100.0
        if week_pnl <= -week_cap:
            # 周减速人工复盘确认（2026-10-07，对称月熔断）：确认时刻在当前 UTC 周
            # 内 = 本周已复盘放行（上限恢复常规），下周一自动重新武装
            acked = (cfg.weekly_breaker_ack_at is not None
                     and cfg.weekly_breaker_ack_at >= week_start)
            if not acked:
                week_derate = True
                logger.warning(
                    "周度回撤减速 %s（%s）：UTC 周已平仓净亏 %.2f ≤ -%.0f 阈值"
                    "（风险基数 × %.0f%%）——本周剩余时间在跑上限/同向上限减半"
                    "（软减速不拦开仓；「确认复盘」可恢复，周一 0 点自动恢复）",
                    a.symbol, trader.account_key, week_pnl, week_cap, week_pct,
                )
    # 单数上限（0 = 不限单数，仅靠余额风控兜底）；已开始移动止损的单子
    # （TP1_HIT 止损已移保本 / TP2_HIT 仅剩跟进仓）风险已锁定，不占名额（2026-09-25）
    moved_syms = {
        sym for (sym,) in db.execute(
            select(TradeRecord.symbol).where(
                TradeRecord.status.in_(("TP1_HIT", "TP2_HIT")),
                TradeRecord.symbol.in_(list(positions.keys())),
                _adopt_acc_cond(trader),
            )
        ).all()
    }
    slot_used = len(positions) - len(moved_syms)
    if cfg.max_open_trades > 0:
        slot_limit = (
            max(1, cfg.max_open_trades // 2) if week_derate else cfg.max_open_trades
        )
        if slot_used >= slot_limit:
            suffix = "（周减速减半）" if week_derate else ""
            logger.info("即时开仓跳过 %s：在跑单子已达上限 %d%s",
                        a.symbol, slot_limit, suffix)
            _mark_open_block(db, a, f"在跑单子已达上限 {slot_limit}{suffix}")
            return False
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
    # 组合热度上限 + 同向持仓上限（docs/24 A3'，紧随在跑单上限闸门）：在跑单按
    # 全状态集合（OPENED/TP1_HIT/TP2_HIT）计——TP1_HIT/TP2_HIT 止损已上移不占
    # 单数名额，但剩余仓的风险敞口与方向集中度仍然真实存在
    # （2026-10-02 上页面：heat_cap/same_cap/risk_pct 均按 cfg 覆盖优先、NULL 回落 env）
    heat_cap = (
        float(cfg.portfolio_heat_cap) if cfg.portfolio_heat_cap is not None
        else float(settings.PORTFOLIO_HEAT_CAP)
    )
    same_cap = (
        int(cfg.same_direction_cap) if cfg.same_direction_cap is not None
        else int(settings.SAME_DIRECTION_CAP)
    )
    if week_derate and same_cap > 0:
        same_cap = max(1, same_cap // 2)  # 周减速：同向上限减半（至少 1）
    near_factor = (
        float(cfg.near_anchor_factor) if cfg.near_anchor_factor is not None
        else float(settings.NEAR_ANCHOR_RISK_FACTOR)
    )
    # 本单 β（docs/13 #1）：对 BTC 的日收益回归 β，查询失败/缺失按 1.0（保守）。
    # beta_raw（None-able）专供落库快照——NULL=不可得（与列注释语义一致，事后
    # 分析可区分「实测 β=1.0」与「不可得」）；闸门加权用回落后的 beta_new
    # （24h 进程缓存，逐笔调用不重拉 K 线；BTCUSDT 恒 1.0）
    from app.services.market_data import get_beta, get_kline_client
    try:
        beta_raw = get_beta(get_kline_client(), a.symbol)
    except Exception as e:
        logger.debug("本单 β 查询失败（按 1.0）: %s", e)
        beta_raw = None
    beta_new = beta_raw if beta_raw is not None else 1.0
    if heat_cap > 0 or same_cap > 0:
        running = db.execute(
            select(TradeRecord.risk_amount, TradeRecord.direction,
                   TradeRecord.symbol, TradeRecord.beta).where(
                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
                _adopt_acc_cond(trader),
            )
        ).all()
        # 本单预估止损金额：与 _attempt_open 同源的固定亏损预算口径（金额模式取
        # 固定值 / 比例模式取基数×比例，含近锚折价）；实际风险在下单时按数量
        # 取整只会更低，闸门用预算值预检不放大放行
        if (cfg.risk_budget_mode or "pct") == "fixed":
            est_risk = float(
                cfg.risk_fixed_amount if cfg.risk_fixed_amount is not None else 3.0
            )
        else:
            _rp = (
                float(cfg.risk_pct) if cfg.risk_pct is not None
                else float(settings.TRADING_RISK_PCT)
            )
            est_risk = _capital_base(wallet) * _rp / 100
        est_risk = _near_discounted_budget(est_risk, a.stop_anchor, factor=near_factor)
        # ① 热度（2026-10-04 β 加权，docs/13 #1）：在跑单止损合计与本单预估均按
        #    β 加权——山寨与 BTC 相关性 0.7~0.9，同向单实际是同一 BTC 敞口的
        #    多份拷贝，「单笔合规、组合隐性集中」。β 缺失（旧行/查询失败）按 1.0
        #    = 原口径。双口径日志（docs/13 验证要求）：加权值与原值差进日志，
        #    影子观察期比对误拦率，稳定后原口径日志移除。0=关闭
        if heat_cap > 0:
            heat_used = sum(
                float(r or 0) * (float(b) if b is not None else 1.0)
                for r, _d, _s, b in running
            )
            heat_used_orig = sum(float(r or 0) for r, _d, _s, _b in running)
            est_risk_eff = est_risk * beta_new
            if heat_used + est_risk_eff > wallet * heat_cap:
                logger.info(
                    "即时开仓跳过 %s：组合热度超限（β加权：在跑 %.2f + 本单 %.2f×β%.2f"
                    " > 钱包 %.2f × %.0f%%；原口径：在跑 %.2f + 本单 %.2f）",
                    a.symbol, heat_used, est_risk, beta_new, wallet,
                    heat_cap * 100, heat_used_orig, est_risk,
                )
                _mark_open_block(
                    db, a,
                    f"组合热度超限（β加权）：在跑 {heat_used:.2f} + 本单 "
                    f"{est_risk_eff:.2f} > 钱包 {wallet:.2f} × {heat_cap * 100:.0f}%",
                )
                return False
        # ② 同向：同 direction 在跑单数 ≥ 同向上限 → 拦截。
        #    全市场同涨同跌（趋势同震）时同向单几乎同损——方向集中度上限（0=不限）
        if same_cap > 0:
            same_n = sum(1 for _r, d, _s, _b in running if d == a.direction)
            if same_n >= same_cap:
                logger.info(
                    "即时开仓跳过 %s：同向（%s）在跑 %d 单已达上限 %d",
                    a.symbol, a.direction, same_n, same_cap,
                )
                _mark_open_block(
                    db, a,
                    f"同向持仓达上限：{a.direction} 方向在跑 {same_n} 单 ≥ {same_cap}"
                    + ("（周减速减半）" if week_derate else ""),
                )
                return False
    # 当日亏损熔断（2026-09-27 正式网复盘）：UTC 日本账号已平仓净亏 ≤ -阈值
    # （风险基数 × DAILY_LOSS_STOP_PCT，默认 9% ≈ 3 笔满损，随账户金额缩放）
    # 停开仓——09-26 全天 13 笔全灭仍持续开单，趋势日的 trail 赢单无法覆盖
    # 震荡日的连损。只停开仓，AI 分析/巡检照常。
    # 2026-09-30 用户拍板开关化：DAILY_LOSS_STOP_ENABLED 默认 True=保持现行为，
    # False=关闭；PCT 置 0 亦关闭（阈值语义）
    # 2026-10-02 上页面：开关与阈值均 cfg 覆盖优先（三态开关列，NULL 回落 env）
    loss_enabled = (
        bool(cfg.daily_loss_stop_enabled) if cfg.daily_loss_stop_enabled is not None
        else settings.DAILY_LOSS_STOP_ENABLED
    )
    loss_pct = (
        float(cfg.daily_loss_stop_pct) if cfg.daily_loss_stop_pct is not None
        else float(settings.DAILY_LOSS_STOP_PCT)
    )
    if loss_enabled and loss_pct > 0:
        day_pnl = _daily_closed_pnl(db, trader.account_key, trader.testnet)
        loss_cap = _daily_loss_cap(wallet, pct=loss_pct)
        if day_pnl <= -loss_cap:
            # WARNING 级：熔断拦截是关键风控事件，须进系统日志页——
            # 否则开单静默被拦，用户只看到测试网开了、正式网没动静，无从归因
            #（2026-10-04 SOL/SUI 案例：live 被 -19.11 熔断拦下，系统日志无痕）
            logger.warning(
                "风控熔断拦截开仓 %s（%s）：当日已平仓净亏 %.2f ≤ -%.0f 阈值"
                "（UTC 日内合计，UTC 0 点自动恢复；分析/巡检不受影响）",
                a.symbol, trader.account_key, day_pnl, loss_cap,
            )
            _mark_open_block(
                db, a,
                f"当日亏损熔断：已平仓净亏 {day_pnl:.2f} USDT "
                f"达 -{loss_cap:.0f} 阈值（风险基数 {_capital_base(wallet):.0f} "
                f"× {loss_pct:.0f}%）")
            return False
    # 月回撤熔断（2026-10-04，docs/13 #2）：日级之上的总闸——连亏跨日的阴跌
    # 行情日熔断天天复位天天开（日亏 5% 连 4 天 = -18.5%），缺少总闸。
    # 月 ≥ 阈值 → 停开新仓，人工复盘确认（monthly_breaker_ack_at 写确认时刻，
    # 确认在当前 UTC 月内 = 放行至月底，跨月自动重新武装）。
    # 周减速（≥ 阈值 → 在跑上限/同向上限减半，软减速不拦开仓）已在单数/同向
    # 闸门前单独判定。只收紧亏损侧：盈利周/月零影响。
    # 阈值 cfg ?? env（nullable 上页面，docs/12 §10 惯例），0=关闭该层
    month_pct = (
        float(cfg.monthly_dd_stop_pct) if cfg.monthly_dd_stop_pct is not None
        else float(settings.MONTHLY_DD_STOP_PCT)
    )
    month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_pct > 0:
        month_pnl = _closed_pnl_since(db, trader.account_key, trader.testnet, month_start)
        month_cap = _capital_base(wallet) * month_pct / 100.0
        if month_pnl <= -month_cap:
            acked = (cfg.monthly_breaker_ack_at is not None
                     and cfg.monthly_breaker_ack_at >= month_start)
            if not acked:
                # WARNING 级进系统日志页（同日熔断口径：拦截不可无痕）
                logger.warning(
                    "月度回撤熔断拦截开仓 %s（%s）：UTC 月已平仓净亏 %.2f ≤ -%.0f 阈值"
                    "（风险基数 × %.0f%%）——停开新仓，人工复盘确认后恢复"
                    "（顶部熔断卡片「确认复盘」按钮）",
                    a.symbol, trader.account_key, month_pnl, month_cap, month_pct,
                )
                _mark_open_block(
                    db, a,
                    f"月度回撤熔断：本月已平仓净亏 {month_pnl:.2f} USDT "
                    f"达 -{month_cap:.0f} 阈值（基数 × {month_pct:.0f}%）——"
                    "人工复盘确认后恢复")
                return False
    # 即将进入闸门序列：清掉沿用/上一轮尝试残留的拒开原因，本次成败由闸门重新裁定
    if a.open_block_reason:
        try:
            a.open_block_reason = None
            db.commit()
        except Exception:
            db.rollback()
    return _attempt_open(db, trader, cfg, a, wallet, positions,
                         force_market=force_market, beta=beta_raw)


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
                      interval: str, open_ids: set[int],
                      atr_k: Optional[float] = None) -> None:
    """按跟进规则移动止损（只收紧不放松）；新 SL 会立即触发时跳过本轮（旧 SL 仍有效保护：
    仓位存在即旧 SL 未触发），避免交易所 -2021 拒单导致整轮结算回滚重试。

    两腿取更紧者（docs/13 #5）：摆动腿（最近3根已收盘K线极值）+ ATR 腿
    （Chandelier，atr_k None/0 关闭）；聚合后统一 TP1 保本夹紧（与
    _trailing_stop 内同语义，幂等）。
    """
    cur = float(rec.stop_loss or 0)
    if cur <= 0:
        return
    klines = ExchangePool().get_klines(rec.symbol, interval, 20)
    candidates: list[float] = []
    swing = _trailing_stop(rec, klines)
    if swing is not None:
        candidates.append(swing)
    k = float(settings.ATR_TRAIL_K) if atr_k is None else float(atr_k)
    if k > 0:
        chand = _chandelier_sl(rec, trader, k)
        if chand is not None:
            candidates.append(chand)
    if not candidates:
        return
    # 只收紧铁律：聚合必须包含现 SL——ATR 腿可能松于现 SL（如回调后高点回落），
    # 漏掉 cur 会把 SL 反向放松（摆动腿自带 max(cur,·) 兜底，ATR 腿没有）
    new_sl = (max([cur, *candidates]) if rec.direction == "long"
              else min([cur, *candidates]))
    if rec.status == "TP2_HIT" and rec.tp1:
        new_sl = (max(new_sl, float(rec.tp1)) if rec.direction == "long"
                  else min(new_sl, float(rec.tp1)))
    new_sl = trader.round_price(rec.symbol, new_sl)
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


def _chandelier_sl(rec: TradeRecord, trader: BinanceTrader, k: float) -> Optional[float]:
    """ATR 跟进腿（2026-10-04，docs/13 #5 Chandelier Exit）：
    多单 = 持仓期最高价 − k×ATR(22,D1)，空单 = 持仓期最低价 + k×ATR22。

    持仓期极值 = opened_at 起已收盘 1h K 的高/低极值（零 schema 改动，随 K 线
    重算口径可回放）。k 默认 3（海龟系标准）；只收紧语义下本腿与摆动腿取更紧者，
    最坏 = 恰好等于现行摆动跟进（下界保证）。
    任一数据不可得（持仓不足 1h 无已收盘根 / ATR 未成形 / 拉取失败）→ None：
    调用方聚合时 None 即跳过，退化为现行摆动跟进。"""
    try:
        from app.services.strategy.technical import atr_series

        opened = rec.opened_at
        if opened is None:
            return None
        opened_ms = opened.replace(tzinfo=timezone.utc).timestamp() * 1000
        now_utc = datetime.utcnow()
        hours = max(1, int((now_utc - opened).total_seconds() // 3600) + 1)
        # 持仓期 1h K（上限 60 天，trail 单实际持仓通常 1-3 天）
        kl = ExchangePool().get_klines(rec.symbol, "1h", min(hours + 1, 24 * 60))
        closed = [b for b in (kl[:-1] if len(kl) >= 2 else kl)
                  if float(b[0]) >= opened_ms]
        if not closed:
            return None
        d1 = ExchangePool().get_klines(rec.symbol, "1d", 30)
        atr = atr_series(d1, period=22)
        if atr is None:
            return None
        a = float(atr[-1])
        if not math.isfinite(a) or a <= 0:
            return None
        if rec.direction == "long":
            return max(float(b[2]) for b in closed) - k * a
        return min(float(b[3]) for b in closed) + k * a
    except Exception as e:
        logger.debug("ATR 跟进腿 %s 不可得（退化摆动跟进）: %s", rec.symbol, e)
        return None


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


def _breakeven_stop_price(rec: TradeRecord, fee_rate: Optional[float] = None) -> float:
    """保本止损价（docs/24 A1''）：成本价 ± 往返费用缓冲。

    窄止损下费用/R 被名义杠杆放大（taker 开+平两腿各收一次费率），止损贴
    成本价的单被扫时纯成本价保本实际仍亏往返手续费——多单外推到成本价
    上方、空单下方 buffer = entry × 2 × 费率，止损触发时价差恰好覆盖往返费用。
    _move_sl_breakeven 与 _derive_exit_reason 共用此口径。
    fee_rate（2026-10-02 上页面）：system_config.taker_fee_rate 覆盖时传入；
    None 回落 env 默认。
    """
    entry = float(rec.entry_price or 0)
    _fee = settings.TAKER_FEE_RATE if fee_rate is None else float(fee_rate)
    buffer = entry * 2 * _fee
    return entry + buffer if rec.direction == "long" else entry - buffer


def _move_sl_breakeven(db: Session, trader: BinanceTrader, rec: TradeRecord,
                       open_ids: set[int], fee_rate: Optional[float] = None) -> bool:
    """TP1 成交后保本止损：SL 移到「成本价 ± 往返费用缓冲」（多单
    SL=max(现SL,保本价)，空单反之，只收紧不放松）。

    docs/24 A1''：窄止损下费用/R 被名义杠杆放大，纯成本价保本实际仍亏往返
    手续费（taker 开+平各收一次），保本价外推一个往返费用缓冲。
    fee_rate（2026-10-02 上页面）：system_config.taker_fee_rate 覆盖时传入。

    幂等：SL 已不差于保本价（或取整后相同）时不动作；价格已回到保本价另一侧
    （挂上即立即触发）时跳过本轮保留原 SL，下轮巡检再试。返回是否实际移动。
    """
    entry = float(rec.entry_price or 0)
    cur = float(rec.stop_loss or 0)
    if entry <= 0 or cur <= 0:
        return False
    be = _breakeven_stop_price(rec, fee_rate)
    new_sl = max(cur, be) if rec.direction == "long" else min(cur, be)
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
    logger.info("保本止损 %s：%s → %s（成本价 %s ± 往返费缓冲）",
                rec.symbol, cur, new_sl, entry)
    return True


def _derive_exit_reason(rec: TradeRecord, fee_rate: Optional[float] = None) -> str:
    # 纯跟踪单（docs/19 §3.4）：状态恒 OPENED（无 TP 档），按武装旗标推断——
    # SL 已收紧越过保本价 → trail_sl；仍贴着保本价 → breakeven_sl；未武装 → sl
    if (rec.raw or {}).get("tp_mode") == "trail" and rec.status == "OPENED":
        if not (rec.raw or {}).get("trail_armed"):
            return "sl"
        entry, sl = float(rec.entry_price or 0), float(rec.stop_loss or 0)
        if entry > 0 and sl > 0:
            eps = entry * 1e-6
            be = _breakeven_stop_price(rec, fee_rate)
            if rec.direction == "long":
                if sl > be + eps:
                    return "trail_sl"
                if sl >= entry - eps:
                    return "breakeven_sl"
            else:
                if sl < be - eps:
                    return "trail_sl"
                if sl <= entry + eps:
                    return "breakeven_sl"
        return "breakeven_sl"
    if rec.status == "TP2_HIT":
        return "trail_sl"
    if rec.status == "TP1_HIT":
        # TP1 后止损已移至「成本价 ± 往返费缓冲」保本（docs/24 A1''，与
        # _breakeven_stop_price 同口径）；无 TP2 的单随后启用跟进止损（只收紧）——
        # SL 已被收紧越过保本价 → TP1后跟踪止损出场；仍贴着保本价 → 保本出场
        entry, sl = float(rec.entry_price or 0), float(rec.stop_loss or 0)
        if entry > 0 and sl > 0:
            eps = entry * 1e-6
            be = _breakeven_stop_price(rec, fee_rate)
            if rec.direction == "long":
                if sl > be + eps:
                    return "tp1_trail"
                if sl >= entry - eps:
                    return "breakeven_sl"
            else:
                if sl < be - eps:
                    return "tp1_trail"
                if sl <= entry + eps:
                    return "breakeven_sl"
        return "tp1_then_sl"
    return "sl"


def _trail_mode_patrol(db: Session, trader: BinanceTrader, rec: TradeRecord,
                       interval: str, open_ids: set[int], fee_rate: Optional[float],
                       atr_k: Optional[float]) -> None:
    """纯跟踪模式巡检（docs/19 §3.4 V1，tp_mode=trail 的单不挂止盈档）：

    未武装：全程盈亏峰值（peak_total_pnl，USDT）≥ 1×ATR(22,1d)×数量 → 武装保本
    （SL 移至成本价±往返费缓冲，与 TP1 后保本同口径）；entry_atr 缺失（开仓时
    快照失败/旧行）退化 1×risk_amount（1R）武装线。
    已武装：先查 EMA21 减半腿（武装后 1h 收盘破 EMA21 → 市价减半仓，一次性），
    再每轮跟进止损（与 TP2_HIT 同机制，摆动腿+ATR 腿只收紧不放松）。
    幂等：armed 旗标在 raw.trail_armed，武装动作失败（交易所异常）不落旗标下轮重试。
    """
    raw = rec.raw or {}
    qty = float(rec.qty or 0)
    if qty <= 0:
        return
    if not raw.get("trail_armed"):
        peak = float(rec.peak_total_pnl or 0)
        atr = float(raw.get("entry_atr") or 0)
        threshold = atr * qty if atr > 0 else float(rec.risk_amount or 0)
        if threshold <= 0 or peak < threshold:
            return
        _move_sl_breakeven(db, trader, rec, open_ids, fee_rate)
        # 武装判定以 SL 现值为准：已不差于保本价（本轮移成/此前已到位）才落旗标；
        # 仍差（挂上即立即触发等，_move_sl_breakeven 保留原 SL）→ 本轮不跟进，
        # 旗标不落，下轮巡检重试
        entry = float(rec.entry_price or 0)
        be = _breakeven_stop_price(rec, fee_rate)
        sl_now = float(rec.stop_loss or 0)
        eps = entry * 1e-6
        armed = (sl_now >= be - eps if rec.direction == "long"
                 else sl_now <= be + eps)
        if not armed:
            logger.info("纯跟踪武装 %s 暂缓：SL %s 未达保本价 %s（下轮重试）",
                        rec.symbol, sl_now, be)
            return
        rec.raw = {**raw, "trail_armed": True}
        logger.info("纯跟踪武装保本 %s：峰值 %.4f 达武装线 %.4f，SL=%s",
                    rec.symbol, peak, threshold, sl_now)
    if (rec.raw or {}).get("trail_armed"):
        # 武装（=已开始移动止损）后先查减半腿：破 EMA21 落袋一半再继续跟进
        # （重读 rec.raw：本轮刚武装时局部 raw 是旧 dict 无旗标）
        _ema21_half_cut(db, trader, rec)
    _move_sl_trailing(db, trader, rec, interval, open_ids, atr_k=atr_k)


def _ema21_half_cut(db: Session, trader: BinanceTrader, rec: TradeRecord) -> None:
    """武装后 1h 收盘破 EMA21 → 市价减半仓（2026-10-10 拍板，trail 方案减仓腿）。

    trail 单武装（已进入移动止损阶段）后，每小时巡检检查最近 1h 已收盘 K 线
    （剔尾，与 structure_1h 同口径）：多单收盘 < EMA21(1h) / 空单收盘 > EMA21
    → 市价 reduceOnly 减掉一半仓位落袋防趋势反噬；剩余仓位继续 ATR 跟进
    （调用方后续 _move_sl_trailing，SL 为 closePosition 全平单无需调整）。

    一次性：raw.ema21_cut 旗标防重复，减一次后纯靠 trailing；减半后 rec.qty
    同步为剩余量（展示/诊断一致性；结算按交易所实际持仓 amt 判断归零、pnl 从
    income 实录拉取，不依赖 rec.qty）。
    数据/约束不可得（K 线不足、EMA 未成形、无持仓、减半量或剩余量低于交易所
    最小规则）→ 跳过本轮不落旗标，下轮巡检重试；交易所下单异常仅告警不中断
    巡检（下轮重试）。
    双刀竞态防护：市价单已成交但事务回滚/响应超时（旗标没落库）时，交易所
    amt 已 ≈ rec.qty 的一半——持仓已明显小于记录数量（<0.7 倍）视为上一刀
    已实际成交（或用户外部部分平仓），只补旗标与 qty 同步、不再下第二刀。
    """
    raw = rec.raw or {}
    if raw.get("ema21_cut"):
        return
    try:
        pos = trader.positions().get(rec.symbol)
        amt = abs(float((pos or {}).get("amt") or 0)) if pos else 0.0
        rec_qty = float(rec.qty or 0)
        if amt <= 0:
            return
        if rec_qty > 0 and amt < rec_qty * 0.7:
            # 上一刀已成交但旗标未落库（commit 回滚/下单响应超时），或用户已在
            # 交易所手动部分平仓——视为已减过，同步状态即可，绝不再下第二刀
            rec.qty = amt
            rec.raw = {**raw, "ema21_cut": True}
            _add_event(db, rec, "CUT_EMA21", {
                "close": None, "ema21": None, "cut_qty": rec_qty - amt,
                "remain_qty": amt, "reason": "already_reduced",
            })
            logger.info("EMA21 减半仓 %s：交易所持仓 %s 已小于记录 %s，视为已减过（补旗标）",
                        rec.symbol, amt, rec_qty)
            return
        kl = ExchangePool().get_klines(rec.symbol, "1h", 60)
        closed = kl[:-1] if len(kl) >= 2 else kl
        if len(closed) < 30:
            return
        c = np.array([float(k[4]) for k in closed], dtype=float)
        e21 = ema_series(c, 21)
        if e21 is None or not math.isfinite(float(e21[-1])):
            return
        close_now = float(c[-1])
        ema_now = float(e21[-1])
        breached = (close_now < ema_now if rec.direction == "long"
                    else close_now > ema_now)
        if not breached:
            return
        f = trader.filters(rec.symbol)
        cut_qty = trader.round_qty(rec.symbol, amt / 2)
        remain = amt - cut_qty
        # 减半量或剩余量低于交易所最小规则 → 不切（市价单会被拒/留下不可平尾巴），
        # 留给 SL 全平
        if cut_qty < f["min_qty"] or remain < f["min_qty"]:
            logger.info("EMA21 减半仓 %s 跳过：cut=%s remain=%s 低于 min_qty=%s",
                        rec.symbol, cut_qty, remain, f["min_qty"])
            return
        trader.market_order(rec.symbol, _close_side(rec.direction), cut_qty,
                            reduce_only=True)
        rec.qty = remain
        rec.raw = {**raw, "ema21_cut": True}
        # 减半腿的已实现盈亏落账（与 TP1 成交同口径：income 实录，含手续费）——
        # 不记则运行中 realized_pnl 恒 0，全程盈亏/峰值回撤（诊断触发参照）少算已落袋一半
        _record_realized(db, trader, rec)
        _add_event(db, rec, "CUT_EMA21", {
            "close": close_now, "ema21": ema_now,
            "cut_qty": cut_qty, "remain_qty": remain,
        })
        logger.info("EMA21 减半仓 %s：收盘 %s 破 EMA21 %s，市价减 %s 剩 %s",
                    rec.symbol, close_now, ema_now, cut_qty, remain)
    except Exception as e:
        logger.warning("EMA21 减半仓 %s 本轮未成（不落旗标下轮重试）: %s",
                       rec.symbol, e)


def _zero_pos_exit_reason(rec: TradeRecord, tp1_gone: bool,
                          fee_rate: Optional[float] = None) -> str:
    """仓位归零时的出场原因：OPENED 状态下 TP1 单已消失，说明 TP1 与 SL 在同一巡检间隔内
    先后成交（如 75% TP1 后回踩触发 SL）→ tp1_then_sl；其余按状态推断"""
    if rec.status == "OPENED" and tp1_gone:
        return "tp1_then_sl"
    return _derive_exit_reason(rec, fee_rate)


def _detect_external_close(rt: BinanceTrader, rec: TradeRecord) -> Optional[dict]:
    """识别外部平仓（交易所网页/App 手动平仓，非本系统挂单链成交）。

    背景（2026-10-02 GTCUSDT 案例）：用户在币安网页一键平仓（IOC 限价单）→
    巡检发现仓位归零，bookkeeping 推断把出场误标为 tp1_trail/sl——复盘图
    止损线与实际出场对不上，且污染止损滑点统计。

    识别原理：SL/TP 为 algo 条件单，触发后的市价子单在 allOrders 里不带
    归属字段，但 /fapi/v1/algoOrder 的 actualOrderId 精确回填触发子单号
    （TP1/SL 两条链均实测验证）。最后一批平仓侧成交（按时间序，同一
    orderId 聚合）的 orderId 不在系统单链（入场单 + SL/TP algo 及其触发
    子单）内 → 外部平仓。

    返回 EXTERNAL_CLOSE 事件 detail；系统单成交 / 链路不完整（algo 查询
    失败——子单归属无从确认，误判风险大于收益）/ 无平仓侧成交 时返回
    None（弃权，维持 bookkeeping 推断，不因识别失败阻塞结算）。
    """
    try:
        raw = rec.raw or {}
        algo_keys = ("sl_order_id", "tp1_order_id", "tp2_order_id")
        system_ids: set[int] = set()
        for key in ("entry_order_id",) + algo_keys:
            if raw.get(key):
                system_ids.add(int(raw[key]))
        # algo 触发子单号回填（actualOrderId）；任一查询失败 → 链不完整，弃权
        for key in algo_keys:
            algo_id = raw.get(key)
            if not algo_id:
                continue
            try:
                detail = rt.algo_order_detail(rec.symbol, int(algo_id))
            except Exception:
                logger.info("外部平仓识别弃权 %s：algo 单 %s 查询失败", rec.symbol, algo_id)
                return None
            sub = str(detail.get("actualOrderId") or "").strip()
            if sub:
                system_ids.add(int(sub))

        close_side = "SELL" if rec.direction == "long" else "BUY"
        since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
        rows = rt._req("GET", "/fapi/v1/userTrades", {
            "symbol": rec.symbol, "startTime": since_ms, "limit": 1000,
        })
        closes = sorted(
            (r for r in rows if r.get("side") == close_side),
            key=lambda r: int(r.get("time") or 0),
        )
        if not closes:
            return None
        last_id = int(closes[-1]["orderId"])
        if last_id in system_ids:
            return None  # 最后一批平仓是系统单链成交 → 维持原推断
        group = [r for r in closes if int(r.get("orderId") or 0) == last_id]
        qty = sum(float(r.get("qty") or 0) for r in group)
        if qty <= 0:
            return None
        vwap = sum(float(r.get("qty") or 0) * float(r.get("price") or 0) for r in group) / qty
        filled_at_ms = min(int(r.get("time") or 0) for r in group)
        # 下单渠道特征（IOC 限价=网页一键平仓；MARKET=App 市价键）——仅备注不参与判定
        try:
            od = rt.order(rec.symbol, last_id)
            order_type, tif = od.get("type"), od.get("timeInForce")
        except Exception:
            order_type, tif = None, None
        return {
            "order_id": last_id, "order_type": order_type, "time_in_force": tif,
            "exit_price": round(vwap, 8), "qty": qty,
            "filled_at": datetime.fromtimestamp(filled_at_ms / 1000, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.warning("外部平仓识别失败 %s: %s", rec.symbol, e)
        return None


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
    # 运行中收益率为全程口径：(已实现 + 浮动) / risk_amount（docs/16）
    rec.pnl_pct = round(
        (pnl + float(rec.unrealized_pnl or 0)) / float(rec.risk_amount or 1) * 100, 2
    ) if rec.risk_amount else None
    return pnl


def close_market_now(db: Session, trader: BinanceTrader, rec: TradeRecord) -> dict:
    """市价平仓（飞书卡片按钮，2026-09-21）：reduceOnly 市价全平 → 清理挂单 →
    立即按 income 口径结算落终态（复用巡检零仓位的费用拆分/事件口径）。

    执行顺序（2026-09-25 调整为先平仓后撤单）：SL/TP 挂单在市价单执行期间
    始终在场——下单失败时止损保护原封未动（无裸奔窗口）；reduceOnly 互斥
    保证不双重平仓（SL 在途先触发则本单被拒 -2022，复查持仓归零视为成功）。
    挂单清理三道网：币安仓位归零自动撤 reduceOnly/closePosition 挂单 +
    成交后显式 cancel_all_algo 兜底 + 巡检孤儿单清扫。

    调用方须已获取开仓锁、确认 rec 在跑状态（本函数内不再校验）。
    返回 {qty, pnl, pnl_pct}；异常向上抛由调用方回滚。
    """
    import time
    pos = trader.positions().get(rec.symbol)
    amt = abs(float((pos or {}).get("amt") or 0)) if pos else 0.0
    close_side = _close_side(rec.direction)
    close_qty = trader.round_qty(rec.symbol, amt if amt > 0 else float(rec.qty or 0))
    if close_qty <= 0:
        raise ValueError("无仓位可平（交易所持仓为空且记录无数量）")

    def _pos_amt() -> float:
        cur = trader.positions().get(rec.symbol)
        return abs(float((cur or {}).get("amt") or 0)) if cur else 0.0

    if amt > 0:
        try:
            trader.market_order(rec.symbol, close_side, close_qty, reduce_only=True)
        except Exception:
            # -2022 边界：SL/TP 在途先触发平掉仓位，reduceOnly 单被交易所拒绝；
            # 复查持仓归零 → 视为已被挂单平掉，走正常结算；仍有持仓 → 重试一次
            if _pos_amt() > 0:
                trader.market_order(rec.symbol, close_side,
                                    trader.round_qty(rec.symbol, _pos_amt()),
                                    reduce_only=True)
        # 市价单通常即时成交；最多重试一次兜底（成交量未反映到持仓时再补一单）
        for _ in range(2):
            cur_amt = _pos_amt()
            if cur_amt < close_qty * 0.05:
                break
            try:
                trader.market_order(rec.symbol, close_side, trader.round_qty(
                    rec.symbol, cur_amt), reduce_only=True)
            except Exception:
                break
            time.sleep(1)
        # 平仓成交后再撤挂单（仓位归零时交易所通常已自动撤销，此处兜底）
        try:
            trader.cancel_all_algo(rec.symbol)
        except Exception:
            logger.warning("平仓后撤销挂单失败 %s（交易所应已自动撤，巡检将清扫孤儿单）", rec.symbol)
    time.sleep(1)  # income 流水落库延迟（REALIZED_PNL/COMMISSION 通常 <1s）
    # 结算：与巡检零仓位分支同口径（费用拆分/全程净额）
    since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
    rows = trader.income_rows(rec.symbol, since_ms)
    rec.fee_total = round(sum(float(r.get("income") or 0) for r in rows
                              if r.get("incomeType") == "COMMISSION"), 6)
    rec.funding_total = round(sum(float(r.get("income") or 0) for r in rows
                                  if r.get("incomeType") == "FUNDING_FEE"), 6)
    rec.unrealized_pnl = 0  # 终态无浮动（docs/16）
    pnl = round(trader.sum_income(rows, trader.INCOME_KEEP), 6)
    rec.realized_pnl = pnl
    rec.pnl_pct = round(pnl / float(rec.risk_amount or 1) * 100, 2) if rec.risk_amount else None
    rec.status = "CLOSED"
    rec.closed_at = datetime.utcnow()
    rec.exit_reason = "manual"
    _add_event(db, rec, "MANUAL_CLOSE", {
        "qty": float(close_qty), "realized_pnl": pnl, "pnl_pct": rec.pnl_pct,
        "exit_reason": "manual", "fee_total": rec.fee_total,
        "funding_total": rec.funding_total,
    })
    db.commit()
    logger.info("市价平仓 %s %s：qty=%s pnl=%s（%s%%）", rec.symbol, rec.direction,
                close_qty, rec.realized_pnl, rec.pnl_pct)
    return {"qty": float(close_qty), "pnl": pnl, "pnl_pct": rec.pnl_pct}


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


def sync_account_transfers(db: Session) -> int:
    """同步合约账户资金划转记录（income TRANSFER）到 account_transfers 表。

    逐账号同步（docs/17 P0）：.env 注册表内每个账号各拉各的流水，按
    testnet + account_key 落库；key 未配置的账号自然不在注册表。增量拉取：
    从该账号库内最新 transfer_time 前移 60s 重叠窗口开始（首次从 7 天前起），
    tran_id 唯一约束 + on_conflict_do_nothing 幂等去重。返回新插入条数（全部账号合计）。

    测试网账号不同步划转（2026-09-29 写死，仅正式网）。
    """
    inserted = 0
    cfg = db.get(SystemConfig, 1)
    # 测试网清除水位：一键清空删光划转后记录时刻，testnet 分支以它为下限，
    # 防止「无记录→从 7 天前重拉」把刚删掉的划转回灌（仅对 testnet 生效）
    purge_at = cfg.testnet_transfer_purge_at if cfg else None
    from app.services.account_registry import registry
    for acc in registry():
        if acc.testnet:
            continue
        trader = BinanceTrader(acc.key)
        if not trader.configured:
            continue
        last = db.execute(
            select(AccountTransfer.transfer_time)
            .where(
                AccountTransfer.testnet == acc.testnet,
                AccountTransfer.account_key == acc.key,
            )
            .order_by(AccountTransfer.transfer_time.desc())
            .limit(1)
        ).scalar()
        since = last - timedelta(seconds=60) if last else datetime.utcnow() - timedelta(days=7)
        if acc.testnet and purge_at and since < purge_at:
            since = purge_at
        since_ms = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)
        try:
            rows = trader.transfer_rows(since_ms)
        except Exception as e:
            logger.warning("划转同步失败（%s/%s）: %s", acc.key, "testnet" if acc.testnet else "正式网", e)
            continue
        # tranId 各账号独立计数，跨账号可能撞号：默认账号保持原格式（历史数据
        # 兼容），附加账号加 account_key 前缀隔离；时间组合键同理
        prefix = "" if acc.key in ("testnet-1", "live-1") else f"{acc.key}:"
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
            base_id = (
                str(raw_tran)
                if raw_tran not in (None, "", 0, "0")
                else f"{t_ms}:{row.get('asset') or 'USDT'}:{row.get('income')}"
            )
            stmt = pg_insert(AccountTransfer).values(
                tran_id=f"{prefix}{base_id}",
                asset=row.get("asset") or "USDT",
                amount=amount,
                transfer_time=datetime.utcfromtimestamp(t_ms / 1000),
                testnet=acc.testnet,
                account_key=acc.key,
                raw=row,
            ).on_conflict_do_nothing(index_elements=["tran_id"])
            res = db.execute(stmt)
            inserted += res.rowcount or 0
        db.commit()
    return inserted


def purge_testnet_data(db: Session, force: bool = False) -> dict:
    """一键清空测试网交易数据（2026-09-20，页面维护操作，实盘数据不受影响）。

    两阶段：force=False 返回将删除的数量（预检）；force=True 执行删除：
    trade_records（testnet=true）→ 级联清其 trade_events + account_transfers
    （testnet=true）→ 记录划转清除水位防下轮同步回灌。不删 trade_reviews
    （复盘统计基于 AI 分析、无环境归属，2026-09-20 拍板不在清理范围）。
    在跑单只提示不拦截（删除后交易所侧仓位无人管理，由前端弹窗二次确认）。
    返回 {records, events, transfers, running, executed}。
    """
    running = int(
        db.execute(
            select(func.count()).select_from(TradeRecord).where(
                TradeRecord.testnet == True,  # noqa: E712
                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
            )
        ).scalar_one()
    )
    ids = db.execute(
        select(TradeRecord.id).where(TradeRecord.testnet == True)  # noqa: E712
    ).scalars().all()
    events = int(
        db.execute(
            select(func.count()).select_from(TradeEvent).where(
                TradeEvent.trade_record_id.in_(ids)
            )
        ).scalar_one()
    ) if ids else 0
    transfers = int(
        db.execute(
            select(func.count()).select_from(AccountTransfer).where(
                AccountTransfer.testnet == True  # noqa: E712
            )
        ).scalar_one()
    )
    stats = {
        "records": len(ids),
        "events": events,
        "transfers": transfers,
        "running": running,
        "executed": False,
    }
    if not force:
        return stats

    task_id = open_task("trade", "清空测试网数据", trigger="manual")
    try:
        if ids:
            db.execute(delete(TradeEvent).where(TradeEvent.trade_record_id.in_(ids)))
            db.execute(delete(TradeRecord).where(TradeRecord.id.in_(ids)))
        if transfers:
            db.execute(
                delete(AccountTransfer).where(AccountTransfer.testnet == True)  # noqa: E712
            )
            cfg = db.get(SystemConfig, 1)
            if cfg:
                cfg.testnet_transfer_purge_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        close_task(task_id, "failed", error=str(e))
        raise
    stats["executed"] = True
    close_task(
        task_id,
        "completed",
        summary=f"删除记录 {stats['records']} / 事件 {stats['events']} / 划转 {stats['transfers']}",
        detail=stats,
    )
    return stats


def settle_trades(db: Session, trader: BinanceTrader) -> tuple[int, list[str]]:
    """对非终态交易：以交易所为准推断事件、跟进止损、结算收益。

    返回 (处理笔数, AI 持仓诊断触发清单)——触发清单由调用方在锁外派发
    celery 子任务诊断（docs/16：LLM 调用绝不进开仓锁）。

    每条记录按自带 account_key 选对应账号结算（docs/17 P0；NULL 旧行按 testnet
    回落 testnet-1/live-1）；trader 参数为当前环境实例，仅作同账号复用与孤儿清扫兜底。

    巡检范围 = 系统自开的单（含测试网），App 手动单不在库内天然不巡检；
    接管入口在任务层按环境守卫（2026-09-29：测试网不接管，避免同步手动单）。
    孤儿清扫兜底不碰测试网主账号，防误撤测试网 App 手动单的挂单。
    """
    cfg = db.get(SystemConfig, 1)
    interval = cfg.kline_interval if cfg else "1h"
    # 保本止损费率口径（2026-10-02 上页面）：system_config.taker_fee_rate 可页面
    # 覆盖，NULL=env 默认（_breakeven_stop_price 内回落）；一轮巡检取值一致
    fee_rate = (
        float(cfg.taker_fee_rate)
        if cfg is not None and cfg.taker_fee_rate is not None else None
    )
    # ATR 跟进倍数（2026-10-04 上页面，docs/13 #5）：cfg 覆盖 NULL 回落 env；0=关闭 ATR 腿
    atr_k = (
        float(cfg.atr_trail_k)
        if cfg is not None and cfg.atr_trail_k is not None
        else float(settings.ATR_TRAIL_K)
    )
    records = db.execute(
        select(TradeRecord).where(TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")))
    ).scalars().all()

    def _rec_acc_key(rec: TradeRecord) -> str:
        return rec.account_key or ("testnet-1" if rec.testnet else "live-1")

    traders: dict[str, BinanceTrader] = {}

    def _rec_trader(rec: TradeRecord) -> BinanceTrader:
        key = _rec_acc_key(rec)
        if key not in traders:
            from app.services.account_registry import get_account
            if get_account(key):
                traders[key] = BinanceTrader(key)
            else:
                # 账号已从 .env 移除：回落环境默认账号（旧行为）
                traders[key] = trader if trader.testnet == rec.testnet else BinanceTrader(rec.testnet)
        return traders[key]

    acc_positions: dict[str, dict] = {}
    for key in {_rec_acc_key(r) for r in records}:
        first = next(r for r in records if _rec_acc_key(r) == key)
        acc_positions[key] = _rec_trader(first).positions()

    def _active_syms(key: str) -> set:
        return {r.symbol for r in records if _rec_acc_key(r) == key}

    if not records:
        # 孤儿清扫兜底只针对巡检仍在服务的账号：测试网主账号不扫（2026-09-29）
        if not trader.testnet:
            _sweep_orphan_algo_orders(trader, set(), trader.positions())
        return 0, []
    handled = 0
    advice_ids: list[str] = []
    now = datetime.utcnow()
    # 双网分析所有权（2026-10-08）：测试网单有在跑正式网孪生 → 不独立诊断，
    # 建议由正式网诊断镜像同步（position_advisor._mirror_advice_to_testnet）；
    # records 已全量在内存，预计算集合零额外查询（ai_analysis_id=None 不配对）
    live_analysis_ids = {r.ai_analysis_id for r in records
                         if not r.testnet and r.ai_analysis_id is not None}

    for rec in records:
        try:
            rt = _rec_trader(rec)
            pos_map = acc_positions.get(_rec_acc_key(rec)) or {}
            qty = float(rec.qty or 0)
            raw = rec.raw or {}
            pos = pos_map.get(rec.symbol)
            amt = abs(pos["amt"]) if pos else 0.0
            open_ids = rt.open_order_ids(rec.symbol)
            tp1_gone = raw.get("tp1_order_id") and int(raw["tp1_order_id"]) not in open_ids
            tp2_gone = raw.get("tp2_order_id") and int(raw["tp2_order_id"]) not in open_ids

            # 仓位归零 → 结算（净盈亏 = 已实现盈亏 − 手续费 − 资金费 − 强平清算，正/负值）
            if qty <= 0 or amt < qty * 0.05:
                # 清理残留挂单（SL 先成交时 TP reduceOnly 单会一直挂着）
                rt.cancel_all_algo(rec.symbol)
                # 起点回拨 5s：开仓手续费 income 的时间戳可能略早于 opened_at（同 _record_realized）
                since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
                rows = rt.income_rows(rec.symbol, since_ms)
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
                rec.unrealized_pnl = 0  # 终态无浮动（docs/16）
                pnl = round(rt.sum_income(rows, rt.INCOME_KEEP), 6)
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
                    exit_reason = _zero_pos_exit_reason(rec, tp1_gone, fee_rate)
                    # 外部平仓识别（2026-10-02 GTCUSDT 案例）：最后一批平仓侧成交
                    # 不在系统单链内 → 交易所网页/App 手动平仓，按 manual 记，
                    # 不再让 bookkeeping 推断误标为止损类出场
                    ext = _detect_external_close(rt, rec)
                    if ext:
                        exit_reason = "manual"
                        _add_event(db, rec, "EXTERNAL_CLOSE", ext)
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
            # 浮动盈亏快照 + 全程收益率（docs/16）：复用本轮已拉取的仓位数据，
            # 零额外交易所请求；交易所为真相
            unrealized = float(pos.get("unrealized_pnl") or 0)
            rec.unrealized_pnl = round(unrealized, 6)
            # 强平价快照（P-09，2026-10-07）：mark price 流爆仓距离预警的数据源
            # （巡检顺带落库零额外请求；事件流 refresh_mark_pnl 按此计算距离）
            if pos.get("liquidation_price"):
                rec.raw = {**(rec.raw or {}), "liq_price": float(pos["liquidation_price"])}
            total_pnl = float(rec.realized_pnl or 0) + unrealized
            # 盈亏峰值跟踪（2026-09-26）：浮盈回撤 ≥0.6R 触发诊断的参照（position_advisor）
            if total_pnl > float(rec.peak_total_pnl or 0):
                rec.peak_total_pnl = round(total_pnl, 6)
            if rec.risk_amount and float(rec.risk_amount) > 0:
                rec.pnl_pct = round(total_pnl / float(rec.risk_amount) * 100, 2)
            # 浮亏在跑单 AI 持仓诊断触发判定（冷却/恶化规则见 position_advisor）；
            # 仅收集，锁外由调用方派发子任务执行。双网所有权：有在跑正式网
            # 孪生的测试网单跳过（跟随镜像）
            if advice_due(rec, total_pnl, now) \
                    and not (rec.testnet
                             and rec.ai_analysis_id in live_analysis_ids):
                advice_ids.append(str(rec.id))
            # 止损挂单存活巡检：SL 单被撤/丢失时补挂（裸仓保护，docs/07 §8-A2）
            _rehang_lost_sl(db, rt, rec, open_ids)
            if rec.status == "OPENED":
                if tp2_gone and ratio <= 0.35:
                    # 同一小时两档止盈都成交：从交易所成交明细按时间序+数量精确拆分
                    # 各档价差盈亏（与常规路径的 _tranche_pnl 书签口径同义，但两档同轮
                    # 成交无法用书签差值区分；TP 条件单触发后换新单号，无法按单号归集）。
                    # 拆分失败回退旧口径——两档合计只记在 TP2 上，TP1 不显示金额
                    realized = _record_realized(db, rt, rec)
                    qty_tp1, qty_tp2 = float(raw.get("qty_tp1") or 0), float(raw.get("qty_tp2") or 0)
                    tranche1 = tranche2 = None
                    if qty_tp1 > 0 and qty_tp2 > 0:
                        try:
                            since_ms = int(rec.opened_at.replace(
                                tzinfo=timezone.utc).timestamp() * 1000) - 5_000
                            tranche1, tranche2 = rt.fill_tranche_pnls(
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
                    _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k)
                    db.commit()
                    handled += 1
                elif tp1_gone and ratio <= 0.75:
                    realized = _record_realized(db, rt, rec)
                    tranche = _tranche_pnl(rt, rec, raw)
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP1_HIT"
                    _move_sl_breakeven(db, rt, rec, open_ids, fee_rate)  # 用户规则：TP1 后止损移至保本价（成本价±往返费缓冲）
                    if not rec.tp2:
                        # 无 TP2：TP1 止盈 80% 后剩余仓即启用跟进止损（与 TP2 后同机制）
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k)
                    db.commit()
                    handled += 1
                elif (raw.get("tp_mode") or "") == "trail":
                    # 纯跟踪单（docs/19 §3.4）：无 TP 挂单（tp*_gone 恒 False 落到这里），
                    # 浮盈达武装线保本 → 每轮跟进。peak_total_pnl 已在本轮上方快照更新
                    _trail_mode_patrol(db, rt, rec, interval, open_ids, fee_rate, atr_k)
                    db.commit()
                    handled += 1
            elif rec.status == "TP1_HIT":
                if tp2_gone and ratio <= 0.35:
                    realized = _record_realized(db, rt, rec)
                    tranche = _tranche_pnl(rt, rec, raw)
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP2_HIT"
                    _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k)
                    db.commit()
                    handled += 1
                else:
                    if rec.realized_pnl is None:
                        # 回填：部署前已升到 TP1_HIT 的旧单，TP1_FILL 事件未记已止盈金额
                        _record_realized(db, rt, rec)
                    # 保本兜底（幂等）：部署前已处于 TP1_HIT 的旧单、或上次保本移动失败的补偿
                    _move_sl_breakeven(db, rt, rec, open_ids, fee_rate)
                    if not rec.tp2:
                        # 无 TP2：与 TP2 后同规则，每次巡检跟进止损（只收紧不放松）
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k)
                    db.commit()
                    handled += 1
            elif rec.status == "TP2_HIT":
                # 每小时跟进止损（只收紧不放松）
                _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k)
                db.commit()
                handled += 1
        except Exception as e:
            db.rollback()
            logger.warning("结算 %s 失败: %s", rec.symbol, e)
            continue
    # ③ 孤儿条件单清扫（每轮兜底：无持仓、无在跑记录的在挂委托一律撤销，按账号执行）
    for key, rt in traders.items():
        _sweep_orphan_algo_orders(rt, _active_syms(key), acc_positions.get(key) or {})
    return handled, advice_ids


# ── 导入持仓（2026-09-24）：币安 App 手动开的单接管进系统 ─────────────
# 接管后与系统单同待遇：每小时巡检结算/保本/跟进止损/AI 持仓诊断/复盘归因。
# 管理范围从接管时刻起（接管前的已实现盈亏不回溯）；接管时撤掉原手动
# 条件单，按系统规则重挂 SL（closePosition）/TP1/TP2（reduceOnly 50%/25%）。

_ADOPT_RUNNING = ("OPENED", "TP1_HIT", "TP2_HIT")


# ── 持仓时长警告（2026-09-27）：在跑单 >12h 黄色预警卡 / >24h 红色预警卡 ──
# 随结算巡检（每 30 分钟）判定；hold_warn_level 只升级不重发（幂等去重）；
# 前端交易记录「持仓时间」列用同一阈值着色（12h 黄 #faad14 / 24h 红 #ff4d4f）。
_HOLD_WARN_YELLOW_H = 12
_HOLD_WARN_RED_H = 24


def _fmt_price(p: float) -> str:
    """卡片价格展示：按量级适配小数位（与前端 fmtPrice 同口径）"""
    if p < 1:
        return f"{p:.6f}"
    return f"{p:.4f}" if p < 100 else f"{p:.2f}"


def _open_feishu_card(rec: TradeRecord) -> dict:
    """自动开仓成功消息卡片（2026-10-02）：币种/方向/杠杆/风险 + 入场/止损/止盈
    价格栅格 + 数量·名义·信号·战法标注行 + 环境脚注。

    返回卡片体（外层包装与发送通道由 feishu_bot 负责）；金K形态/信号族中文
    沿用复盘映射（与 _scan_hit_card 同源）。战法标注取 ai_snapshot 自持值
    （快照自持，删战法不断链）。
    """
    from app.services.actual_review import TRIGGER_FAMILY
    from app.services.strategy.candlestick import PATTERN_LABEL_MAP

    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    is_long = rec.direction == "long"
    side = "做多 Long" if is_long else "做空 Short"
    env = "测试网" if rec.testnet else "正式网"
    snap = rec.ai_snapshot or {}
    sig = TRIGGER_FAMILY.get(rec.signal_type or "", rec.signal_type or "未知信号")
    if snap.get("pattern"):
        sig += f"·{PATTERN_LABEL_MAP.get(snap['pattern'], snap['pattern'])}"
    tactic = snap.get("tactic_name")
    recmd = (
        f"{float(rec.recommendation):.0f} 分" if rec.recommendation is not None else "—"
    )
    qty_line = (
        f"数量 {rec.qty:g} · 名义 {float(rec.notional or 0):.2f} USDT"
        if rec.qty else ""
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🚀 自动开仓 · {rec.symbol}"},
            "template": "blue",
        },
        "elements": [
            # 概览行：方向 / 杠杆 / 预估止损风险 / AI 评分
            {"tag": "column_set", "flex_mode": "stretch", "columns": [
                _col("方向", side),
                _col("杠杆", f"{rec.leverage}× 逐仓"),
                _col("预估风险", f"{float(rec.risk_amount or 0):g} USDT"),
                _col("AI 评分", recmd),
            ]},
            # 价格栅格：入场 / 止损 / 止盈（本单核心计划）
            {"tag": "column_set", "flex_mode": "stretch", "columns": [
                _col("入场价", _fmt_price(float(rec.entry_price or 0))),
                _col("止损 SL", _fmt_price(float(rec.stop_loss or 0))),
                _col("止盈 TP1", _fmt_price(float(rec.tp1 or 0))),
                _col("止盈 TP2", _fmt_price(float(rec.tp2)) if rec.tp2 else "—"),
            ]},
            {"tag": "div", "text": {"tag": "lark_md", "content":
             " · ".join(x for x in [qty_line, f"信号 {sig}"] if x)
             + (f" · 战法 {tactic}" if tactic else "")}},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": f"{env} · {rec.account_key or '默认账号'} · SL/TP 条件单已挂 · 详情见交易记录"}]},
        ],
    }


def notify_open_cards(trade_ids: list[str]) -> int:
    """自动开仓成功 → 逐笔发飞书开仓卡片。必须在开仓锁外调用（飞书 HTTP
    超时 10s/张不进锁，与 check_hold_warnings 卡片同一惯例）；未配置自建
    应用整批跳过，单张失败仅记日志不影响其余。返回成功发送张数。"""
    try:
        from app.services.feishu_bot import configured, send_app_card
        if not configured():
            return 0
    except Exception as e:
        logger.warning("开仓卡片通道检查失败，跳过推送: %s", e)
        return 0

    from app.database import SessionLocal

    sent = 0
    db = SessionLocal()
    try:
        for tid in trade_ids:
            try:
                rec = db.get(TradeRecord, UUID(str(tid)))
                if rec is None:
                    continue
                if send_app_card(_open_feishu_card(rec)):
                    sent += 1
            except Exception as e:
                logger.warning("开仓卡片发送失败 %s: %s", tid, e)
    finally:
        db.close()
    return sent


def _live_off_card(a: AIAnalysis) -> dict:
    """正式网自动开单关闭提醒卡片（2026-10-09）：价格栅格复用开仓卡片风格，
    橙色头区分（蓝=开仓成功、橙=实盘错过达标信号）。"""
    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    is_long = a.direction == "long"
    recmd = f"{float(a.recommendation):.0f} 分" if a.recommendation is not None else "—"
    rr = f"{float(a.risk_reward_ratio):.1f}" if a.risk_reward_ratio else "—"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⚠️ 正式网未开单 · {a.symbol}"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             "该信号已达标可开单，但**正式网自动开单开关关闭**，本次未开实盘单（测试网照常）。"}},
            {"tag": "column_set", "flex_mode": "stretch", "columns": [
                _col("方向", "做多 Long" if is_long else "做空 Short"),
                _col("AI 评分", recmd),
                _col("盈亏比", rr),
            ]},
            {"tag": "column_set", "flex_mode": "stretch", "columns": [
                _col("入场价", _fmt_price(float(a.entry_price or 0))),
                _col("止损 SL", _fmt_price(float(a.stop_loss or 0))),
                _col("止盈 TP1", _fmt_price(float(a.take_profit_1 or 0))),
                _col("止盈 TP2", _fmt_price(float(a.take_profit_2)) if a.take_profit_2 else "—"),
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": (f"战法 {a.tactic_name} · " if a.tactic_name else "")
             + "如需实盘可在扫描结果/交易记录手动开单，或开启正式网自动开单"}]},
        ],
    }


def notify_live_auto_open_off(db: Session, cfg, analysis_id: str) -> None:
    """正式网自动开单开关关闭 → 达标信号错过实盘开仓，飞书提醒（2026-10-09）。

    「可以开单」口径 = 开仓资格快速过滤（analysis_open_eligible，与开仓闸门
    同源；评分门槛分发层已过，此处复验防门槛中途上调）。不含账号维度闸门
    ——正式网开关关着本就没跑到账号闸门那一步，无法也不必预判其结果。
    该分析正式网任一账号已有成交（含此前提醒后手动开的单）则不提醒。
    每条分析至多随任务分发提醒一次；发送失败仅记日志不影响开仓扇出。
    """
    try:
        if cfg is None:
            return
        a = db.get(AIAnalysis, UUID(str(analysis_id)))
        if a is None:
            return
        min_rec = (
            float(cfg.min_recommendation) if cfg.min_recommendation is not None
            else float(settings.TRADING_MIN_RECOMMENDATION)
        )
        if not analysis_open_eligible(a, min_rec):
            return
        # 实盘已有单（提醒后手动开/重新分析前已开）不再重复提醒
        if db.execute(
            select(TradeRecord.id).where(
                TradeRecord.ai_analysis_id == a.id,
                TradeRecord.testnet.is_(False),
            ).limit(1)
        ).first():
            return
        from app.services.feishu_bot import configured, send_app_card
        if not configured():
            return
        if send_app_card(_live_off_card(a)):
            logger.info("已发送正式网开关关闭提醒 analysis=%s symbol=%s",
                        analysis_id, a.symbol)
    except Exception:
        logger.exception("正式网开关关闭提醒异常 analysis=%s", analysis_id)



def check_hold_warnings(db) -> list[dict]:
    """持仓时长分级警告判定（2026-09-27，随结算巡检每 30 分钟执行）。

    在跑单（OPENED/TP1_HIT/TP2_HIT）持仓超 12h → 黄色预警、超 24h → 红色
    预警（跨级直接发红卡不补黄卡）。hold_warn_level 只升级不重发；平仓后
    状态不在跑即不再判定。只做 DB 判定与标记（本函数内 commit），返回待发
    卡片列表由调用方在开仓锁外发送——飞书 HTTP（超时 10s/张）不进开仓锁。
    """
    now = datetime.now(timezone.utc)
    cards: list[dict] = []
    recs = db.execute(
        select(TradeRecord).where(TradeRecord.status.in_(_ADOPT_RUNNING))
    ).scalars().all()
    for rec in recs:
        if not rec.opened_at:
            continue
        # opened_at 为 UTC-naive（datetime.utcnow 落库）；挂 tzinfo 后与 now 同源相减
        hours = (now - rec.opened_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        level = 2 if hours >= _HOLD_WARN_RED_H else 1 if hours >= _HOLD_WARN_YELLOW_H else 0
        if level == 0 or (rec.hold_warn_level or 0) >= level:
            continue
        rec.hold_warn_level = level
        cards.append(_hold_warn_card(rec, hours, level))
    if cards:
        db.commit()
        logger.warning("持仓时长警告 %d 张：%s", len(cards),
                       "、".join(f"{c['_symbol']}({c['_hours']:.1f}h)" for c in cards))
    return cards


def _hold_warn_card(rec, hours: float, level: int) -> dict:
    """构建黄色/红色预警卡片（黄=12h 提醒、红=24h 升级处置）。

    数据取巡检刚刷新的 DB 快照（unrealized_pnl/stop_loss），不调交易所——
    判定在锁内、展示要快。浮盈/浮亏用 <font color> 着色（green/red）。
    """
    is_long = (rec.direction or "long") != "short"
    dur = f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"
    pnl = rec.unrealized_pnl
    pnl_txt = "—" if pnl is None else f"{pnl:+.2f} USDT"
    pnl_color = "green" if (pnl or 0) >= 0 else "red"

    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    red = level == 2
    body = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"已持仓 **{dur}**（{'🔴 红色预警' if red else '🟡 黄色预警'}）"}},
        {"tag": "column_set", "flex_mode": "stretch", "columns": [
            _col("方向", "做多" if is_long else "做空"),
            _col("开仓价", _fmt_price(float(rec.entry_price)) if rec.entry_price else "—"),
            _col("当前止损", _fmt_price(float(rec.stop_loss)) if rec.stop_loss else "—"),
            _col("浮动盈亏", f"<font color='{pnl_color}'>{pnl_txt}</font>"),
        ]},
        {"tag": "note", "elements": [{"tag": "plain_text",
         "content": ("占用风险预算超 24h · 请检查持仓逻辑或到交易记录页市价平仓"
                     if red else "超 24h 将升级红色预警 · 交易记录页可市价平仓")}]},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"{'🚨 持仓超24小时' if red else '⏳ 持仓超12小时'} · {rec.symbol}"},
            "template": "red" if red else "yellow",
        },
        "elements": body,
        # 内部字段：发送侧日志用，发送前无需剔除（飞书忽略未知键）
        "_symbol": rec.symbol,
        "_hours": hours,
    }


def _adopt_acc_cond(trader: BinanceTrader):
    """同账号在跑记录过滤条件（NULL 旧行按环境回落，与 list_trades 口径一致）"""
    return or_(
        TradeRecord.account_key == trader.account_key,
        and_(TradeRecord.account_key.is_(None), TradeRecord.testnet == trader.testnet),
    )


def list_adoptable_positions(db: Session, trader: BinanceTrader) -> list[dict]:
    """当前账号交易所全部非零持仓（导入持仓弹窗数据源）：
    附现挂条件单解析出的 SL/TP 预填值（用户在币安 App 设置的止盈止损）、
    现价与 tracked 标记（已被系统在跑记录接管的不重复导入）。
    任一 symbol 行情/挂单查询失败降级为空值，不阻塞整表。"""
    running = {
        sym for (sym,) in db.execute(
            select(TradeRecord.symbol).where(
                TradeRecord.status.in_(_ADOPT_RUNNING), _adopt_acc_cond(trader))
        ).all()
    }
    close_side_of = {"long": "SELL", "short": "BUY"}
    out = []
    for sym, p in sorted(trader.positions().items()):
        amt = float(p["amt"])
        direction = "long" if amt > 0 else "short"
        close_side = close_side_of[direction]
        # 现挂条件单解析：SL = STOP_MARKET/STOP（市价/限价触发止损，App 默认市价）；
        # TP = TAKE_PROFIT_MARKET/TAKE_PROFIT。按触发先后排序（long 触发价升序 /
        # short 降序）→ 前两档预填 TP1/TP2；追踪委托无固定触发价不识别
        sl_pre: Optional[float] = None
        tps: list[float] = []
        try:
            for o in trader.open_algo_orders(sym):
                if o.get("side") != close_side:
                    continue
                try:
                    trigger = float(o.get("triggerPrice") or 0)
                except (TypeError, ValueError):
                    continue
                if trigger <= 0:
                    continue
                # Algo Order API 返回字段为 orderType（无 type 字段，2026-09-24 线上实锤）
                otype = o.get("orderType") or o.get("type")
                if otype in ("STOP_MARKET", "STOP"):
                    sl_pre = trigger
                elif otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                    tps.append(trigger)
        except Exception as e:
            logger.warning("导入持仓：拉取 %s 在挂条件单失败（预填为空）: %s", sym, e)
        tps.sort(reverse=(direction == "short"))
        try:
            cur = trader.price(sym)
        except Exception:
            cur = 0.0
        entry = float(p.get("entry_price") or 0)
        margin_used = float(p.get("initial_margin") or 0)
        notional = abs(amt) * entry
        out.append({
            "symbol": sym,
            "direction": direction,
            "qty": abs(amt),
            "entry_price": entry,
            "current_price": cur,
            "unrealized_pnl": float(p.get("unrealized_pnl") or 0),
            "margin_used": round(margin_used, 4),
            "leverage": max(1, round(notional / margin_used)) if margin_used > 0 else 1,
            "notional": round(notional, 2),
            "tracked": sym in running,
            "prefill": {
                "stop_loss": sl_pre,
                "tp1": tps[0] if tps else None,
                "tp2": tps[1] if len(tps) > 1 else None,
            },
        })
    return out


def adopt_position(db: Session, trader: BinanceTrader, symbol: str,
                   stop_loss: float, tp1: float, tp2: float = 0) -> TradeRecord:
    """导入持仓：把交易所已有手动仓接管为系统在跑单（status=OPENED）。

    流程：持仓仍在校验 → 无同账号同币在跑记录 → 三价方向校验（按现价）→
    撤原手动条件单 → 按系统规则重挂 SL/TP1/TP2（失败尽力恢复原 SL，绝不
    把用户裸仓留在市场）→ 落库（opened_at=接管时刻，管理范围从此刻起）。
    需在开仓锁（lock:trade_open:{account}）内调用，与结算巡检互斥。
    """
    # ① 持仓仍在（弹窗打开到提交之间可能已平仓）
    pos = trader.positions().get(symbol)
    if not pos or abs(float(pos["amt"])) == 0:
        raise ValueError(f"交易所已无 {symbol} 持仓（可能已平仓），请刷新列表")
    # 止损金额模式快照（2026-09-27）：接管单同样记录开仓口径，便于追溯
    _adopt_cfg = db.get(SystemConfig, 1)
    amt = float(pos["amt"])
    direction = "long" if amt > 0 else "short"
    close_side = _close_side(direction)
    qty = abs(amt)
    entry = float(pos.get("entry_price") or 0)
    if entry <= 0 or qty <= 0:
        raise ValueError(f"{symbol} 持仓数据异常（entry={entry}, qty={qty}）")

    # ② 同账号同币不重复接管
    exists = db.execute(
        select(TradeRecord.id).where(
            TradeRecord.symbol == symbol,
            TradeRecord.status.in_(_ADOPT_RUNNING),
            _adopt_acc_cond(trader),
        )
    ).first()
    if exists:
        raise ValueError(f"{symbol} 已有系统在跑记录，无需导入")

    # ③ 三价方向校验（按现价；导入单不做盈亏比/风险预算闸门——尊重用户既定计划）
    stop_loss, tp1, tp2 = float(stop_loss), float(tp1), float(tp2 or 0)
    price_now = trader.price(symbol)
    if stop_loss <= 0 or tp1 <= 0:
        raise ValueError("止损价与止盈一必填")
    if direction == "long":
        if stop_loss >= price_now:
            raise ValueError(f"多头止损价 {stop_loss} 必须低于现价 {price_now}")
        if tp1 <= price_now:
            raise ValueError(f"多头止盈一 {tp1} 必须高于现价 {price_now}")
        if tp2 and tp2 <= tp1:
            raise ValueError(f"止盈二 {tp2} 必须高于止盈一 {tp1}")
    else:
        if stop_loss <= price_now:
            raise ValueError(f"空头止损价 {stop_loss} 必须高于现价 {price_now}")
        if tp1 >= price_now:
            raise ValueError(f"空头止盈一 {tp1} 必须低于现价 {price_now}")
        if tp2 and tp2 >= tp1:
            raise ValueError(f"止盈二 {tp2} 必须低于止盈一 {tp1}")

    # ④ 系统接管：撤原手动条件单 → 重挂 SL/TP1/TP2。
    #    SL 是裸仓保护的生命线：挂失败时尽力按原价恢复用户的 SL，再把异常抛回。
    #    注意 Algo API 字段为 orderType；App OTOCO 止损是 reduceOnly 带数量
    #    （closePosition=false），恢复匹配不要求 closePosition，认 STOP_MARKET/STOP 即可
    orig_algo = trader.open_algo_orders(symbol)
    orig_sl = next(
        (float(o.get("triggerPrice") or 0) for o in orig_algo
         if o.get("side") == close_side
         and (o.get("orderType") or o.get("type")) in ("STOP_MARKET", "STOP")
         and float(o.get("triggerPrice") or 0) > 0),
        None,
    )
    trader.cancel_all_algo(symbol)
    try:
        sl_order = trader.stop_market_close(
            symbol, close_side, trader.round_price(symbol, stop_loss))
    except Exception:
        if orig_sl:
            try:
                trader.stop_market_close(symbol, close_side, trader.round_price(symbol, orig_sl))
                logger.warning("导入 %s：新 SL 挂单失败，已按原价 %s 恢复止损", symbol, orig_sl)
            except Exception:
                logger.error("导入 %s：新 SL 挂单失败且原价恢复也失败，仓位暂无止损保护！", symbol)
        raise
    f = trader.filters(symbol)
    _p1, _p2, _p1s = _tp_ratio_cfg(_adopt_cfg)
    qty_tp1, qty_tp2 = _tp_quantities(qty, tp2, _p1, _p2, _p1s)
    tp1_order = None
    tp2_order = None
    if qty_tp1 >= f["min_qty"]:
        try:
            tp1_order = trader.take_profit_reduce(
                symbol, close_side, trader.round_qty(symbol, qty_tp1),
                trader.round_price(symbol, tp1))
        except Exception as e:
            logger.warning("导入 %s：TP1 挂单失败（SL 已就位，仅缺自动止盈）: %s", symbol, e)
    if tp2 > 0 and tp1_order and qty_tp2 >= f["min_qty"] and qty_tp1 + qty_tp2 < qty:
        try:
            tp2_order = trader.take_profit_reduce(
                symbol, close_side, trader.round_qty(symbol, qty_tp2),
                trader.round_price(symbol, tp2))
        except Exception as e:
            logger.warning("导入 %s：TP2 挂单失败: %s", symbol, e)

    # ⑤ 落库：opened_at=接管时刻（管理范围从此刻起，接管前盈亏不回溯）
    now = datetime.utcnow()
    notional = qty * entry
    margin_used = float(pos.get("initial_margin") or 0)
    rec = TradeRecord(
        symbol=symbol,
        direction=direction,
        scan_result_id=None,
        ai_analysis_id=None,  # 手动导入无 AI 分析行（2026-09-24 起该列可空）
        recommendation=None,
        rule_version=settings.RULE_VERSION,
        testnet=trader.testnet,
        account_key=trader.account_key,
        ai_snapshot=None,
        entry_price=entry,
        qty=qty,
        notional=round(notional, 2),
        leverage=max(1, round(notional / margin_used)) if margin_used > 0 else 1,
        margin_mode="isolated",
        margin_used=round(margin_used, 4),
        risk_amount=round(abs(entry - stop_loss) * qty, 4),  # 实际风险 = 止损距离 × 数量
        risk_budget_mode=(
            (_adopt_cfg.risk_budget_mode or "pct")
            if _adopt_cfg is not None else "pct"
        ),  # 接管时刻模式快照（无 AI 分析行的手动单同样记录）
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2 if tp2_order else 0,
        status="OPENED",
        opened_at=now,
        raw={
            "adopted": True,
            "sl_order_id": sl_order.get("algoId") or sl_order.get("orderId"),
            "tp1_order_id": (tp1_order.get("algoId") or tp1_order.get("orderId")) if tp1_order else None,
            "tp2_order_id": (tp2_order.get("algoId") or tp2_order.get("orderId")) if tp2_order else None,
            "qty_tp1": qty_tp1 if tp1_order else 0,
            "qty_tp2": qty_tp2 if tp2_order else 0,
            "realized_bookmark": 0,  # 接管时点起算（_tranche_pnl 书签）
            "adopt_price": price_now,
            "adopt_note": "导入持仓：接管时点起的盈亏与仓位管理由系统跟进，接管前已实现盈亏不回溯",
        },
    )
    db.add(rec)
    db.flush()
    _add_event(db, rec, "OPEN", {
        "adopted": True, "price": price_now, "qty": qty,
        "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2 if tp2_order else 0,
        "risk_amount": round(abs(entry - stop_loss) * qty, 4),
        "entry_price": entry, "note": "手动持仓导入（管理范围自接管时点起）",
    })
    db.commit()
    logger.info("已导入持仓 %s %s qty=%s entry=%s sl=%s tp1=%s tp2=%s（风险 %s USDT）",
                symbol, direction, qty, entry, stop_loss, tp1,
                tp2 if tp2_order else 0, rec.risk_amount)
    return rec


def auto_adopt_positions(db: Session, trader: BinanceTrader) -> list[str]:
    """自动导入手动持仓（2026-09-24，随每小时结算巡检执行）：
    交易所非零持仓中未被系统在跑记录接管的 = 用户在币安 App 手动开的单，
    逐一尝试 adopt_position 接管（SL/TP 取 App 现挂条件单原值）。

    跳过：已在跑记录的、App 未同时设置 SL+TP1 的（裸仓不入册，只记日志）、
    三价校验不过的。需在开仓锁内调用。返回新导入的 symbol 列表。
    """
    out: list[str] = []
    for row in list_adoptable_positions(db, trader):
        if row["tracked"]:
            continue
        sym = row["symbol"]
        pre = row["prefill"]
        if not pre["stop_loss"] or not pre["tp1"]:
            logger.info("自动导入跳过 %s：币安未同时设置止损+止盈条件单（裸仓不入册）", sym)
            continue
        try:
            adopt_position(db, trader, sym, pre["stop_loss"], pre["tp1"], pre["tp2"] or 0)
            out.append(sym)
        except ValueError as e:
            logger.info("自动导入跳过 %s：%s", sym, e)
        except Exception:
            logger.exception("自动导入失败 %s", sym)
    return out
