"""自动交易引擎（docs/06）

开仓（唯一通道 = 即时开仓，2026-09-14 起不再有 :42 批量开仓）：
    AI 结论 suggest 且推荐度 ≥60 → 分析落库即分发 open_trade_for_analysis
    → try_open_for_analysis 过闸门（在跑单上限 / 余额风控(可配置) / 同币无仓 /
    方向价格复验）→ 固定亏损法定仓位（基数分档 ×3% ÷ 止损距离）
    → 市价开仓 + 挂 SL/TP1/TP2 → 入库；闸门未过即放弃，无批次重试

结算（settle_trades，每小时 :01 巡检，对齐 1h K 线收盘 1 分钟）：
    以交易所为真源（持仓量/挂单/已实现盈亏）推断成交事件：
    仓位归零 → 结算收益（正/负值）；外部平仓识别（actualOrderId 单链，
    2026-10-02）→ exit_reason=manual 不误标止损类；TP1/TP2 真成交 → 状态迁移
    （判定以 algoStatus 终态 FINISHED 为准，不看「单号是否还在挂单列表」——
    TRIGGERING/EXPIRED/REJECTED/CANCELED 都不是成交，2026-10-20 SQDUSDT 修复）；
    止损挂单消失（被撤/交易所异常）→ 按当前止损价补挂（裸仓保护）；
    TP1 成交 → 止损移至成本价 ± 往返费缓冲（保本，幂等，docs/24 A1''）；
    无 TP2 的单：TP1 止盈 80%，剩余 20% 从 TP1 起跟进止损（2026-10-09 调整）；
    TP2 后（或无 TP2 的单 TP1 后）每次巡检跟进止损（只收紧不放松）；
    移动止损方式按 trade_records.trailing_mode：hl=HL/LH 结构跟踪（新开单
    默认，2026-10-20）/ three_line=摆动极值 + ATR Chandelier（旧行与可选手动）
"""
import logging
import math
import re
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
from app.models.trade import (
    AccountTransfer,
    PendingEntryOrder,
    TradeEvent,
    TradeRecord,
)
from app.services.binance_trader import BinanceTrader, BinanceTradeError
from app.services.exchange_pool import ExchangePool
from app.services.llm_client import configure_from
from app.services.position_advisor import advice_due
from app.services.score_thresholds import effective_min_recommendation
from app.services.risk_guard import (
    AI_RR_TOL,
    liquidation_gate_pct,
    LIQ_GATE_BUFFER,
    LIQ_GATE_MMR_FALLBACK,
    PULLBACK_MARKET_PCT,
    MAX_SLIPPAGE_PCT,
)
from app.services.task_log import close_task, open_task

import numpy as np
from app.services.strategy.ema_gate import ema_series
from app.services.strategy.swing import find_swing_points

logger = logging.getLogger(__name__)

# 移动止损方式（2026-10-20 引入；2026-10-21 起新开单默认 three_line，
# hl 保留、可经 /trades/{id}/trailing-mode 切换）
# three_line=三线止损（摆动极值 + ATR Chandelier + TP1 保本夹紧）；
# hl=HL/LH 结构跟踪
TRAIL_MODE_HL = "hl"
TRAIL_MODE_THREE_LINE = "three_line"
TRAIL_MODES = (TRAIL_MODE_HL, TRAIL_MODE_THREE_LINE)


def _capital_base(wallet: float) -> float:
    """风险基数：按钱包余额首位数取整，上限 5000，且不超过本金（2026-10-27 修正）：
    首位 digit = int(wallet / 10^n)，基数 = digit × 10^n，超 5000 封顶；
    不足 100 时不再强制抬到 100，而是取钱包本金——
    旧口径 50 U 账户基数 100，单笔风险 3 U = 权益 6%，日熔断 9 U = 权益 18%。
    例：4800 → 4000、980 → 900、100 → 100、50 → 50、5 → 5、5200/15000 → 5000"""
    if wallet <= 0:
        return 0.0
    magnitude = 10 ** int(math.log10(wallet))
    digit = int(wallet / magnitude)
    base = float(min(max(digit * magnitude, 100), 5000))
    return float(min(base, wallet))


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


def _open_margin_required(qty: float, price: float, leverage: int,
                          fee_rate: Optional[float] = None) -> float:
    """开仓保证金需求 = 初始保证金 + 开仓 taker 手续费（2026-10-19 -2019 加固）。

    交易所对新开仓按「初始保证金 + 开仓手续费」之和校验可用余额
    （availableBalance）；此前闸门只比初始保证金（qty×价/杠杆），可用余额
    恰好等于初始保证金时被交易所按「+手续费」拒单（-2019 Margin is
    insufficient）。fee_rate 缺省取 settings.TAKER_FEE_RATE（交易所真实 taker
    费率，非页面建模参数 system_config.taker_fee_rate）。"""
    notional = qty * price
    fee = notional * (settings.TAKER_FEE_RATE if fee_rate is None else fee_rate)
    return notional / leverage + fee


def _cost_adjusted_stop_pct(ref_price: float, stop_loss: float,
                            fee_rate: Optional[float] = None) -> float:
    """计入双边 taker 费后的有效止损距离占比（2026-10-27 F1）。

    触止损时实际亏损 = 价格距离 + 入场费 + 出场费 ≈ qty × (|ref−sl| + fee×(ref+sl))，
    因此固定亏损法的计价分母须含往返费，否则窄止损单实际亏损显著超预算
    （0.5% 止损 + 0.05% taker ≈ 1.20×；再叠加止损滑点更高）。
    risk_amount 仍按价格风险记录，保持组合热度/复盘 R 的既有语义。
    """
    _fee = settings.TAKER_FEE_RATE if fee_rate is None else float(fee_rate)
    if ref_price <= 0:
        return 0.0
    return (abs(ref_price - stop_loss) + _fee * (ref_price + stop_loss)) / ref_price


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
                   p1: float = 60.0, p2: float = 20.0, p1s: float = 80.0,
                   full_exit: bool = False) -> tuple[float, float]:
    """TP1/TP2 分配数量（比例 % 值由 _tp_ratio_cfg 从 system_config/env 解析）：
    AI 给出 TP2 → TP1 p1% / TP2 p2% 两档分批止盈，剩余额跟进；
    未给出（AI 未报 / 前方无结构位程序回退清零 / 配置关闭 TP2）
    → TP1 一次止盈 p1s%，剩余额从 TP1 成交起由跟进止损退出（与 TP2 后同机制）。

    full_exit（2026-10-28 止盈全平模式）：不留跟进尾仓——有 TP2 时 TP2 平掉
    TP1 减仓后的剩余全部；无 TP2 时 TP1 一次平掉 100% 仓位。"""
    if tp2 and tp2 > 0:
        if full_exit:
            return qty * p1 / 100, qty * (100 - p1) / 100
        return qty * p1 / 100, qty * p2 / 100
    return (qty if full_exit else qty * p1s / 100), 0.0


def _tp_order_quantities(trader: BinanceTrader, symbol: str, qty: float,
                         tp2: float, cfg, min_qty: float = 0.0
                         ) -> tuple[float, float, bool]:
    """按配置解析并取整 TP1/TP2 挂单数量，返回 (qty_tp1, qty_tp2, 是否全平模式)。

    全平模式下 TP2 数量取「qty − 取整后的 TP1」，保证两档合计恰好覆盖全仓、
    不留交易所最小步长以下的碎尾仓；关闭时沿用分批比例。

    T14f（docs/23，2026-10-30）：全平模式下若 TP2 取整后仍低于交易所最小数量，
    TP2 根本挂不上（`raw.qty_tp2=0`），而 TP1 若仍按 p1=60% 挂，剩下 40% 尾仓
    只能靠跟踪止损退出、状态永远到不了 TP2_HIT（残仓兜底 T14e 也永远不触发）。
    此时把 TP1 提升为一整仓（100%），让「全平」在只有一个止盈档时仍然成立。"""
    p1, p2, p1s = _tp_ratio_cfg(cfg)
    full_exit = bool(cfg is not None and cfg.tp_full_exit_enabled)
    q1, q2 = _tp_quantities(qty, tp2, p1, p2, p1s, full_exit=full_exit)
    qty_tp1 = trader.round_qty(symbol, q1)
    if full_exit and tp2 > 0:
        qty_tp2 = trader.round_qty(symbol, max(0.0, qty - qty_tp1))
        if min_qty > 0 and qty_tp2 < min_qty:
            qty_tp1 = trader.round_qty(symbol, qty)
            qty_tp2 = 0.0
    else:
        qty_tp2 = trader.round_qty(symbol, q2)
    return qty_tp1, qty_tp2, full_exit


def _tp_sum_ok(qty_tp1: float, qty_tp2: float, qty: float,
               full_exit: bool) -> bool:
    """两档止盈数量合计校验：全平模式允许恰好等于全仓（TP2 平完剩余），
    关闭时仍须严格小于（保留跟进尾仓，与 2026-10-28 前的行为一致）。"""
    total = qty_tp1 + qty_tp2
    return total <= qty if full_exit else total < qty


def _tp2_skip_info(tp2: float, tp2_order, qty_tp1: float, qty_tp2: float,
                   qty: float, min_qty: float,
                   full_exit: bool) -> Optional[dict]:
    """TP2 挂单被跳过的原因（docs/23 T14d，2026-10-29）；None = 无需告警。

    AI 报了 TP2 却没挂上条件单时（数量低于最小步长 / 两档合计超出全仓 /
    下单异常），旧代码把 `rec.tp2` 静默写成 0，事后只看到「这单没有 TP2」，
    无法区分「AI 没报」与「报了没挂上」——前者是策略选择，后者是执行缺陷。
    这里把原因带出，落 raw.tp2_skipped + TP2_SKIPPED 事件。
    """
    if tp2 is None or float(tp2) <= 0 or tp2_order is not None:
        return None
    if qty_tp2 < min_qty:
        reason = "qty_below_min"
    elif not _tp_sum_ok(qty_tp1, qty_tp2, qty, full_exit):
        reason = "tp_sum_not_ok"
    else:
        reason = "order_failed"
    return {
        "tp2": round(float(tp2), 8),
        "qty_tp2": round(float(qty_tp2), 8),
        "min_qty": float(min_qty),
        "full_exit": bool(full_exit),
        "reason": reason,
    }


# 全平残仓自动补平上限（open 量占比）：超过即视为「TP2 后手动加仓」，只告警不平仓
_FULL_EXIT_RESIDUAL_MAX_RATIO = 0.10


def _enforce_full_exit_residual(db: Session, rt: BinanceTrader, rec: TradeRecord,
                                amt: float, ratio: float) -> bool:
    """全平模式 TP2 后残仓兜底（docs/23 T14e，2026-10-29）。

    背景：TRUTHUSDT 排查证明全平代码路径本身正确，但 TP2 数量受最小步长取整、
    两档合计校验、以及 2026-10-28 全平模式上线前的旧单影响——`tp_full_exit=true`
    的单在 TP2 成交后仍可能留残仓，旧逻辑把残仓交给跟进止损，与「全平」语义不符。
    这里在 TP2_HIT 时校验交易所实际持仓，仍有量就市价 reduceOnly 补平。

    约束：同单只补一次（raw.full_exit_residual_at 幂等标记）；补平成功写
    exit_override=tp_full（下一轮零仓位结算按「TP 档全平」记出场，不被
    bookkeeping 误标为止损类）。返回 True = 本次已补平，调用方跳过跟进止损。

    安全边界（ratio > 10% 不自动平）：TP2 后正常残仓只是最小步长取整的碎尾
    （通常 <5%），若交易所剩余量仍占开仓量 10% 以上，多半是 TP2 之后又手动
    加过仓（或快照严重滞后）——此时 reduceOnly 会把用户的加仓一起市价平掉，
    属于越权操作，只记事件告警、交人工核对。
    """
    raw = rec.raw or {}
    if not raw.get("tp_full_exit") or raw.get("full_exit_residual_at"):
        return False
    if amt <= 0:
        return False
    if ratio > _FULL_EXIT_RESIDUAL_MAX_RATIO:
        # 每单只告警一次（巡检每 30 分钟一轮，否则事件与警告会刷屏）；
        # 之后若用户减仓使 ratio 回落到阈值内，仍会正常补平
        if not raw.get("residual_skip_notified"):
            _add_event(db, rec, "FULL_EXIT_RESIDUAL_SKIPPED", {
                "amt": round(amt, 8), "ratio": round(ratio, 4),
            })
            rec.raw = {**raw, "residual_skip_notified": True}
            logger.warning(
                "全平模式残仓偏大 %s：剩仓 %s（占开仓量 %.1f%%），疑似手动加仓，不自动补平",
                rec.symbol, amt, ratio * 100,
            )
        return False
    close_qty = rt.round_qty(rec.symbol, amt)
    if close_qty <= 0:
        return False
    try:
        rt.market_order(rec.symbol, _close_side(rec.direction), close_qty,
                        reduce_only=True)
    except Exception as e:  # noqa: BLE001 - 补平失败不阻断巡检，下轮再试
        logger.warning("全平模式残仓补平失败 %s（qty=%s）: %s", rec.symbol, close_qty, e)
        _add_event(db, rec, "FULL_EXIT_RESIDUAL_FAILED", {
            "qty": round(close_qty, 8), "error": str(e)[:200],
        })
        return False
    rec.raw = {
        **raw,
        "full_exit_residual_at": datetime.utcnow().isoformat(),
        "exit_override": "tp_full",
    }
    _add_event(db, rec, "FULL_EXIT_RESIDUAL", {
        "qty": round(close_qty, 8),
        "amt": round(amt, 8),
        "ratio": round(ratio, 4),
    })
    logger.warning(
        "全平模式残仓补平 %s：TP2 后仍余 %s（剩仓比例 %.2f%%），已市价 reduceOnly 平掉",
        rec.symbol, close_qty, ratio * 100,
    )
    return True


# 市价入场确认（2026-10-19 最小加固）：POST 市价单返回 200+orderId 仅表示「已受理」，
# 交易所侧可能瞬时异常导致从未真正落单（AVAUSDT/GTCUSDT 测试网实例：allOrders/
# userTrades 均查无此单）。此情形下无持仓，closePosition 止损单会被 -4509 拒绝，
# 而旧代码误判为「开仓失败」并空转应急平仓。以下常量控制入场成交/持仓确认的短重试。
_ENTRY_FILL_TRIES = 3
_ENTRY_FILL_DELAY = 0.4


def _confirm_position(trader: BinanceTrader, symbol: str,
                      tries: int = _ENTRY_FILL_TRIES, delay: float = _ENTRY_FILL_DELAY) -> bool:
    """挂 SL/TP 前确认交易所侧已有本 symbol 持仓（市价单 ACK≠成交）。

    查询失败（网络/接口异常）保守返回 True——宁可继续走原有「挂 SL/TP（-4509 有短
    重试兜底）」路径，也不误判「未成交」而把真实仓位弃成裸仓。"""
    for attempt in range(tries):
        try:
            if symbol in trader.positions():
                return True
        except Exception as e:
            logger.warning("开仓 %s：持仓确认查询失败（按已开仓继续）: %s", symbol, e)
            return True
        if attempt < tries - 1:
            time.sleep(delay)
    return False


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
        # 评分原始值（T2）：档位归一的原始分随快照自持，复盘可比较档位与实际表现
        "recommendation_raw": _num(getattr(a, "recommendation_raw", None)),
        # 统一决策版本（T5）：配置/补丁/提示词变更切点随快照自持，复盘按版本分组；
        # 本列上线前的旧行 None（归「legacy」）
        "decision_version": getattr(a, "decision_version", None),
        # 4h 快照时刻与降级标记（T1）：交易记录自持，事后可判断开仓时的 4h 时效
        "htf_as_of": a.htf_as_of.isoformat() if getattr(a, "htf_as_of", None) else None,
        "htf_degraded": bool(getattr(a, "htf_degraded", False)),
        # 止损锚类型（docs/23）：structure/near，交易记录随快照自持，复盘分组用
        "stop_anchor": a.stop_anchor,
        # 战法命中标注（2026-10-02，仅借鉴）：随快照自持，删战法不断链，复盘分组用
        "tactic_name": a.tactic_name,
        "tactic_reason": a.tactic_reason,
        # 4h 状态（2026-10-10 五次拍板；2026-10-11 再重构 EMA 状态机 11 态）：
        # tangle/turn_*/macro_cross_*/*_recover/aligned_*/bull_dip/bear_rally，
        # 随快照自持，复盘「4h趋势期」维度分组用；旧行（退役旧枚举）/无 4h 数据 None→「未知」
        "htf_phase": a.htf_phase,
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


_PENDING_ACTIVE = "pending"  # PendingEntryOrder.status 活跃态（挂单入场）


def _pending_acc_cond(account_key: Optional[str], testnet: bool):
    """挂单表账号过滤条件（与 _adopt_acc_cond 同口径：account_key 为空旧行按
    testnet 环境回落，保证按账号查询不漏旧行）。"""
    return or_(
        PendingEntryOrder.account_key == account_key,
        and_(PendingEntryOrder.account_key.is_(None), PendingEntryOrder.testnet == testnet),
    )


def _active_pendings(
    db: Session, account_key: Optional[str], testnet: bool,
    symbol: Optional[str] = None, ai_analysis_id: Optional[str] = None,
) -> list:
    """同账号活跃挂单（可按 symbol / ai_analysis_id 过滤）。"""
    q = select(PendingEntryOrder).where(
        PendingEntryOrder.status == _PENDING_ACTIVE,
        _pending_acc_cond(account_key, testnet),
    )
    if symbol is not None:
        q = q.where(PendingEntryOrder.symbol == symbol)
    if ai_analysis_id is not None:
        q = q.where(PendingEntryOrder.ai_analysis_id == ai_analysis_id)
    return db.execute(q).scalars().all()


def _pending_reserved(db: Session, account_key: Optional[str], testnet: bool) -> tuple[int, float]:
    """同账号活跃挂单的占用：(条数, 预估保证金合计)。

    计入在跑单上限与余额风控——挂单在交易所侧已锁保证金（available 减少），
    不并入口径会在保金/单数闸门上重复分配。
    """
    n, margin = db.execute(
        select(func.count(PendingEntryOrder.id),
               func.coalesce(func.sum(PendingEntryOrder.margin_used), 0.0)).where(
            PendingEntryOrder.status == _PENDING_ACTIVE,
            _pending_acc_cond(account_key, testnet),
        )
    ).one()
    return int(n or 0), float(margin or 0.0)


def _remaining_risk(rec: TradeRecord, qty: Optional[float] = None) -> Optional[float]:
    """当前剩余风险 = 剩余数量 × 入场价到当前止损的亏损距离（USDT）。

    qty 缺省按分批结构推导（与 event_executor.remaining_qty 同源）；结算路径
    传入交易所实际剩余量（amt），手动减仓/部分止损成交同样能反映。止损已在
    盈利侧（含保本缓冲）时按 0 计——已锁定的利润不是当前风险。
    """
    entry = float(rec.entry_price or 0)
    sl = float(rec.stop_loss or 0)
    if entry <= 0 or sl <= 0:
        return None
    if qty is None:
        from app.services.event_executor import remaining_qty
        qty = remaining_qty(rec)
    qty = max(float(qty or 0), 0.0)
    per_unit = (entry - sl) if rec.direction == "long" else (sl - entry)
    return round(max(per_unit, 0.0) * qty, 4)


def _recover_unknown_limit_entry(
    db: Session, trader: BinanceTrader, symbol: str, direction: str,
    qty: float, limit_px: float, entry_err: Exception,
) -> None:
    """限价入场单提交响应丢失后的最坏情况收敛（2026-10-21 审计 7.3）。

    限价单与市价单不同：响应丢失时既可能没送达，也可能挂着未成交，甚至已成交。
    没有 orderId 只能按 symbol 全撤普通挂单（开仓闸门保证本账号同币无其他系统
    挂单），再核对是否已成交——有仓立即 reduceOnly 平掉并落事故交巡检对账，
    绝不留无保护仓位或悬挂单。撤单结果未知时按人工复核处理，巡检每轮重试撤单。
    """
    from app.services.protection_recovery import (
        create_incident_after_rollback, status_for_close_error,
    )
    reason = "open_limit_entry_submit_unknown"

    def _record(*, qty_, error, status, entry_price=limit_px):
        try:
            create_incident_after_rollback(
                db, account_key=trader.account_key, testnet=trader.testnet,
                symbol=symbol, direction=direction, reason=reason,
                qty=qty_, entry_order_id=None, entry_price=entry_price,
                error=error, status=status,
            )
        except Exception:
            logger.exception("限价单响应丢失事故无法持久化 %s", symbol)

    try:
        trader.cancel_all_open_orders(symbol)
    except Exception as cancel_err:
        # 撤单结果未知：不能假定无残留挂单，交人工复核（巡检会重试撤单）。
        # 若已成交，尽量用交易所实际入场价落事故，避免世代校验拿限价误判。
        pos_entry = limit_px
        try:
            pos = trader.positions().get(symbol)
            if pos and abs(float(pos.get("amt") or 0)) > 0:
                pos_entry = float(pos.get("entry_price") or 0) or limit_px
        except Exception:
            pass
        _record(qty_=qty, entry_price=pos_entry,
                error=f"entry={entry_err}; cancel={cancel_err}",
                status="manual_review")
        return
    try:
        pos = trader.positions().get(symbol)
    except Exception as pos_err:
        # 挂单已确认撤掉，仅成交与否未知；巡检按仓位核对，无仓即解除
        _record(qty_=qty, error=f"entry={entry_err}; positions={pos_err}",
                status="pending")
        return
    amt = abs(float((pos or {}).get("amt") or 0))
    if amt <= 0:
        return  # 未成交且撤单成功：无残留，直接按原错误外抛
    pos_entry = float((pos or {}).get("entry_price") or 0) or limit_px
    same_side = (float((pos or {}).get("amt") or 0) > 0) == (direction == "long")
    if not same_side or amt > qty * 1.001:
        # 方向不符或数量超过本单（疑似并发人工仓）：不自动平，交人工
        _record(qty_=amt, entry_price=pos_entry,
                error=f"entry={entry_err}; position={pos}", status="manual_review")
        return
    close_side = "SELL" if direction == "long" else "BUY"
    try:
        close_result = trader.market_order(symbol, close_side, amt, reduce_only=True)
    except Exception as close_err:
        status = status_for_close_error(close_err)
        error = f"entry={entry_err}; close={close_err}"
    else:
        status = "awaiting_reconcile"
        error = f"entry={entry_err}; close_ack={close_result}"
    _record(qty_=amt, entry_price=pos_entry, error=error, status=status)


def _entry_snapshot(
    cfg: SystemConfig, a: AIAnalysis, *,
    price: float, stop_loss: float, direction: str, tp1: float,
    entry_path: str,
) -> dict:
    """入场诊断快照（docs/需求文档/15-实盘止损归因与上线包 B1+O1，2026-10-27）。

    一次 1h K 线请求同时喂两个消费方：① pos 闸（成交价落在信号 K 的位置）；
    ② O1 诊断落库（pos/room_r/stop_atr/planned_rr/quality_total/range_flag）。

    任何失败（交易所瞬断/ATR 样本不足/字段缺失）都返回 {}，由调用方按
    "无诊断"处理：诊断是观测手段，绝不能因为观测故障阻断真实开仓。
    """
    try:
        from app.services.entry_diagnostics import snapshot
        from app.services.exchange_pool import ExchangePool

        interval = (getattr(cfg, "kline_interval", None)
                    or getattr(settings, "KLINE_INTERVAL", None) or "1h")
        klines = ExchangePool().get_klines(a.symbol, interval, 60, allow_stale=True)
        scan = getattr(a, "scan_result", None)
        return snapshot(
            klines=klines,
            price=float(price),
            stop_loss=float(stop_loss),
            direction=direction,
            tp1=float(tp1),
            entry_price=(float(a.entry_price) if a.entry_price is not None else None),
            entry_path=entry_path,
            quality_parts=(scan.quality_parts if scan is not None else None),
            signal_type=(scan.signal_type if scan is not None else None),
            interval=interval,
        )
    except Exception:  # noqa: BLE001 - 诊断失败降级，不影响开仓主链路
        logger.warning("入场诊断快照失败 %s（按无诊断继续开仓）", a.symbol, exc_info=True)
        return {}


def _attempt_open(
    db: Session, trader: BinanceTrader, cfg: SystemConfig,
    a: AIAnalysis,
    wallet: float, positions: dict,
    force_market: bool = False,
    beta: Optional[float] = None,
    available: Optional[float] = None,
) -> str:
    """对单条分析过全部开仓闸门并尝试开仓（docs/06 §3 逐项检查）。

    返回开仓结局："opened"（市价成交并建单）/ "pending"（现价盈亏比不足，
    已在入场价挂限价单等待入场，docs/27）/ "blocked"（闸门拦截或失败）。
    市价成功时更新 positions（供调用方累计在跑数与占用保证金）；挂单不建
    TradeRecord、不占 positions（成交由 materialize_pending_entry 落地）。
    即时开仓唯一入口（try_open_for_analysis，分析完成即分发）调用；
    开仓失败统一 rollback 并落 FAILED 记录（仅 -1121 未上架）。

    force_market（2026-09-28 手动开单语义）：跳过入场偏离/待回踩时机闸门，
    以现价市价立即入场——等待回踩是自动成交的时机安排，手动点击是用户对
    当下价格的明确意图；其余风控闸门（RR 现价复算 / SL-TP 可挂性 / 保证金 /
    滑点 / 上限 / 黑名单）全部保留。

    available（可选）：交易所 availableBalance（真实可用保证金）。传入则保证金
    闸门以它为准（权威口径，避免 -2019）；缺省（None，测试替身直接传 wallet）
    回落本地估算 wallet - occupied。
    """
    # 占用保证金含同账号活跃挂单（挂单在交易所侧已锁保证金，见 _pending_reserved）
    _pend_n, _pend_margin = _pending_reserved(db, trader.account_key, trader.testnet)
    occupied = sum(p["initial_margin"] for p in positions.values()) + _pend_margin
    symbol = a.symbol
    if symbol in positions:
        logger.info("开仓跳过 %s：已有持仓", symbol)
        _mark_open_block(db, a, "同币种已有在跑持仓")
        return "blocked"
    # 挂单哨兵（2026-10-17，docs/27）：同币种已有活跃挂单时——同分析重复点击
    # 幂等返回 pending（不重复挂单）；不同分析则拦截（一币一挂单，避免同币
    # 挂出多张限价单，成交后超仓）
    _pend_same_sym = _active_pendings(db, trader.account_key, trader.testnet, symbol=symbol)
    if _pend_same_sym:
        if any(pe.ai_analysis_id == a.id for pe in _pend_same_sym):
            logger.info("开仓 %s：该分析已有挂单等待入场，幂等返回 pending", symbol)
            return "pending"
        logger.info("开仓跳过 %s：同币种已有挂单等待入场", symbol)
        _mark_open_block(db, a, "同币种已有挂单等待入场")
        return "blocked"
    # 黑名单兜底（2026-09-18）：扫描宇宙过滤（主过滤）与开仓之间的时间差内，
    # 币可能被手动拉黑或刚触发自动拉黑——开仓前最后核对一次（D 双保险）
    from app.services.blacklist_service import active_symbols
    if symbol in active_symbols(db):
        logger.info("开仓跳过 %s：币种在黑名单（手动/连续止损自动）", symbol)
        _mark_open_block(db, a, "币种在黑名单（手动添加或连续止损自动冷却中）")
        return "blocked"
    # 下单环境可交易校验（2026-09-19，ARX 案例；2026-10-11 升级为唯一校验点）：
    # 扫描期 C0 过滤已撤（用户拍板——未上架币照常分析，AI 配额换取全市场可见性），
    # 开单时按本单账号网络逐网校验：未上架 → 拒开 + 落原因（BLOCK_UNTRADABLE），
    # 任务层扇出后锁外发飞书提示（正式网有该合约的账号正常开单走常规卡片）。
    # 空集合（拉取失败）放行不阻断
    listed = trader.listed_symbols()
    if listed and symbol not in listed:
        logger.info("开仓跳过 %s：下单环境未上架该合约（exchangeInfo 校验）", symbol)
        _mark_open_block(db, a, BLOCK_UNTRADABLE)
        return "blocked"
    # 开单类型仅作记录（2026-09-15 拍板）：策略开关不再拦截开仓（未知类型也不拦）
    # EMA 排列前提随指标多数决重设计移除（2026-09-17）：方向判定权已移交方向票多数决
    # （方向票同向 ≥2 才成候选），与 AI 提示词、扫描筛选同一把尺子，开仓侧不再二次设限
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
    try:
        price = trader.price(symbol)
        # 挂单入场模式（docs/27）：默认市价；RR 闸门处若现价盈亏比不足则切换为
        # "limit" 并把仓位计算基准价从现价下沉到 AI 入场价（ref_price=entry_ai）
        order_mode = "market"
        ref_price = price
        # 风控参数解析（2026-10-02 上页面）：system_config 列可页面覆盖，
        # NULL（页面留空）= 回落 env 默认；本单内取值一致
        rr_min = (
            float(cfg.ai_rr_min) if cfg.ai_rr_min is not None
            else float(settings.AI_RR_MIN)
        )
        rr_tol = (
            float(cfg.ai_rr_tol) if cfg.ai_rr_tol is not None
            else float(settings.AI_RR_TOL)
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
            return "blocked"
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
            return "blocked"
        # 盈亏比按现价复算（2026-09-18 补上，此前仅 AI 入场价口径校验）：市价成交
        # 价就是现价，待回踩单/漂移单偏离 AI 入场价后风险变宽、盈利变窄，RR 随之
        # 退化——按现价复算仍须 ≥盈亏比铁律下限（ai_rr_min 可页面覆盖），否则市价进不划算。
        # 容差带（2026-10-11-3，v2026-10-11-3）：rr_now ≥ rr_min - ai_rr_tol 即放行——
        # CYBERUSDT 案例：AI 三价 RR 1.57 达标，3 分钟漂移后现价复算 1.4998 被 1.5 拦，
        # 0.0002 级漂移误杀信号。初期限本闸（容忍 AI 报价到开仓间的执行漂移）；
        # 2026-10-14-3 起同一条容差带扩到 AI 入场价口径铁律与 TP1 上限
        # （risk_guard.validate_decision，吞 tick 取整擦边）。放行时落 task log 审计。
        if sl != price:
            rr_now = abs(tp1 - price) / abs(price - sl)
            if rr_now < rr_min - rr_tol:
                if settings.PENDING_ENTRY_ENABLED:
                    # 挂单入场（docs/27）：现价 RR 不足 ≠ 计划不达标——AI 入场价口径
                    # 仍自洽，只是现价已向有利侧漂移、此刻市价进场盈亏比退化。改为在
                    # AI 入场价挂 GTC 限价单等待回踩成交，仓位计算基准价随之下沉到
                    # 入场价（ref_price）——成交价即限价，RR/仓位须按真实成交口径自洽。
                    # 多头 rr_now<rr_ai ⟺ 现价>入场价、空头对称，故限价单必然在现价
                    # 回踩侧，天然被动挂单不会立即成交。
                    order_mode = "limit"
                    ref_price = entry_ai
                    logger.info(
                        "开仓 %s：盈亏比按现价复算 %.2f < %s-容差%s=%.2f（入场 %s，现价 %s），"
                        "改在入场价挂限价单等待入场（docs/27）",
                        symbol, rr_now, rr_min, rr_tol, rr_min - rr_tol, entry_ai, price,
                    )
                else:
                    logger.info(
                        "开仓跳过 %s：盈亏比按现价复算 %.2f < %s-容差%s=%.2f（入场 %s，现价 %s）",
                        symbol, rr_now, rr_min, rr_tol, rr_min - rr_tol, entry_ai, price,
                    )
                    _mark_open_block(
                        db, a,
                        f"盈亏比按现价复算 {rr_now:.2f} < {rr_min:g}-容差{rr_tol:.2f}"
                        f"={rr_min - rr_tol:.2f}（入场 {entry_ai}，现价 {price}），市价进不满足盈亏计划",
                    )
                    return "blocked"
            if rr_now < rr_min:
                logger.info(
                    "开仓 %s：盈亏比按现价复算 %.2f 落在容差带 [%.2f, %.2f)，容差放行",
                    symbol, rr_now, rr_min - rr_tol, rr_min,
                )
        # 入场位置闸 + 诊断快照（docs/需求文档/15 B1/O1，2026-10-27）：pos =
        # (成交价−low)/(high−low) 在信号 K（最后一根已收盘 1h K）中的相对位置。
        # 正式网 56 笔实测：pos≥0.6 的 22 笔 63.6% 胜 +18.42R；pos<0.6 的 34 笔
        # 17.6% 胜 −14.30R。市价路径（15 笔 +15.48 / 18 笔 −8.59）与挂单路径
        # （7 笔 +2.94 / 16 笔 −5.72）同向，故两条路径出手前都拦——挂单模式用
        # 限价（=AI 入场价）算 pos，市价模式用现价算。位于 RR/仓位闸之后：
        # 前面那些闸门不通过的单子不必为此多花一次 K 线请求。
        pos_min = float(getattr(settings, "ENTRY_POS_MIN_RATIO", 0.0) or 0.0)
        entry_path = "limit" if order_mode == "limit" else "market"
        entry_diag: dict = {}
        if pos_min > 0 or bool(getattr(settings, "ENTRY_DIAG_ENABLED", False)):
            entry_diag = _entry_snapshot(
                cfg, a, price=float(ref_price), stop_loss=sl,
                direction=direction, tp1=tp1, entry_path=entry_path,
            )
        pos_now = entry_diag.get("pos")
        if pos_min > 0 and pos_now is not None and float(pos_now) < pos_min:
            if force_market:
                logger.info(
                    "开仓 %s：入场位置 pos=%.2f < %.2f，手动开单按用户意图放行（entry_pos 闸）",
                    symbol, float(pos_now), pos_min,
                )
            else:
                logger.info(
                    "开仓跳过 %s：入场位置 pos=%.2f < %.2f（成交价贴信号 K 低端，"
                    "路径 %s，入场价 %s，stop_dist %s）",
                    symbol, float(pos_now), pos_min, entry_path,
                    ref_price, entry_diag.get("stop_dist"),
                )
                _mark_open_block(
                    db, a,
                    f"入场位置 pos={float(pos_now):.2f} < {pos_min:g}（成交价落在信号 K "
                    f"下半部，做多贴低点/做空贴高点），entry_pos 闸拦截",
                )
                return "blocked"
        # 固定亏损仓位：基数分档 × 3% ÷ 止损距离（基准价 ref_price：市价模式=现价，
        # 挂单模式=AI 入场价——成交口径即限价，见上 RR 闸门）
        stop_pct = abs(ref_price - sl) / ref_price
        if stop_pct <= 0:
            # 原"方向-价格一致性/RR 复验"闸门（2026-09-15 移除）曾顺带拦住 sl==现价；
            # 现在显式防零：止损距离为 0 无法按固定亏损法计算仓位
            logger.info("开仓放弃 %s：止损距离为 0（sl=%s，基准价 %s）", symbol, sl, ref_price)
            _mark_open_block(db, a, f"止损距离为 0（sl={sl}，基准价 {ref_price}），无法计算仓位")
            return "blocked"
        # 成本感知计价（2026-10-27 F1）：折旧前的分母只含价格距离，触止损时还要付
        # 双边 taker 费——按含费距离定仓位，实际止损亏损才回到风险预算
        fee_rate = float(settings.TAKER_FEE_RATE)
        stop_pct_eff = _cost_adjusted_stop_pct(ref_price, sl, fee_rate)
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
                return "blocked"
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
        # 滑点上限（补丁参数 max_slippage_pct，NULL=默认 0.3%）：既用于盘口滑点闸门，
        # 也作为强平闸门的余量——止损触发后是市价成交，只比纯止损距离会在极端行情
        # 下「强平先于止损」（2026-10-27 F1）
        slip_max = (
            float(cfg.max_slippage_pct)
            if getattr(cfg, "max_slippage_pct", None) is not None else MAX_SLIPPAGE_PCT
        )
        lever_stop_pct = stop_pct_eff + max(slip_max, 0.0)
        mmr_1 = trader.bracket_mmr(symbol)
        mmr_eff = mmr_1 if mmr_1 is not None else LIQ_GATE_MMR_FALLBACK
        # 限制层（2026-09-18）：动态杠杆下限，页面补丁参数 min_adapt_leverage 可覆盖
        # （NULL=默认 config.TRADING_LEVERAGE_MIN）——不是所有宽止损单都算完就开，
        # 安全杠杆低于下限的直接放弃
        lev_min = (
            int(cfg.min_adapt_leverage)
            if cfg.min_adapt_leverage is not None else settings.TRADING_LEVERAGE_MIN
        )
        leverage = _adaptive_leverage(lever_stop_pct, mmr_eff, liq_buf, lev_cap=lev_cap)
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
            return "blocked"
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
        notional = risk_budget / stop_pct_eff
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
            return "blocked"
        # 杠杆上限取三者最小：配置上限 / 档位最大杠杆 / 精确档 MMR 下的强平安全杠杆；
        # 低于下限 lev_min 仍放弃（限制层不变）
        cap_reason = None
        if lev_max is not None and leverage > lev_max:
            cap_reason = f"名义 {notional:.0f} 档位最大杠杆 {lev_max}×"
            leverage = lev_max
        lev_exact = _adaptive_leverage(lever_stop_pct, mmr_exact, liq_buf, lev_cap=lev_cap)
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
            return "blocked"
        if cap_reason:
            logger.info(
                "开仓 %s：杠杆下调至 %s×（%s，强平距离 %.2f%%）",
                symbol, leverage, cap_reason,
                liquidation_gate_pct(leverage=leverage, mmr=mmr_exact, buffer=liq_buf) * 100,
            )
        qty = trader.round_qty(symbol, notional / ref_price)
        f = trader.filters(symbol)
        if qty < f["min_qty"] or qty * ref_price < f["min_notional"]:
            logger.info(
                "开仓放弃 %s：计算数量 %s 低于交易所最小规则（min_qty=%s, min_notional=%s）",
                symbol, qty, f["min_qty"], f["min_notional"],
            )
            _mark_open_block(
                db, a,
                f"计算数量 {qty} 低于交易所最小规则（min_qty={f['min_qty']}, min_notional={f['min_notional']}）",
            )
            return "blocked"
        # 市价入场按 MARKET_LOT_SIZE 校验（常比 LOT_SIZE 严得多，BBUSDT -4005 案例：
        # LOT_SIZE 100万 / MARKET_LOT_SIZE 5万），条件单按 LOT_SIZE；取两者更严的上限
        max_qty = min(f["max_qty"], f.get("market_max_qty", f["max_qty"]))
        if qty > max_qty:
            logger.info("开仓 %s：数量 %s 超交易所单笔上限 %s，按上限缩减（实际风险低于 %.0f%% 预算）",
                        symbol, qty, max_qty, risk_pct)
            qty = trader.round_qty(symbol, max_qty)
        # 开仓保证金需求 = 初始保证金 + 开仓 taker 手续费（2026-10-19 -2019 加固）：
        # 交易所按「初始保证金 + 手续费」之和校验可用余额，此前只比初始保证金，
        # 可用恰好等于 margin_used 时被交易所按「+手续费」拒单（-2019）。
        margin_needed = _open_margin_required(qty, ref_price, leverage)
        # 可用保证金闸门（2026-10-17 -2019 修复）：优先用交易所 availableBalance——
        # 本地「钱包余额 - 估算占用」会高估可用（漏掉交易所侧非本系统挂单冻结、
        # 逐仓已分配保证金、未实现亏损对可用额度的影响），本地放行后交易所按真实
        # 可用余额拒单（-2019 Margin is insufficient）。无交易所口径时回落本地估算
        avail = available if available is not None else wallet - occupied
        if margin_needed > avail:
            # 三值分解（2026-10-17）：钱包余额高 ≠ 可用够——保证金可能已被其它持仓/
            # 挂单冻结（availableBalance 已扣除全部持仓初始保证金与挂单冻结）。给出
            # 钱包/占用明细，避免"钱包还有钱为什么开不了"的困惑
            src = "交易所可用" if available is not None else "本地估算可用"
            logger.info(
                "开仓放弃 %s：所需保证金 %.2f 超过可用 %.2f（%s；钱包 %.2f · 已占用 %.2f）",
                symbol, margin_needed, avail, src, wallet, wallet - avail,
            )
            _mark_open_block(
                db, a,
                f"可用保证金不足（{symbol}）：需 {margin_needed:.2f}，{src} {avail:.2f}"
                f"（钱包 {wallet:.2f}，已占用约 {wallet - avail:.2f}）",
            )
            return "blocked"
        # 盘口滑点实测闸门（GRIFFAIN 案例，2026-09-18）：按本单数量走订单簿逐层吃，
        # 预期滑点超 max_slippage_pct（补丁参数，NULL=默认 0.3%）放弃——簿薄的小币
        # 吃单会撑宽止损距离、实际风险超预算（0.637% 滑点 → 风险多 17%）。成交额
        # 门槛拦不住这类币（拉盘时 24h 成交额照样过线）。簿异常 None 不阻断
        if order_mode == "market" and slip_max > 0:
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
                return "blocked"
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
                    return "blocked"
            except Exception as spread_err:
                logger.warning(
                    "开仓 %s：盘口价差预检查询失败，放行（走滑点实测兜底）: %s",
                    symbol, spread_err,
                )
        # TP1 分批数量预检（下单前）：低于交易所最小数量时整单放弃——
        # 开完仓才挂 TP1 被交易所拒单，会走紧急撤单+平仓，且该失败不落记录，
        # 候选每小时重试"开仓→拒单→平仓"空转（docs/07 §8-A1）。TP2 本就有
        # min_qty 检查（不足时静默跳过该档），TP1 是主止盈档、缺它无意义。
        # 须传 min_qty（T14f，2026-10-30）：全平模式 TP2 挂不上时 TP1 会提到整仓，
        # 此时 60% 低于 min_qty 不代表挂不上，不传会把「可全平的小仓」误拦
        qty_tp1_pre, _, _tp_full = _tp_order_quantities(
            trader, symbol, qty, tp2, cfg, min_qty=f["min_qty"])
        if qty_tp1_pre < f["min_qty"]:
            logger.info(
                "开仓放弃 %s：止盈一数量 %s 低于交易所最小数量 %s，分批止盈无法挂单",
                symbol, qty_tp1_pre, f["min_qty"],
            )
            _mark_open_block(
                db, a,
                f"止盈一数量 {qty_tp1_pre} 低于交易所最小数量 {f['min_qty']}，分批止盈无法挂单",
            )
            return "blocked"
        # 实际风险金额按最终数量精确计（向下取整/上限缩减只会低于 3% 预算）
        risk_amount = round(qty * abs(ref_price - sl), 4)

        # 下单：市价开仓 → 挂 SL/TP1/TP2 → 入库，全在保护块内：
        # 任何一步失败都紧急撤单+平仓，不留裸仓（含记录构建/入库失败）
        trader.setup_leverage(symbol, leverage)
        side = "BUY" if direction == "long" else "SELL"
        close_side = _close_side(direction)
        if order_mode == "limit":
            # 挂单入场分支（docs/27）：只在 AI 入场价挂 GTC 限价单——不建 TradeRecord、
            # 不挂 SL/TP、不占 positions（成交前不污染结算/在跑计数）；成交后由
            # materialize_pending_entry 补挂 SL/TP 并落 TradeRecord，1 小时未成交由
            # watch_pending_entries 撤单清除。
            limit_px = trader.round_price(symbol, entry_ai)
            ok_side = (limit_px < price) if direction == "long" else (limit_px > price)
            if not ok_side:
                logger.info(
                    "开仓放弃 %s：入场价 %s 取整后 %s 不在现价 %s 的正确侧，放弃挂单",
                    symbol, entry_ai, limit_px, price,
                )
                _mark_open_block(
                    db, a,
                    f"入场价 {entry_ai} 取整后 {limit_px} 不在现价 {price} 的正确侧，放弃挂单",
                )
                return "blocked"
            try:
                order = trader.limit_order(symbol, side, qty, limit_px)
            except Exception as entry_err:
                # 响应丢失（超时/断连）≠ 未挂出：撤残留挂单并按最坏情况核对成交，
                # 明确拒单（含币安错误码）意味着未送达，走原路径直接外抛。
                if re.search(r"(?<!\d)-\d{4}(?!\d)", str(entry_err)):
                    raise
                _recover_unknown_limit_entry(
                    db, trader, symbol, direction, qty, limit_px, entry_err)
                raise
            pe = PendingEntryOrder(
                symbol=symbol, direction=direction,
                scan_result_id=a.scan_result_id, ai_analysis_id=a.id,
                account_key=trader.account_key, testnet=trader.testnet,
                rule_version=settings.RULE_VERSION,
                entry_price=limit_px, stop_loss=sl, tp1=tp1, tp2=tp2,
                qty=qty, leverage=leverage,
                margin_used=round(qty * limit_px / leverage, 4),
                risk_amount=round(qty * abs(limit_px - sl), 4),
                limit_order_id=str(order["orderId"]), status="pending",
                expires_at=datetime.utcnow() + timedelta(
                    minutes=int(settings.PENDING_ENTRY_TTL_MINUTES)),
                raw={
                    "wallet": wallet, "capital_base": _capital_base(wallet),
                    "risk_budget_mode": (cfg.risk_budget_mode or "pct"),
                    "beta": (round(beta, 4) if beta is not None else None),
                    "ai_snapshot": _ai_snapshot(a),
                    "entry_order_id": str(order["orderId"]),
                    # 入场诊断快照（O1）：挂单期即落，成交落地时随 trade_records 带走
                    "entry_diag": (entry_diag or None),
                },
            )
            try:
                db.add(pe)
                db.commit()
            except Exception:
                # 入库失败即撤掉刚挂出的交易所限价单，不留悬挂单（防孤儿买单）
                db.rollback()
                try:
                    trader.cancel_regular_order(symbol, int(order["orderId"]))
                except Exception as cancel_err:
                    logger.error("挂单入库失败后撤单失败 %s: %s", symbol, cancel_err)
                raise
            logger.info(
                "已挂单等待入场 %s %s 限价=%s qty=%s sl=%s tp1=%s tp2=%s（现价 %s，"
                "%s 分钟未成交自动撤销，docs/27）",
                symbol, direction, limit_px, qty, sl, tp1, tp2, price,
                int(settings.PENDING_ENTRY_TTL_MINUTES),
            )
            _mark_open_block(
                db, a,
                f"现价盈亏比不足，已在入场价 {limit_px} 挂单等待入场"
                f"（{int(settings.PENDING_ENTRY_TTL_MINUTES)} 分钟未成交自动撤销）",
            )
            return "pending"
        # 入场单提交（2026-10-21 审计 7.3）：响应丢失 ≠ 未成交。超时/断连时订单
        # 可能已送达并成交，而此刻还没有 orderId、也没有 TradeRecord/SL——若直接
        # 外抛，交易所侧会留下无保护的裸仓。按最坏情况处理：立即 reduceOnly 平仓
        # （无仓会被拒、有仓归零），并持久化事故交巡检对账释放账号封锁。
        # 消息含币安错误码（-1xxx/-2xxx/-4xxx）说明是明确拒单、未送达，走原路径。
        try:
            entry_order = trader.market_order(symbol, side, qty)
        except Exception as entry_err:
            if re.search(r"(?<!\d)-\d{4}(?!\d)", str(entry_err)):
                raise
            from app.services.protection_recovery import (
                create_incident_after_rollback, status_for_close_error,
            )
            try:
                close_result = trader.market_order(
                    symbol, close_side, qty, reduce_only=True)
            except Exception as close_err:
                status = status_for_close_error(close_err)
                error = f"entry={entry_err}; close={close_err}"
            else:
                status = "awaiting_reconcile"
                error = f"entry={entry_err}; close_ack={close_result}"
            # 有仓时用交易所实际入场价落事故：响应丢失只能拿到参考价，测试网盘口薄
            # 时实际成交可能偏离 >0.1%，用参考价会让巡检的世代校验永远对不上。
            incident_entry = float(price)
            try:
                live = trader.positions().get(symbol)
                if live and abs(float(live.get("amt") or 0)) > 0:
                    incident_entry = float(live.get("entry_price") or 0) or incident_entry
            except Exception:
                pass
            try:
                create_incident_after_rollback(
                    db, account_key=trader.account_key, testnet=trader.testnet,
                    symbol=symbol, direction=direction,
                    reason="open_entry_submit_unknown", qty=qty,
                    entry_order_id=None, entry_price=incident_entry,
                    error=error, status=status,
                )
            except Exception:
                logger.exception("入场单响应丢失事故无法持久化 %s", symbol)
            raise
        # 成交回报：下单响应为 ACK 不含均价，市价单即时成交后查实际成交价/量。
        # 滑点 = 成交价对参考价的偏离（testnet 盘口薄，深吃单可达 0.5%+）；
        # 查询失败按参考价记账，不阻断挂 SL/TP（保护块兜底仍在）
        fill_price, fill_qty = price, qty
        od = None
        for attempt in range(_ENTRY_FILL_TRIES):
            try:
                od = trader.order(symbol, entry_order["orderId"])
                break
            except Exception as fill_err:
                # -2013（Order does not exist）：市价单已 ACK 但交易所侧尚未落账
                # （2026-10-19 实例），短退避重试；其余错误不重试，直接放弃查询
                if attempt >= _ENTRY_FILL_TRIES - 1 or "-2013" not in str(fill_err):
                    logger.warning("开仓 %s：成交回报查询失败，按参考价 %s 记账（%s）",
                                   symbol, price, fill_err)
                    break
                time.sleep(_ENTRY_FILL_DELAY)
        if od is not None and float(od.get("executedQty") or 0) > 0:
            fill_qty = float(od["executedQty"])
            if float(od.get("avgPrice") or 0) > 0:
                fill_price = float(od["avgPrice"])
        else:
            # 成交未确认（查不到成交单或成交量为 0）：挂 SL/TP 前先确认交易所侧已有持仓
            # （2026-10-19 最小加固）。市价单 ACK≠成交——交易所侧瞬时异常时无持仓，
            # closePosition 止损单会被 -4509 拒绝（AVAUSDT/GTCUSDT 实例）。确认无持仓即
            # 判定入场未成交，放弃本币开仓（不挂 SL/TP、不应急平仓），避免空转与裸仓误判
            if not _confirm_position(trader, symbol):
                # 放弃前先尽力撤掉入场的普通单（幂等：-2011/-2013 静默），防「判定未成交后
                # 又迟到成交」造裸仓；随后复核成交——撤单失败于已成交单（-2011 被静默吞掉）
                # 时，此处能查到成交并转按已开仓继续，绝不把真实仓位弃成裸仓。复核查询
                # 自身失败时同样保守继续（不 bail），宁可多挂一次保护单也不误弃仓位
                try:
                    trader.cancel_regular_order(symbol, int(entry_order["orderId"]))
                except Exception as cancel_err:
                    logger.warning("开仓 %s：撤销未成交入场单失败（忽略）: %s", symbol, cancel_err)
                recheck = None
                try:
                    recheck = trader.order(symbol, entry_order["orderId"])
                except Exception as re_err:
                    logger.warning("开仓 %s：放弃前成交复核失败，保守继续挂保护单（%s）",
                                   symbol, re_err)
                if recheck is not None and float(recheck.get("executedQty") or 0) <= 0:
                    logger.warning(
                        "开仓 %s：市价入场单 %s 交易所侧无持仓/无成交，放弃本币开仓",
                        symbol, entry_order.get("orderId"),
                    )
                    _mark_open_block(
                        db, a, "市价入场单未成交（交易所侧无持仓），放弃本币开仓（未挂 SL/TP）")
                    return "blocked"
                if recheck is not None:
                    fill_qty = float(recheck["executedQty"])
                    if float(recheck.get("avgPrice") or 0) > 0:
                        fill_price = float(recheck["avgPrice"])
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
            qty_tp1, qty_tp2, _tp_full = _tp_order_quantities(
                trader, symbol, qty, tp2, cfg, min_qty=f["min_qty"])
            tp1_order = trader.take_profit_reduce(
                symbol, close_side, qty_tp1, trader.round_price(symbol, tp1))
            tp2_order = None
            # 全平模式允许两档合计恰好等于全仓；关闭时须严格小于（保留跟进尾仓）
            if tp2 > 0 and qty_tp2 >= f["min_qty"] and _tp_sum_ok(
                    qty_tp1, qty_tp2, qty, _tp_full):
                tp2_order = trader.take_profit_reduce(
                    symbol, close_side, qty_tp2, trader.round_price(symbol, tp2))
            # TP2 未挂上诊断（docs/23 T14d，2026-10-29）：AI 报了 TP2 却因数量
            # 取整/合计校验没挂上条件单时，rec.tp2 会被静默写成 0；原因另存
            _tp2_skip = _tp2_skip_info(
                tp2, tp2_order, qty_tp1, qty_tp2, qty, f["min_qty"], _tp_full)
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
                # 三口径（2026-10-21 T3）：risk_amount 兼容列写成交初始风险（旧消费方
                # 的 1R 口径），计划口径与剩余口径分列；滑点超预算不再污染 R 分母
                risk_amount=round(actual_risk, 4),
                planned_risk=round(risk_amount, 4),
                initial_risk=round(actual_risk, 4),
                remaining_risk=round(actual_risk, 4),
                risk_budget_mode=(cfg.risk_budget_mode or "pct"),  # 开仓时刻模式快照（追溯口径）
                beta=(round(beta, 4) if beta is not None else None),  # 开仓时 β 快照（docs/13 #1 热度加权；None=不可得按 1.0 计）
                stop_loss=sl,
                tp1=rec_tp1,  # tp1 恒为挂档止盈价（2026-10-12 起无纯跟踪单）
                tp2=tp2 if tp2_order else 0,
                trailing_mode=TRAIL_MODE_THREE_LINE,  # 新开单默认三线止损（2026-10-21 拍板改回；HL/LH 保留可切换）
                status="OPENED",
                opened_at=now,
                raw={
                    # SL/TP 为 algo 条件单，标识是 algoId（MARKET 入场单仍是 orderId）
                    "entry_order_id": entry_order["orderId"],
                    "sl_order_id": sl_order.get("algoId") or sl_order.get("orderId"),
                    "tp1_order_id": (tp1_order.get("algoId") or tp1_order.get("orderId")) if tp1_order else None,
                    "tp2_order_id": (tp2_order.get("algoId") or tp2_order.get("orderId")) if tp2_order else None,
                    "qty_tp1": qty_tp1,
                    "qty_tp2": qty_tp2 if tp2_order else 0,
                    # 止盈全平模式快照（2026-10-28）：巡检结算据本单口径推断
                    # 「TP 档平完全仓」出场，不随期间配置改动漂移
                    "tp_full_exit": _tp_full,
                    # TP2 未挂上原因（docs/23 T14d）：区分「AI 没报」与「报了没挂上」
                    "tp2_skipped": _tp2_skip,
                    "capital_base": _capital_base(wallet),
                    "wallet": wallet,
                    # 价差盈亏书签（REALIZED_PNL 累计，仅价差不含费用）：
                    # TP1/TP2 成交事件用"当前累计 − 书签"算本档盈亏
                    "realized_bookmark": 0,
                    # 入场诊断快照（O1，2026-10-27）：pos/room_r/stop_atr/planned_rr/
                    # quality_total/range_flag 等，归因时免重新拉 K 线重算
                    "entry_diag": (entry_diag or None),
                    # 市价路径的快照就是在成交时算的（ref_price=现价），故与挂单落地
                    # 重算同源，统一标 placement；挂单落地重算成功的标 fill（见
                    # materialize_pending_entry），两者便于事后区分"算于下单还是成交"。
                    "entry_diag_source": ("placement" if entry_diag else None),
                },
            )
            db.add(rec)
            db.flush()
            _add_event(db, rec, "OPEN", {
                "price": price, "qty": qty, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
                "risk_amount": round(actual_risk, 4), "entry_order_id": entry_order["orderId"],
                "planned_risk": round(risk_amount, 4),
                "initial_risk": round(actual_risk, 4),
                "remaining_risk": round(actual_risk, 4),
                "fill_price": fill_price, "slippage_pct": round(slippage_pct, 3),
                "actual_risk": round(actual_risk, 4),
            })
            # 报了吗/挂上了吗分开记（docs/23 T14d）：静默把 tp2 写 0 会让复盘
            # 误判成「策略本来就不要 TP2」
            if _tp2_skip:
                _add_event(db, rec, "TP2_SKIPPED", _tp2_skip)
                logger.warning("开仓 %s：AI 报 TP2=%s 但未挂上（%s）",
                               symbol, _tp2_skip["tp2"], _tp2_skip["reason"])
            db.commit()
        except Exception as place_err:
            # -4509（TIF=GTE 仅可用于已有持仓）是「交易所侧无持仓」的可靠信号，发生在
            # 入场单 ACK 未落单时（2026-10-19 最小加固）：不做 reduceOnly 应急平仓（会
            # 白付两次市价手续费，且真实竞态下可能误杀已成交好单）；仅清理可能已挂出的
            # 残余条件单，落拒开原因后按「未开仓」返回，交由下一轮候选重试。
            # 注意：只认 -4509——-2013（Order does not exist）在 POST 建单语境下不成立，
            # 若据此旁路应急平仓会误弃真实持仓
            if "-4509" in str(place_err):
                logger.warning(
                    "开仓 %s：挂保护单遇无持仓错误（%s），判定入场未成交，放弃本币开仓",
                    symbol, place_err,
                )
                try:
                    trader.cancel_all_algo(symbol)
                except Exception as cancel_err:
                    logger.warning("开仓 %s：清理残余条件单失败（忽略）: %s", symbol, cancel_err)
                _mark_open_block(
                    db, a, f"市价入场单未成交（{str(place_err)[:120]}），放弃本币开仓")
                return "blocked"
            # 紧急处理：先撤全部挂单再只减仓平仓（reduceOnly：开仓单未成交时会被
            # 交易所拒绝，而不是反向开成裸仓）
            try:
                # Do not cancel protective orders before a close request: if close fails,
                # an existing SL is still valuable protection. The incident persists
                # after the outer rollback and the reconciler decides whether retry is safe.
                close_result = trader.market_order(symbol, close_side, qty, reduce_only=True)
            except Exception as close_err:
                logger.error("紧急平仓失败 %s: %s", symbol, close_err)
                from app.services.protection_recovery import (
                    create_incident_after_rollback, status_for_close_error,
                )
                try:
                    create_incident_after_rollback(
                        db, account_key=trader.account_key, testnet=trader.testnet,
                        symbol=symbol, direction=direction,
                        reason="open_protection_failed", qty=qty,
                        entry_order_id=entry_order.get("orderId"), entry_price=fill_price,
                        error=f"protection={place_err}; close={close_err}",
                        status=status_for_close_error(close_err),
                    )
                except Exception:
                    logger.exception("开仓保护事故无法持久化 %s", symbol)
            else:
                # ACK is not proof of flat; block new entries until position reconciliation.
                from app.services.protection_recovery import create_incident_after_rollback
                try:
                    create_incident_after_rollback(
                        db, account_key=trader.account_key, testnet=trader.testnet,
                        symbol=symbol, direction=direction,
                        reason="open_protection_failed", qty=qty,
                        entry_order_id=entry_order.get("orderId"), entry_price=fill_price,
                        error=f"protection={place_err}; close_ack={close_result}",
                        status="awaiting_reconcile",
                    )
                except Exception:
                    logger.exception("开仓平仓待对账事故无法持久化 %s", symbol)
            raise

        positions[symbol] = {"amt": qty if direction == "long" else -qty,
                             "initial_margin": qty * fill_price / leverage,
                             "entry_price": fill_price}
        logger.info("已开仓 %s %s qty=%s 成交=%s（对参考价 %s 滑 %.2f%%）sl=%s tp1=%s tp2=%s（评分 %s）",
                    symbol, direction, qty, fill_price, price, slippage_pct,
                    sl, tp1, tp2, a.recommendation)
        return "opened"
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
        return "blocked"


def _pending_gate_breaches(
    db: Session, pe: PendingEntryOrder, cfg: SystemConfig,
    risk_amount: float, wallet: float,
) -> list[str]:
    """挂单成交落地时的开仓闸门复核（2026-10-27 F3）。

    挂单存续期间组合热度/同向集中度/当日熔断可能已被其它成交推过线。
    成交已发生，无法回退；本函数只返回越线原因，调用方记 GATE_BREACH 事件
    + warning，保护单仍照常挂出（绝不留裸仓）。wallet 取挂单创建时快照，
    缺失（旧行）则不复核。
    """
    if wallet <= 0 or risk_amount <= 0:
        return []
    reasons: list[str] = []
    acc_cond = or_(
        TradeRecord.account_key == pe.account_key,
        and_(TradeRecord.account_key.is_(None), TradeRecord.testnet == pe.testnet),
    )
    pend_cond = or_(
        PendingEntryOrder.account_key == pe.account_key,
        and_(PendingEntryOrder.account_key.is_(None), PendingEntryOrder.testnet == pe.testnet),
    )
    beta = (pe.raw or {}).get("beta")
    beta_new = float(beta) if beta is not None else 1.0
    running = db.execute(
        select(TradeRecord.remaining_risk, TradeRecord.risk_amount,
               TradeRecord.direction, TradeRecord.beta).where(
            TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
            acc_cond,
        )
    ).all()
    rows = [
        (float(rem if rem is not None else (risk or 0)), d, b)
        for rem, risk, d, b in running
    ]
    # 其它活跃挂单（排除本单）同样占用额度，与开仓闸门口径一致
    others = db.execute(
        select(PendingEntryOrder.risk_amount, PendingEntryOrder.direction,
               PendingEntryOrder.raw).where(
            PendingEntryOrder.status == _PENDING_ACTIVE,
            PendingEntryOrder.id != pe.id,
            pend_cond,
        )
    ).all()
    for r_amt, d, r_raw in others:
        b = (r_raw or {}).get("beta")
        rows.append((float(r_amt or 0), d, float(b) if b is not None else 1.0))
    heat_cap, same_cap = _effective_caps(cfg)
    if same_cap > 0 and _week_derate_stats(
            db, pe.account_key, pe.testnet, cfg, wallet) is not None:
        same_cap = max(1, same_cap // 2)  # 周减速：同向上限减半（至少 1），与开仓同口径
    if heat_cap > 0:
        btc_exposure = sum(
            r * (float(b) if b is not None else 1.0) for r, _d, b in rows
        )
        add = risk_amount * beta_new
        if btc_exposure + add > wallet * heat_cap:
            reasons.append(
                f"组合热度超限（β加权）：在跑+挂单 {btc_exposure:.2f} + 本单 "
                f"{add:.2f} > 钱包 {wallet:.2f} × {heat_cap * 100:.0f}%"
            )
    if same_cap > 0:
        same_n = sum(1 for _r, d, _b in rows if d == pe.direction) + 1
        if same_n > same_cap:
            reasons.append(
                f"同向持仓达上限：{pe.direction} 方向含本单 {same_n} 单 > {same_cap}"
            )
    loss_enabled = (
        bool(cfg.daily_loss_stop_enabled) if cfg.daily_loss_stop_enabled is not None
        else settings.DAILY_LOSS_STOP_ENABLED
    )
    loss_pct = (
        float(cfg.daily_loss_stop_pct) if cfg.daily_loss_stop_pct is not None
        else float(settings.DAILY_LOSS_STOP_PCT)
    )
    if loss_enabled and loss_pct > 0:
        day_pnl = _daily_closed_pnl(db, pe.account_key, pe.testnet)
        loss_cap = _daily_loss_cap(wallet, pct=loss_pct)
        if day_pnl <= -loss_cap:
            reasons.append(f"当日亏损熔断：已平仓净亏 {day_pnl:.2f} ≤ -{loss_cap:.0f}")
    return reasons


def materialize_pending_entry(
    db: Session, trader: BinanceTrader, pe: PendingEntryOrder, cfg: SystemConfig,
) -> Optional[UUID]:
    """把一条已成交的挂单幂等地转成真实仓位（docs/27 成交后落地）。

    入口共用：watch_pending_entries（1 分钟兜底巡检）与 event_executor
    （user-data-stream 实时，EVENT_EXEC_ENABLED 开时）。步骤：
    ① 行锁重读 + status 二次校验（已终态直接返回既有 trade_record_id，幂等）；
    ② 撤剩余未成交部分（部分成交防护——不加仓到超 SL/TP 覆盖）；
    ③ 查实际成交（executedQty / avgPrice）；
    ④ 设杠杆 + 补挂 SL/TP1/（TP2）；
    ⑤ 建 TradeRecord(OPENED) + 落 OPEN 事件（source=pending_limit）；
    ⑥ 回写 pending → filled；
    ⑦ 挂 SL/TP 失败即紧急平仓并将 pending 落 failed（不留裸仓）。
    返回新建 TradeRecord.id；无成交/失败返回 None（调用方据此记账）。
    """
    symbol = pe.symbol
    pe_id = pe.id
    raw = pe.raw or {}
    # ① 行锁重读 + 幂等二次校验
    locked = db.execute(
        select(PendingEntryOrder)
        .where(PendingEntryOrder.id == pe_id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        db.rollback()
        return None
    if locked.status != _PENDING_ACTIVE:
        db.commit()  # 释放行锁
        logger.info("挂单落地跳过 %s：状态已为 %s（幂等）", symbol, locked.status)
        return locked.trade_record_id
    pe = locked
    direction = pe.direction
    sl = float(pe.stop_loss or 0)
    tp1 = float(pe.tp1 or 0)
    tp2 = float(pe.tp2 or 0)
    leverage = int(pe.leverage or 1)
    close_side = _close_side(direction)
    filled_qty_real = 0.0
    # 成交价在 ③ 读取；先兜底为限价，保证异常路径落事故时仍有可用价格。
    # rollback 后 pe.filled_price 会被还原为 None，事故必须用局部实际均价。
    fill_price = float(pe.entry_price or 0)
    try:
        # ② 撤剩余未成交部分（部分成交防护；已全成交/已撤则静默）
        try:
            trader.cancel_regular_order(symbol, int(pe.limit_order_id))
        except Exception as cancel_err:
            logger.warning("挂单 %s 撤剩余失败（继续按已成交量落地）: %s", symbol, cancel_err)
        # ③ 实际成交
        od = trader.order(symbol, int(pe.limit_order_id))
        qty = float(od.get("executedQty") or 0)
        fill_price = float(od.get("avgPrice") or 0) or float(pe.entry_price or 0)
        if qty <= 0:
            # 2026-10-26：币安订单查询存在瞬时不一致（MORPHOUSDT live-1 实例：
            # 已 FILLED 99.9 的订单在撤余量后被复读为 0 成交）。单次复读不足以
            # 判定无成交——用持仓交叉验证：同向有仓即按实际持仓量落地，避免把
            # 已成交单标成 no_fill、留下无 SL/TP/无 TradeRecord 的裸仓。
            pos = trader.positions().get(symbol) or {}
            amt = float(pos.get("amt") or 0)
            if amt != 0 and (amt > 0) == (direction == "long"):
                qty = abs(amt)
                fill_price = float(pos.get("entry_price") or 0) or fill_price
                logger.warning(
                    "挂单 %s：订单复读 0 成交但持仓 %s，按持仓量落地（防裸仓）",
                    symbol, amt,
                )
            else:
                pe.status = "canceled"
                pe.resolve_reason = "no_fill"
                pe.resolved_at = datetime.utcnow()
                db.commit()
                logger.info("挂单 %s：撤单后无成交，标记 canceled", symbol)
                return None
        filled_qty_real = qty
        # ④ 设杠杆 + 补挂 SL/TP（与市价开仓同口径）
        a = db.get(AIAnalysis, pe.ai_analysis_id) if pe.ai_analysis_id else None
        trader.setup_leverage(symbol, leverage)
        f = trader.filters(symbol)
        sl_order = trader.stop_market_close(
            symbol, close_side, trader.round_price(symbol, sl))
        qty_tp1, qty_tp2, _tp_full = _tp_order_quantities(
            trader, symbol, qty, tp2, cfg, min_qty=f["min_qty"])
        tp1_order = trader.take_profit_reduce(
            symbol, close_side, qty_tp1, trader.round_price(symbol, tp1))
        tp2_order = None
        # 全平模式允许两档合计恰好等于全仓；关闭时须严格小于（保留跟进尾仓）
        if tp2 > 0 and qty_tp2 >= f["min_qty"] and _tp_sum_ok(
                qty_tp1, qty_tp2, qty, _tp_full):
            tp2_order = trader.take_profit_reduce(
                symbol, close_side, qty_tp2, trader.round_price(symbol, tp2))
        # ⑤ 建 TradeRecord（口径镜像 _attempt_open 的市价开仓）
        now = datetime.utcnow()
        # 三口径：挂单的计划风险在挂单时按限价算（pe.risk_amount），成交后按
        # 实际均价重算初始风险；未成交前计划值不受成交滑点影响
        initial_risk = round(qty * abs(fill_price - sl), 4)
        planned_risk = (
            round(float(pe.risk_amount), 4) if pe.risk_amount is not None else initial_risk
        )
        # 闸门复核（2026-10-27 F3）：挂单存续期间组合热度/同向/日熔断可能已被
        # 其它成交推过线。成交已发生不能回退，保护单照挂，只留越线痕迹供归因。
        # SAVEPOINT 隔离只读复核（2026-10-27 review）：查询级错误（如未跑迁移）
        # 只回滚保存点，避免事务被标记失败后 commit 报错、进而触发外层紧急平仓；
        # 复核失败降级为不复核，绝不阻断保护单挂出。
        try:
            with db.begin_nested():
                gate_breaches = _pending_gate_breaches(
                    db, pe, cfg, initial_risk, float(raw.get("wallet") or 0))
        except Exception as e:
            logger.warning("挂单成交落地 %s：闸门复核失败（跳过）: %s", symbol, e)
            gate_breaches = []
        if gate_breaches:
            logger.warning(
                "挂单成交落地 %s：开仓闸门越线（%s）——保护单照常挂出",
                symbol, "；".join(gate_breaches),
            )
        # 入场诊断（O1，2026-10-27）：成交时按真实成交价重算 pos——信号 K 可能
        # 已换根，挂单期快照的位置就过期了；重算失败才沿用挂单期快照
        entry_diag_fresh = False
        entry_diag: dict = dict(raw.get("entry_diag") or {})
        if a is not None:
            _recomputed = _entry_snapshot(
                cfg, a, price=float(fill_price), stop_loss=sl,
                direction=direction, tp1=tp1, entry_path="limit",
            )
            if _recomputed:
                entry_diag = _recomputed
                entry_diag_fresh = True
        if entry_diag:
            entry_diag["filled_price"] = float(fill_price)
        # TP2 未挂上诊断（docs/23 T14d，2026-10-29）：挂单落地路径与市价同口径
        _tp2_skip = _tp2_skip_info(
            tp2, tp2_order, qty_tp1, qty_tp2, qty, f["min_qty"], _tp_full)
        rec = TradeRecord(
            symbol=symbol,
            direction=direction,
            scan_result_id=pe.scan_result_id,
            ai_analysis_id=pe.ai_analysis_id,
            recommendation=(float(a.recommendation)
                            if a is not None and a.recommendation is not None else None),
            signal_type=(a.scan_result.signal_type
                         if a is not None and a.scan_result is not None else None),
            pattern=(a.scan_result.pattern
                     if a is not None and a.scan_result is not None else None),
            rule_version=settings.RULE_VERSION,
            testnet=trader.testnet,
            account_key=trader.account_key,
            ai_snapshot=raw.get("ai_snapshot") or (_ai_snapshot(a) if a is not None else None),
            entry_price=fill_price,
            qty=qty,
            notional=round(qty * fill_price, 2),
            leverage=leverage,
            margin_mode="isolated",
            margin_used=round(qty * fill_price / leverage, 4),
            risk_amount=initial_risk,
            planned_risk=planned_risk,
            initial_risk=initial_risk,
            remaining_risk=initial_risk,
            risk_budget_mode=raw.get("risk_budget_mode") or (cfg.risk_budget_mode or "pct"),
            beta=raw.get("beta"),
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2 if tp2_order else 0,
            trailing_mode=TRAIL_MODE_THREE_LINE,  # 挂单成交落库同默认三线止损（2026-10-21）
            status="OPENED",
            opened_at=now,
            raw={
                "entry_order_id": pe.limit_order_id,
                "sl_order_id": sl_order.get("algoId") or sl_order.get("orderId"),
                "tp1_order_id": (tp1_order.get("algoId") or tp1_order.get("orderId")) if tp1_order else None,
                "tp2_order_id": (tp2_order.get("algoId") or tp2_order.get("orderId")) if tp2_order else None,
                "qty_tp1": qty_tp1,
                "qty_tp2": qty_tp2 if tp2_order else 0,
                # 止盈全平模式快照（2026-10-28）：成交落地时定稿，巡检结算据本单
                # 口径推断「TP 档平完全仓」出场，不随期间配置改动漂移
                "tp_full_exit": _tp_full,
                # TP2 未挂上原因（docs/23 T14d）：区分「AI 没报」与「报了没挂上」
                "tp2_skipped": _tp2_skip,
                "capital_base": raw.get("capital_base") or _capital_base(float(raw.get("wallet") or 0)),
                "wallet": raw.get("wallet"),
                "realized_bookmark": 0,
                "pending_entry_id": str(pe_id),
                # 入场诊断（O1，2026-10-27）：成交时按真实成交价重算 pos（信号 K 可能
                # 已换根，挂单期快照的位置就过期了）；重算不出来才沿用挂单期快照
                "entry_diag": (entry_diag or None),
                "entry_diag_source": ("fill" if entry_diag_fresh else "placement"),
            },
        )
        db.add(rec)
        db.flush()
        _add_event(db, rec, "OPEN", {
            "price": fill_price, "qty": qty, "stop_loss": sl, "tp1": tp1, "tp2": tp2,
            "risk_amount": initial_risk,
            "planned_risk": planned_risk, "initial_risk": initial_risk,
            "remaining_risk": initial_risk,
            "entry_order_id": pe.limit_order_id, "source": "pending_limit",
        })
        # 报了吗/挂上了吗分开记（docs/23 T14d）：挂单落地路径同样标 TP2_SKIPPED
        if _tp2_skip:
            _add_event(db, rec, "TP2_SKIPPED", _tp2_skip)
            logger.warning("挂单成交 %s：AI 报 TP2=%s 但未挂上（%s）",
                           symbol, _tp2_skip["tp2"], _tp2_skip["reason"])
        if gate_breaches:
            _add_event(db, rec, "GATE_BREACH", {
                "reasons": gate_breaches, "source": "pending_limit",
            })
        # ⑥ 回写 pending
        pe.status = "filled"
        pe.filled_price = fill_price
        pe.filled_qty = qty
        pe.trade_record_id = rec.id
        pe.resolved_at = now
        db.commit()
        logger.info(
            "挂单成交落地 %s %s qty=%s 成交=%s sl=%s tp1=%s tp2=%s（trade=%s，docs/27）",
            symbol, direction, qty, fill_price, sl, tp1, tp2, rec.id,
        )
        return rec.id
    except Exception as e:
        # ⑦ 挂 SL/TP 失败即紧急平仓，不留裸仓（镜像市价开仓的兜底）
        db.rollback()
        logger.error("挂单成交落地失败 %s: %s（紧急平仓防裸仓）", symbol, e)
        close_qty = filled_qty_real if filled_qty_real > 0 else float(pe.qty or 0)
        incident = dict(
            account_key=trader.account_key, testnet=trader.testnet,
            symbol=symbol, direction=direction,
            reason="pending_materialize_protection_failed", qty=close_qty,
            entry_order_id=pe.limit_order_id,
            entry_price=fill_price or float(pe.entry_price or 0),
            rollback_session=False,
        )
        try:
            close_result = trader.market_order(
                symbol, close_side, close_qty, reduce_only=True)
        except Exception as close_err:
            logger.error("挂单成交后紧急平仓失败 %s: %s", symbol, close_err)
            from app.services.protection_recovery import (
                create_incident_after_rollback, status_for_close_error,
            )
            try:
                create_incident_after_rollback(
                    db, **incident,
                    error=f"protection={e}; close={close_err}",
                    status=status_for_close_error(close_err),
                )
            except Exception:
                logger.exception("挂单保护事故无法持久化 %s", symbol)
        else:
            # 与市价开仓同口径：ACK 不代表已空仓，落待对账事故由巡检确认
            from app.services.protection_recovery import create_incident_after_rollback
            try:
                create_incident_after_rollback(
                    db, **incident,
                    error=f"protection={e}; close_ack={close_result}",
                    status="awaiting_reconcile",
                )
            except Exception:
                logger.exception("挂单平仓待对账事故无法持久化 %s", symbol)
        try:
            fresh = db.get(PendingEntryOrder, pe_id)
            if fresh is not None:
                fresh.status = "failed"
                fresh.resolve_reason = str(e)[:32]
                fresh.resolved_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
        return None


# 下单环境未上架拒开原因（2026-10-11 常量化）：_attempt_open 落库用文本，
# trade_tasks 扇出后按此匹配收集、锁外发飞书提示——两处必须同步修改
BLOCK_UNTRADABLE = "下单环境未上架该合约（无法交易）"


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
            TradeRecord.excluded.is_(False),  # 异常单剔除（2026-10-18）：伪亏损不得触发熔断
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


def _effective_caps(cfg: Optional[SystemConfig]) -> tuple[float, int]:
    """组合热度/同向上限的生效值（2026-10-27 抽出，开仓与挂单落地复核共用）。

    cfg 覆盖优先、NULL 回落 env——单一事实源，避免两处解析口径漂移。
    """
    heat_cap = (
        float(cfg.portfolio_heat_cap)
        if cfg is not None and cfg.portfolio_heat_cap is not None
        else float(settings.PORTFOLIO_HEAT_CAP)
    )
    same_cap = (
        int(cfg.same_direction_cap)
        if cfg is not None and cfg.same_direction_cap is not None
        else int(settings.SAME_DIRECTION_CAP)
    )
    return heat_cap, same_cap


def _week_derate_stats(
    db: Session, account_key: Optional[str], testnet: bool,
    cfg: Optional[SystemConfig], wallet: float,
) -> Optional[tuple[float, float, float]]:
    """周回撤减速是否生效（2026-10-27 抽出，开仓与挂单落地复核共用）。

    UTC 周已平仓净亏达「风险基数 × weekly_dd_stop_pct」且本 UTC 周未人工
    确认时生效。生效返回 (week_pnl, week_cap, week_pct)，未生效返回 None。
    开仓侧据此把在跑/同向上限减半，挂单成交复核用同一口径，避免漏判半档上限。
    """
    week_pct = (
        float(cfg.weekly_dd_stop_pct)
        if cfg is not None and cfg.weekly_dd_stop_pct is not None
        else float(settings.WEEKLY_DD_STOP_PCT)
    )
    if week_pct <= 0:
        return None
    now_utc = datetime.utcnow()
    week_start = (now_utc - timedelta(days=now_utc.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)  # UTC 周一 0 点
    week_pnl = _closed_pnl_since(db, account_key, testnet, week_start)
    week_cap = _capital_base(wallet) * week_pct / 100.0
    if week_pnl > -week_cap:
        return None
    acked = (cfg is not None and cfg.weekly_breaker_ack_at is not None
             and cfg.weekly_breaker_ack_at >= week_start)
    if acked:
        return None
    return week_pnl, week_cap, week_pct


# 钱包余额缓存（熔断状态卡片用）：account_key → (wallet, ts)，5 分钟 TTL——
# scan_status 前端 30s 轮询，逐次查币安会把 GET /fapi/v2/account 打成高频
_wallet_cache: dict[str, tuple[float, float]] = {}


def breaker_status_by_net(db: Session, cfg) -> dict:
    """各网熔断状态（顶部卡片「熔断状态」列，2026-10-04）：
    {"testnet": {...}, "live": {...}}，每网：
    - enabled：熔断生效前提（开关开 && 阈值>0 && 该网有可开单账号）
    - active：True=已熔断 / False=未熔断 / None=无法判定（钱包查询失败或未启用）
    - day_pnl：该网账号 UTC 当日已平仓净亏合计
    - cap：熔断阈值（多账号取最小=最保守）
    - week/month（docs/13 #2 分层，2026-10-04）：周/月回撤状态
      week: {active, hit, acked, pnl, cap}；month: {active, hit, acked, pnl, cap}
      month.active = 触发且未人工确认（ack 在当前 UTC 月内 = 已确认放行）
      week.active = 触发且未人工确认（ack 在当前 UTC 周内 = 已确认放行，
      2026-10-07 对称月熔断补人工通道）
    账号口径（2026-10-13）：按 managed_accounts（双网管理清单，不随自动开单开关
    变化）统计——熔断/减速对手动开单同样生效，故展示口径须覆盖手动可开账号；
    此前用 auto_trade_accounts 时「正式网自动开单关闭」会让正式网整网丢失周减速
    状态（手动开单提示「（周减速减半）」而顶部卡片不显示）。
    钱包余额 5 分钟进程内缓存；这是展示口径，开仓拦截仍以 open gate 实时判定为准。"""
    from app.services.account_registry import managed_accounts

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
    for acc in managed_accounts(db):
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
            TradeRecord.excluded.is_(False),  # 异常单剔除（2026-10-18）
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
    账号维度闸门（去重/在跑/熔断/降速，按账号实时判定）与 1h 时效。
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
) -> str:
    """单条分析完成即开仓（2026-09-14：不再等 :42 统一批次，docs/06 §2）。

    闸门与批量开仓完全一致（§3）：上限 / 70% 余额 / 最小资金 / 同币无仓 /
    策略开关 / 方向价格复验 / 盈亏比复验 / EMA / 强平闸门。
    返回开仓结局："opened"（市价成交建单）/ "pending"（现价盈亏比不足，已在
    入场价挂限价单等待入场，docs/27）/ "blocked"（闸门拦截或失败）；闸门未过
    即放弃（无批次重试，等该币下次信号重新分析）。
    force_market=True（2026-09-28 手动开单按钮）：市价即时入场，跳过
    入场偏离/待回踩时机闸门（自动成交路径默认 False，语义不变）。
    """
    a = db.get(AIAnalysis, UUID(str(analysis_id)))
    if a is None:
        return "blocked"
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        return "blocked"
    # 开仓资格快速过滤（suggest / 方向 / 三价 / 评分门槛 / 1 小时内）
    # 评分门槛（2026-10-02 上页面）：cfg.min_recommendation 覆盖时优先，NULL 回落 env
    min_rec = (
        float(cfg.min_recommendation) if cfg.min_recommendation is not None
        else float(settings.TRADING_MIN_RECOMMENDATION)
    )
    if not analysis_open_eligible(a, min_rec):
        return "blocked"
    # 4h 资格与时效复核（2026-10-21 第二阶段 T1）：扫描/分析到实际开仓之间
    # 可能跨越 4h 收盘——缺数（htf_degraded）或状态快照陈旧一律禁开，
    # 写明确原因供前端与复盘（旧行 htf_as_of=NULL 按兼容放行）。
    from app.services.market_data import htf_open_block_reason
    htf_block = htf_open_block_reason(
        bool(getattr(a, "htf_degraded", False)),
        getattr(a, "htf_as_of", None),
        datetime.utcnow(),
    )
    if htf_block:
        _mark_open_block(db, a, htf_block)
        return "blocked"
    if a.created_at and a.created_at < datetime.utcnow() - timedelta(hours=1):
        logger.info("即时开仓跳过 %s：分析已超过 1 小时", a.symbol)
        _mark_open_block(db, a, "分析已超过 1 小时（信号过期）")
        return "blocked"
    # 持久保护事故按账号封锁新开仓，避免异常仓位尚未确认时继续增加风险。
    from app.services.protection_recovery import has_active_incident
    if has_active_incident(db, trader.account_key, trader.testnet):
        _mark_open_block(db, a, "账号存在未解决的保护事故，禁止新开仓")
        return "blocked"
    # 该分析在本账号已开过仓（docs/17 P1 扇出：同一 AI 分析允许多账号各开一单，
    # 按账号判断；NULL account_key 旧行按环境回落，与全局口径一致）
    acc_cond = or_(
        TradeRecord.account_key == trader.account_key,
        and_(TradeRecord.account_key.is_(None), TradeRecord.testnet == trader.testnet),
    )
    if db.execute(
        select(TradeRecord.id).where(TradeRecord.ai_analysis_id == a.id).where(acc_cond)
    ).first():
        return "blocked"  # 该账号已用此分析开过仓
    # 连损降速软熔断（docs/24 A3）：紧随评分门槛（TRADING_MIN_RECOMMENDATION）的
    # 动态提升——当日连损 ≥3 → 评分 ≥85，连损 2 → ≥75（绝对档位；提升档位低于
    # 现行门槛值时不生效，不会反向放宽）。连损趋势日降低开单频率观望，
    # 软熔断只提门槛不停摆（区别于当日亏损硬熔断）。0 连损/开关关闭不生效
    if settings.CONSECUTIVE_LOSS_DERATE:
        losses = _consecutive_losses_today(db, trader.account_key, trader.testnet)
        # 生效门槛单一事实源（T0-2，2026-10-21）：与 AI 提示词「执行门槛」行
        # 同一函数；max(静态门槛, 连损档) 保证降速不会反向放宽
        need_score = effective_min_recommendation(cfg, losses)
        if float(a.recommendation or 0) < need_score:
            logger.info(
                "即时开仓跳过 %s：连损 %d 笔降速，评分 %s 低于提升后门槛 %s",
                a.symbol, losses, a.recommendation, f"{need_score:g}",
            )
            _mark_open_block(
                db, a,
                f"连损{losses}笔降速：评分门槛提升至{need_score:g}"
                f"（当前评分 {float(a.recommendation or 0):.0f}）",
            )
            return "blocked"
    # 待回踩单不再在此跳过（2026-09-18）：回踩距离 ≤ pullback_market_pct 容差
    # 时市价入场、超容差维持不下单——判定移入 _attempt_open 偏离闸门
    positions = trader.positions()
    wallet_info = trader.wallet_balance()
    wallet = wallet_info["wallet"]
    # 交易所权威「可用保证金」（availableBalance，2026-10-17 -2019 修复）：已扣除
    # 全部持仓初始保证金 / 挂单冻结 / 未实现亏损对可用额度的影响，是「此刻再开新仓
    # 最多能用多少保证金」的唯一真实口径，与钱包余额 wallet（balance）不是一个量
    available = wallet_info["available"]
    # 周回撤减速（2026-10-04 docs/13 #2；2026-10-07 改口径：单笔预算减半 →
    # 在跑上限/同向上限减半）：UTC 周已平仓净亏达基数×阈值且未人工确认时，
    # 本请求的单数上限与同向上限减半（floor，至少 1）。软减速不拦开仓；
    # 确认在当前 UTC 周内 = 本周放行，下周一 0 点自动重新武装。
    # （wallet 提前取：减半后的上限在下方单数/同向闸门即需生效）
    now_utc = datetime.utcnow()  # 月熔断等其他闸门共用
    week_stats = _week_derate_stats(db, trader.account_key, trader.testnet, cfg, wallet)
    week_derate = week_stats is not None
    if week_stats is not None:
        week_pnl, week_cap, week_pct = week_stats
        logger.warning(
            "周度回撤减速 %s（%s）：UTC 周已平仓净亏 %.2f ≤ -%.0f 阈值"
            "（风险基数 × %.0f%%）——本周剩余时间在跑上限/同向上限减半"
            "（软减速不拦开仓；「确认复盘」可恢复，周一 0 点自动恢复）",
            a.symbol, trader.account_key, week_pnl, week_cap, week_pct,
        )
    # 单数上限（0 = 不限单数，仅靠余额风控兜底）；已开始移动止损的单子
    # （TP1_HIT 止损已移保本 / TP2_HIT 仅剩跟进仓）风险已锁定，不占名额（2026-09-25）
    # 测试网不受最大持仓限制（2026-10-15 用户拍板）：模拟环境用于多开观察策略
    # 表现，持仓数量仅受下方「开仓最低可用余额」闸约束；同向上限/熔断/热度等
    # 风险闸两网口径不变
    if not trader.testnet and cfg.max_open_trades > 0:
        moved_syms = {
            sym for (sym,) in db.execute(
                select(TradeRecord.symbol).where(
                    TradeRecord.status.in_(("TP1_HIT", "TP2_HIT")),
                    TradeRecord.symbol.in_(list(positions.keys())),
                    _adopt_acc_cond(trader),
                )
            ).all()
        }
        # 在跑单名额并计入同账号活跃挂单（docs/27 D7）：挂单已锁定保证金/敞口，
        # 不并入会在单数闸门上重复分配名额
        _pend_live_n, _ = _pending_reserved(db, trader.account_key, trader.testnet)
        slot_used = len(positions) - len(moved_syms) + _pend_live_n
        slot_limit = (
            max(1, cfg.max_open_trades // 2) if week_derate else cfg.max_open_trades
        )
        if slot_used >= slot_limit:
            suffix = "（周减速减半）" if week_derate else ""
            logger.info("即时开仓跳过 %s：在跑单子已达上限 %d%s",
                        a.symbol, slot_limit, suffix)
            _mark_open_block(db, a, f"在跑单子已达上限 {slot_limit}{suffix}")
            return "blocked"
    occupied = sum(p["initial_margin"] for p in positions.values())
    # 余额风控占用并计入活跃挂单保证金（docs/27 D7）
    _, _pend_margin = _pending_reserved(db, trader.account_key, trader.testnet)
    occupied += _pend_margin
    # 余额风控（2026-09-15 起可配置）：开仓最低可用余额占钱包比例，默认 50%（原固定 70%）
    min_free = (
        settings.TRADING_MIN_FREE_PCT
        if cfg is None or cfg.trading_min_free_pct is None
        else float(cfg.trading_min_free_pct)
    )
    if wallet > 0 and wallet - occupied < wallet * min_free:
        logger.info("即时开仓跳过 %s：可用余额低于总资金 %.0f%%", a.symbol, min_free * 100)
        _mark_open_block(db, a, f"可用余额低于总资金 {min_free * 100:.0f}%")
        return "blocked"
    if _capital_base(wallet) <= 0:
        logger.info("即时开仓跳过 %s：钱包余额低于最小风险档位", a.symbol)
        _mark_open_block(db, a, "钱包余额低于最小风险档位")
        return "blocked"
    # 组合热度上限 + 同向持仓上限（docs/24 A3'，紧随在跑单上限闸门）：在跑单按
    # 全状态集合（OPENED/TP1_HIT/TP2_HIT）计——TP1_HIT/TP2_HIT 止损已上移不占
    # 单数名额，但剩余仓的风险敞口与方向集中度仍然真实存在
    # （2026-10-02 上页面：heat_cap/same_cap/risk_pct 均按 cfg 覆盖优先、NULL 回落 env）
    heat_cap, same_cap = _effective_caps(cfg)
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
            select(TradeRecord.remaining_risk, TradeRecord.risk_amount,
                   TradeRecord.direction, TradeRecord.symbol, TradeRecord.beta).where(
                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
                _adopt_acc_cond(trader),
            )
        ).all()
        # 组合风险分列口径（2026-10-21 第二阶段 T4）：闸门用 remaining_risk（TP/移损
        # 后的当前暴露），不再用开仓时的 historical risk_amount。旧行 remaining_risk
        # 为空时回落 risk_amount（迁移已回填；双保险）。三个指标各司其职：
        # ① 总止损风险=Σ剩余风险（不按 β 加权）；② 方向集中度=同向单数与同向风险；
        # ③ BTC 因子暴露=Σ剩余风险×β（相关性重复计数，闸门依据）
        rows_risk = [
            (float(rem if rem is not None else (risk or 0)), d, s, b)
            for rem, risk, d, s, b in running
        ]
        # 活跃挂单同样占用组合风险额度（2026-10-27 F3）：挂单成交前不建
        # TradeRecord，但交易所侧已锁定敞口；不并入会让多条挂单同时穿透
        # 热度/同向上限。排除本分析自身的挂单（重复触发幂等，不自我拦截）。
        for _pe in _active_pendings(db, trader.account_key, trader.testnet):
            if _pe.ai_analysis_id == a.id:
                continue
            _pe_beta = (_pe.raw or {}).get("beta")
            rows_risk.append((
                float(_pe.risk_amount or 0), _pe.direction, _pe.symbol,
                float(_pe_beta) if _pe_beta is not None else 1.0,
            ))
        total_stop_risk = sum(r for r, _d, _s, _b in rows_risk)
        btc_exposure = sum(
            r * (float(b) if b is not None else 1.0)
            for r, _d, _s, b in rows_risk
        )
        direction_risk: dict[str, float] = {}
        for r, d, _s, _b in rows_risk:
            direction_risk[d] = direction_risk.get(d, 0.0) + r
        same_n = sum(1 for _r, d, _s, _b in rows_risk if d == a.direction)
        same_risk = direction_risk.get(a.direction, 0.0)
        # 原口径对照（仅日志）：开仓时 risk_amount 的 β 加权合计
        heat_used_orig = sum(
            float(risk or 0) * (float(b) if b is not None else 1.0)
            for _rem, risk, _d, _s, b in running
        )
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
        # ① BTC 因子暴露闸门（2026-10-04 β 加权，docs/13 #1）：山寨与 BTC 相关性
        #    0.7~0.9，同向单实际是同一 BTC 敞口的多份拷贝，「单笔合规、组合隐性
        #    集中」。β 缺失（旧行/查询失败）按 1.0。input 已切换为 remaining_risk。
        #    0=关闭
        if heat_cap > 0:
            est_risk_eff = est_risk * beta_new
            if btc_exposure + est_risk_eff > wallet * heat_cap:
                logger.info(
                    "即时开仓跳过 %s：组合热度超限（β加权·剩余风险：在跑 %.2f + 本单 %.2f×β%.2f"
                    " > 钱包 %.2f × %.0f%%；总止损风险 %.2f；同向 %s %.2f；"
                    "原口径（risk_amount×β）%.2f）",
                    a.symbol, btc_exposure, est_risk, beta_new, wallet,
                    heat_cap * 100, total_stop_risk, a.direction, same_risk,
                    heat_used_orig,
                )
                _mark_open_block(
                    db, a,
                    f"组合热度超限（β加权）：在跑 {btc_exposure:.2f} + 本单 "
                    f"{est_risk_eff:.2f} > 钱包 {wallet:.2f} × {heat_cap * 100:.0f}%"
                    f"（总止损风险 {total_stop_risk:.2f}，"
                    f"{a.direction} 同向 {same_risk:.2f}）",
                )
                return "blocked"
        # ② 同向：同 direction 在跑单数 ≥ 同向上限 → 拦截。
        #    全市场同涨同跌（趋势同震）时同向单几乎同损——方向集中度上限（0=不限）。
        #    同向剩余风险 same_risk 进日志/拒绝原因，作为集中度的金额口径对照
        if same_cap > 0:
            if same_n >= same_cap:
                logger.info(
                    "即时开仓跳过 %s：同向（%s）在跑 %d 单已达上限 %d",
                    a.symbol, a.direction, same_n, same_cap,
                )
                _mark_open_block(
                    db, a,
                    f"同向持仓达上限：{a.direction} 方向在跑 {same_n} 单 ≥ {same_cap}"
                    + ("（周减速减半）" if week_derate else "")
                    + f"（同向剩余风险 {same_risk:.2f}）",
                )
                return "blocked"
    # 当日亏损熔断（2026-09-27 正式网复盘）：UTC 日本账号已平仓净亏 ≤ -阈值
    # （风险基数 × DAILY_LOSS_STOP_PCT，默认 9% ≈ 3 笔满损，随账户金额缩放）
    # 停开仓——09-26 全天 13 笔全灭仍持续开单，趋势日的赢单无法覆盖
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
            return "blocked"
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
                return "blocked"
    # 即将进入闸门序列：清掉沿用/上一轮尝试残留的拒开原因，本次成败由闸门重新裁定
    if a.open_block_reason:
        try:
            a.open_block_reason = None
            db.commit()
        except Exception:
            db.rollback()
    return _attempt_open(db, trader, cfg, a, wallet, positions,
                         force_market=force_market, beta=beta_raw,
                         available=available)


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


def _hl_structure_stop(rec: TradeRecord, klines: list[list],
                       order: int = 3) -> Optional[float]:
    """HL/LH 结构跟进止损（2026-10-20 用户方案）。

    多单：入场后最近一个已确认的 **HL（更高低点）** → 止损移到该 HL 的低点；
    空单：入场后最近一个已确认的 **LH（更低高点）** → 止损移到该 LH 的高点。

    结构判定用实际 K 线高/低点（非收盘价）：摆动低点 = 低于左右各 order 根的
    最低价，两个相邻摆动低点抬高即为 HL。只用已收盘 K 线（剔除最后一根未收盘），
    确认延迟 = order 根，避免用未确认摆动点追价。
    最近摆动低点为 LL（未抬高）时返回 None → 保留现 SL，等下一个 HL 确认。
    """
    if not klines or order < 1:
        # 数据/配置异常：本轮 HL 腿静默不生效，必须留下告警（系统日志只收 WARNING+）
        logger.warning("HL 结构 %s：无 K 线或阶数非法（order=%s），本轮不跟进止损",
                       getattr(rec, "symbol", "?"), order)
        return None
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < 2 * order + 1:
        logger.warning("HL 结构 %s：已收盘 K 线不足（%d < %d，order=%s），"
                       "摆动点无法确认，本轮不跟进止损",
                       getattr(rec, "symbol", "?"), len(closed), 2 * order + 1, order)
        return None
    highs = np.array([float(k[2]) for k in closed], dtype=float)
    lows = np.array([float(k[3]) for k in closed], dtype=float)
    high_idx, low_idx = find_swing_points(highs, lows, order)
    opened = getattr(rec, "opened_at", None)
    opened_ms = (opened.replace(tzinfo=timezone.utc).timestamp() * 1000
                 if opened is not None else None)

    def _after_open(seq: list[int]) -> list[int]:
        if opened_ms is None:
            return seq
        return [i for i in seq if float(closed[i][0]) >= opened_ms]

    if rec.direction == "long":
        seq = _after_open(low_idx)
        if len(seq) < 2:
            logger.debug("HL 结构 %s：入场后确认摆动低点不足 2 个（%d），无 HL 可比对",
                         getattr(rec, "symbol", "?"), len(seq))
            return None
        last, prev = seq[-1], seq[-2]
        if lows[last] <= lows[prev]:
            logger.debug("HL 结构 %s：最近摆动低点 %s 未抬高（前低 %s），无新 HL",
                         getattr(rec, "symbol", "?"), lows[last], lows[prev])
            return None  # 最近低点未抬高（LL/持平）→ 无新 HL
        return float(lows[last])
    seq = _after_open(high_idx)
    if len(seq) < 2:
        logger.debug("HL 结构 %s：入场后确认摆动高点不足 2 个（%d），无 LH 可比对",
                     getattr(rec, "symbol", "?"), len(seq))
        return None
    last, prev = seq[-1], seq[-2]
    if highs[last] >= highs[prev]:
        logger.debug("HL 结构 %s：最近摆动高点 %s 未降低（前高 %s），无新 LH",
                     getattr(rec, "symbol", "?"), highs[last], highs[prev])
        return None  # 最近高点未降低（HH/持平）→ 无新 LH
    return float(highs[last])


def _trail_swing_order(cfg) -> int:
    """HL/LH 结构确认阶数：取扫描配置页「摆动点阶数」(system_config.swing_order)。

    解析口径与扫描侧 scan_filters._swing_order 完全一致：NULL / 非法类型 /
    <1 一律回落 env SWING_ORDER（默认 3）——跟进止损与扫描必须用同一把尺子，
    否则直连 API 写入 0 时会出现「扫描按 3 阶、跟进按 1 阶」的口径分裂。
    确认延迟 = order 根已收盘 K——阶数越大，新 HL/LH 出现得越稀。
    """
    try:
        v = int(getattr(cfg, "swing_order", None))
    except (TypeError, ValueError, OverflowError):
        return settings.SWING_ORDER
    return v if v >= 1 else settings.SWING_ORDER


def _trailing_mode(rec: TradeRecord) -> str:
    """记录的移动止损方式：NULL（上线前旧行）按三线止损理解。

    非空但不在白名单内的值（手工改库/坏配置）同样回落三线，并告警——
    避免一个拼写错误的模式名静默改变风控行为而不留痕迹。"""
    mode = getattr(rec, "trailing_mode", None)
    if mode in TRAIL_MODES:
        return mode
    if mode:
        logger.warning(
            "未知移动止损方式 %r（%s），按三线止损执行",
            mode, getattr(rec, "symbol", "?"),
        )
    return TRAIL_MODE_THREE_LINE


def _move_sl_trailing(db: Session, trader: BinanceTrader, rec: TradeRecord,
                      interval: str, open_ids: set[int],
                      atr_k: Optional[float] = None,
                      swing_order: int = 3) -> None:
    """按记录的移动止损方式跟进（只收紧不放松）；新 SL 会立即触发时跳过本轮
    （旧 SL 仍有效保护：仓位存在即旧 SL 未触发），避免交易所 -2021 拒单导致
    整轮结算回滚重试。

    - three_line（新开单默认，2026-10-21 拍板改回）：摆动腿（最近3根已收盘K线极值）+
      ATR 腿（Chandelier，atr_k None/0 关闭）取更紧者（docs/13 #5）
    - hl（可手动切换）：HL/LH 结构腿——多单移到最近已确认 HL 低点，空单移到
      LH 高点；不再叠加 ATR 腿与 3 根极值腿
    两模式聚合后统一只收紧 + TP1 保本夹紧。
    """
    cur = float(rec.stop_loss or 0)
    if cur <= 0:
        return
    mode = _trailing_mode(rec)
    # HL 结构需要更长历史确认多个摆动点；三线腿只用尾部 3 根，按 20 根取够即可
    limit = 60 if mode == TRAIL_MODE_HL else 20
    klines = ExchangePool().get_klines(rec.symbol, interval, limit)
    candidates: list[float] = []
    if mode == TRAIL_MODE_HL:
        hl_sl = _hl_structure_stop(rec, klines, order=swing_order)
        if hl_sl is not None:
            candidates.append(hl_sl)
    else:
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
    # 移损同步剩余风险（保本/跟进后暴露应下降；盈利侧止损按 0 计）
    rec.remaining_risk = _remaining_risk(rec)
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
        # 持仓期 1h K（上限 60 天，趋势单持仓可跨多日）
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


_TP_FILLED_STATUS = "FINISHED"  # 币安 algo 条件单终态：已触发且成交


def _tp_algo_filled(trader: BinanceTrader, symbol: str,
                    order_id) -> Optional[bool]:
    """TP 条件单是否「真成交」（algoStatus 终态 FINISHED = 触发并成交）。

    TRIGGERING（已触发但未成交）、EXPIRED/REJECTED/CANCELED 都不算成交。
    返回 None 表示查询失败无法判定（调用方回落旧口径，见 _tp_filled_real）。
    """
    if not order_id:
        return False
    try:
        d = trader.algo_order_detail(symbol, int(order_id))
    except Exception as e:
        logger.debug("TP 条件单状态查询失败 %s #%s: %s", symbol, order_id, e)
        return None
    return str(d.get("algoStatus") or "").upper() == _TP_FILLED_STATUS


def _tp_filled_real(trader: BinanceTrader, symbol: str, order_id,
                    open_ids: set) -> bool:
    """TP 档是否真成交（2026-10-20 SQDUSDT 误标修复）。

    旧口径只看「TP 单号不在挂单列表」→ TRIGGERING/EXPIRED/REJECTED/CANCELED
    一律被当成交。测试网薄盘使 TP1 卡在 TRIGGERING，叠加止损 closePosition 部分
    成交把仓位缩到 60%，恰好过 ratio≤0.75 门槛，把一笔止损亏损平仓误标成
    TP1_HIT（并触发保本移损，掩盖了剩余裸仓，最终逼近强平）。

    新口径：仍挂在挂单列表（未触发）→ 直接判未成交（零额外请求，常态）；已不在
    列表 → 查 algoStatus 确认 FINISHED 才算成交；查询失败回落旧口径（不在列表即
    视为成交），避免识别失败阻塞正常结算。
    """
    if not order_id or int(order_id) in open_ids:
        return False
    filled = _tp_algo_filled(trader, symbol, order_id)
    return True if filled is None else filled


def _sl_algo_rejected(trader: BinanceTrader, symbol: str, order_id) -> bool:
    """SL 条件单是否被交易所拒单（algoStatus=REJECTED）。

    补挂的 SL 触发时被拒 → 仓位不会平，「现价越过 SL=疑似刚触发、等仓位归零」的
    等待逻辑失效，必须立即处置，否则裸仓死锁（2026-10-20 SQDUSDT 呆等 6h+）。
    查询失败/未拒单返回 False（保守，维持原逻辑）。
    """
    if not order_id:
        return False
    try:
        d = trader.algo_order_detail(symbol, int(order_id))
    except Exception:
        return False
    return str(d.get("algoStatus") or "").upper() == "REJECTED"


def _alert_naked_position(rec: TradeRecord, price: float, sl: float,
                          reason: str) -> None:
    """裸仓风险告警（飞书，best-effort，发送失败不阻断巡检）。"""
    try:
        from app.services.notify import send_feishu_text
        send_feishu_text(
            f"⚠️ 裸仓风险 {rec.symbol} {rec.direction}：{reason}\n"
            f"现价 {price} / 原止损 {sl}，该仓位已无止损保护，请人工处置。",
            testnet=bool(rec.testnet),
        )
    except Exception as e:
        logger.warning("裸仓告警发送失败 %s: %s", rec.symbol, e)


def _rehang_lost_sl(db: Session, trader: BinanceTrader, rec: TradeRecord,
                    open_ids: set[int]) -> None:
    """止损挂单存活巡检（docs/07 §8-A2）：SL algo 单消失（手动撤销/交易所异常/
    触发时被拒）时按当前止损价补挂——OPENED/TP1_HIT/TP2_HIT 状态下仓位无 SL
    保护只能等强平。

    - SL 单仍在挂单列表（或记录无 sl_order_id 的极端情况无价可挂）→ 不动作；
    - 现价已被止损价越过且 SL 非「被拒单」：疑似 SL 刚触发、持仓量尚未反映，
      跳过本轮，留待仓位归零的结算路径确认；
    - SL 单 algoStatus=REJECTED（触发时被交易所拒，仓位不会平）→ 上述等待逻辑
      失效，立即处置，避免裸仓死锁（2026-10-20 SQDUSDT：补挂 SL 触发被拒后，
      每轮都因现价越过 SL 而跳过，裸仓呆等 6h+ 直至逼近强平）；
    - 补挂抛错（如被卡在 TRIGGERING 的 reduceOnly 条件单阻塞）→ ERROR + 飞书告警。
    """
    raw = rec.raw or {}
    sl_id = raw.get("sl_order_id")
    if sl_id and int(sl_id) in open_ids:
        return
    cur_sl = float(rec.stop_loss or 0)
    if cur_sl <= 0:
        return
    price = trader.price(rec.symbol)
    overshoot = _immediate_trigger(cur_sl, price, rec.direction)
    sl_dead = _sl_algo_rejected(trader, rec.symbol, sl_id)
    if overshoot and not sl_dead:
        logger.info(
            "止损单丢失 %s：现价 %s 已越过 SL %s，疑似刚触发，下轮结算确认",
            rec.symbol, price, cur_sl,
        )
        return
    if overshoot and sl_dead:
        # Route through the durable account incident; no blanket algo cancel before
        # a confirmed position-specific close.
        from app.services.protection_recovery import create_incident_after_rollback
        try:
            create_incident_after_rollback(
                db, account_key=trader.account_key or ("testnet-1" if rec.testnet else "live-1"),
                testnet=rec.testnet, symbol=rec.symbol, direction=rec.direction,
                reason="sl_rejected_overshot", qty=float(rec.qty or 0),
                trade_record_id=rec.id,
                entry_order_id=(raw.get("entry_order_id")),
                entry_price=float(rec.entry_price or 0) or None,
                opened_at=rec.opened_at, error=f"SL {sl_id} rejected; price {price} crossed {cur_sl}",
                status="pending", rollback_session=False,
            )
        except Exception:
            logger.exception("SL拒单事故持久化失败 %s", rec.symbol)
        _alert_naked_position(rec, price, cur_sl,
                              "止损单触发被拒且现价已越过，已进入保护事故恢复队列")
        return
    try:
        _replace_sl(db, trader, rec, open_ids,
                    trader.round_price(rec.symbol, cur_sl), reason="sl_rehang")
        db.commit()
        logger.warning("止损挂单丢失已补挂 %s：SL=%s（巡检发现挂单消失）", rec.symbol, cur_sl)
    except Exception as e:
        db.rollback()
        logger.error(
            "止损补挂失败 %s：SL=%s err=%s（可能被卡在 TRIGGERING 的 reduceOnly "
            "条件单阻塞，裸仓）", rec.symbol, cur_sl, e,
        )
        _alert_naked_position(rec, price, cur_sl, "止损补挂失败（可能被卡在 TRIGGERING 的 reduceOnly 条件单阻塞）")
        raise


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


def _zero_pos_exit_reason(rec: TradeRecord, tp1_hit: bool,
                          fee_rate: Optional[float] = None) -> str:
    """仓位归零时的出场原因：OPENED 状态下 TP1 真成交（非「单号消失」），说明 TP1 与 SL
    在同一巡检间隔内先后成交（如 75% TP1 后回踩触发 SL）→ tp1_then_sl；其余按状态推断。

    tp1_hit 由 _tp_filled_real 判定（真成交，见 2026-10-20 SQDUSDT 误标修复）。"""
    if rec.status == "OPENED" and tp1_hit:
        return "tp1_then_sl"
    return _derive_exit_reason(rec, fee_rate)


def _is_tp_full_exit(rec: TradeRecord, raw: dict, tp1_hit: bool,
                     tp2_hit: bool) -> bool:
    """仓位归零是否由「止盈全平模式」的 TP 档平完全仓导致（2026-10-28）：
    本单开仓快照 tp_full_exit 开启，且实际挂出的 TP 档合计覆盖全仓
    （qty_tp1 + qty_tp2 ≥ qty；TP2 挂单失败时为 TP1 单档比例，不算全平），
    再有 TP2 的单须 TP2 真成交（TP1 后回踩止损仍算保本/跟踪出场）、
    无 TP2 的单 TP1 即全平（TP1 没挂上/未成交不算）。"""
    if not raw.get("tp_full_exit"):
        return False
    qty = float(rec.qty or 0)
    covered = float(raw.get("qty_tp1") or 0) + float(raw.get("qty_tp2") or 0)
    if qty <= 0 or covered < qty * 0.99:
        return False  # 挂单结构未覆盖全仓（如 TP2 挂单失败）
    return tp2_hit if rec.tp2 else tp1_hit


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
    # 运行中收益率为全程口径：(已实现 + 浮动) / 初始风险（成交后 1R，docs/16）
    _ru = rec.risk_unit
    rec.pnl_pct = round(
        (pnl + float(rec.unrealized_pnl or 0)) / _ru * 100, 2
    ) if _ru > 0 else None
    return pnl


def close_market_now(db: Session, trader: BinanceTrader, rec: TradeRecord,
                     exit_reason: str = "manual") -> dict:
    """市价平仓（飞书卡片按钮，2026-09-21）：reduceOnly 市价全平 → 清理挂单 →
    立即按 income 口径结算落终态（复用巡检零仓位的费用拆分/事件口径）。

    exit_reason: 落库出场原因（默认 "manual"=飞书按钮手动平仓；4h 状态机
    下车传 "htf_state"，docs/21 O1——不传则程序性全平会被误标手动平仓）。

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
    rec.remaining_risk = 0  # 终态无剩余风险
    pnl = round(trader.sum_income(rows, trader.INCOME_KEEP), 6)
    rec.realized_pnl = pnl
    _ru = rec.risk_unit
    rec.pnl_pct = round(pnl / _ru * 100, 2) if _ru > 0 else None
    rec.status = "CLOSED"
    rec.closed_at = datetime.utcnow()
    rec.exit_reason = exit_reason
    _add_event(db, rec, "MANUAL_CLOSE", {
        "qty": float(close_qty), "realized_pnl": pnl, "pnl_pct": rec.pnl_pct,
        "exit_reason": exit_reason, "fee_total": rec.fee_total,
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
    # 生效 AI 凭证读页面配置（2026-10-26：Key+端点成对才生效，缺一整体回落
    # env）——巡检路径 advice_due 的 current_api_key() 依赖本进程 runtime
    configure_from(cfg)
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
    # HL/LH 结构确认深度：跟扫描配置页「摆动点阶数」（与扫描同一把尺子）
    swing_order = _trail_swing_order(cfg)
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
            # TP「真成交」判定（2026-10-20 SQDUSDT 误标修复）：不再只看「单号不在挂单
            # 列表」——TRIGGERING/EXPIRED/REJECTED/CANCELED 都不算成交（见 _tp_filled_real）
            tp1_hit = _tp_filled_real(rt, rec.symbol, raw.get("tp1_order_id"), open_ids)
            tp2_hit = _tp_filled_real(rt, rec.symbol, raw.get("tp2_order_id"), open_ids)

            # 仓位归零 → 结算（净盈亏 = 已实现盈亏 − 手续费 − 资金费 − 强平清算，正/负值）
            if qty <= 0 or amt < qty * 0.05:
                # 清理残留挂单（SL 先成交时 TP reduceOnly 单会一直挂着）
                rt.cancel_all_algo(rec.symbol)
                # 起点回拨 5s：开仓手续费 income 的时间戳可能略早于 opened_at（同 _record_realized）
                since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
                rows = rt.income_rows(rec.symbol, since_ms)
                # 强平识别：逐仓保证金亏光时交易所强制平仓并撤销全部挂单（含 TP），
                # 会产生 INSURANCE_CLEAR 清算记录——此时 TP1 挂单消失≠TP1 成交，
                # 不能按 tp1_hit 误判为 tp1_then_sl
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
                rec.remaining_risk = 0  # 终态无剩余风险
                pnl = round(rt.sum_income(rows, rt.INCOME_KEEP), 6)
                rec.realized_pnl = pnl
                _ru = rec.risk_unit
                rec.pnl_pct = round(pnl / _ru * 100, 2) if _ru > 0 else None
                if insurance_clear != 0:
                    _add_event(db, rec, "LIQUIDATION", {
                        "qty": qty, "insurance_clear": round(insurance_clear, 6),
                        "realized_pnl": pnl,
                    })
                    exit_reason = "liquidation"
                else:
                    if rec.status == "OPENED" and tp1_hit:
                        # TP1 与 SL 在同一巡检间隔内先后成交（如 75% TP1 后回踩触发 SL）：补记 TP1_FILL
                        _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1"),
                                                         "realized_pnl": round(pnl, 6)})
                    # 止盈全平模式（2026-10-28）：TP 档直接平完全仓——有 TP2 的单需
                    # TP2 真成交才算（TP1 后回踩止损仍是保本/跟踪出场）；无 TP2 的单
                    # TP1 即全平（TP1_FILL 已在上方补记）。已升到 TP2_HIT 的单，归零
                    # 来自后续止损/跟踪，不按 tp_full 记
                    tp_full = rec.status in ("OPENED", "TP1_HIT") and \
                        _is_tp_full_exit(rec, raw, tp1_hit, tp2_hit)
                    if tp_full and rec.tp2 and tp2_hit:
                        _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2"),
                                                         "realized_pnl": round(pnl, 6)})
                    # 程序主动全平（2026-10-12 策略二删除后不再产生新 override；
                    # 存量在跑 trail 单/历史 htf_state 仍按此消费，防误标止损类出场）→
                    # exit_reason=htf_state 之类，不让 bookkeeping 推断误标
                    if raw.get("exit_override"):
                        exit_reason = raw["exit_override"]
                    elif tp_full:
                        exit_reason = "tp_full"
                    else:
                        exit_reason = _zero_pos_exit_reason(rec, tp1_hit, fee_rate)
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
                # 黑名单自动登记（D，2026-09-18）：实亏 ≥1.5R 或 14 天内 3 次止损
                # → 冷却 14 天；≥1.8R 或强平（按倍数）→ 永久档。失败不阻断结算
                try:
                    from app.services.blacklist_service import register_settle
                    # 超预算判定按「计划风险」口径（滑点后的 initial_risk 不作为预算基准）
                    register_settle(db, rec.symbol, rec.planned_risk or rec.risk_amount,
                                    rec.realized_pnl, rec.exit_reason or "",
                                    trade_id=rec.id)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning("黑名单登记失败 %s: %s", rec.symbol, e)
                continue

            ratio = amt / qty
            # 剩余风险随交易所实际剩余量刷新（覆盖 TP 分批、手动减仓与部分止损成交）
            rec.remaining_risk = _remaining_risk(rec, qty=amt)
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
            # 全程收益率分母 = 成交后初始风险（滑点后的真实 1R）
            _ru = rec.risk_unit
            if _ru > 0:
                rec.pnl_pct = round(total_pnl / _ru * 100, 2)
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
                if tp2_hit and ratio <= 0.35:
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
                    # 全平模式残仓兜底（docs/23 T14e）：TP2 数量被最小步长取整、
                    # 或旧单 TP2 只覆盖部分仓位时，仍会余下尾仓。补平成功就不再
                    # 给已平掉的仓位挂跟进止损（否则会挂出无仓位可保的孤儿单）
                    if not _enforce_full_exit_residual(db, rt, rec, amt, ratio):
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k,
                                         swing_order=swing_order)
                    db.commit()
                    handled += 1
                elif tp1_hit and (ratio <= 0.75 or raw.get("tp_full_exit")):
                    realized = _record_realized(db, rt, rec)
                    tranche = _tranche_pnl(rt, rec, raw)
                    _add_event(db, rec, "TP1_FILL", {"qty": raw.get("qty_tp1"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP1_HIT"
                    _move_sl_breakeven(db, rt, rec, open_ids, fee_rate)  # 用户规则：TP1 后止损移至保本价（成本价±往返费缓冲）
                    if not rec.tp2:
                        # 无 TP2：TP1 止盈 80% 后剩余仓即启用跟进止损（与 TP2 后同机制）
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k,
                                         swing_order=swing_order)
                    db.commit()
                    handled += 1
                else:
                    # 仓位被缩减但 TP 未成交 → 非系统止盈腿的减仓（止损 closePosition 部分
                    # 成交 / 交易所侧手动减仓）。2026-10-20 SQDUSDT：止损部分成交曾被误标
                    # TP1_HIT，掩盖了剩余裸仓。此处只记异常事件、保留 OPENED，保护交由止损
                    # 存活巡检，绝不按「仓位缩小」推断止盈成交。
                    if ratio <= 0.75 and not raw.get("partial_close_flagged"):
                        _add_event(db, rec, "PARTIAL_CLOSE_ANOMALY", {
                            "ratio": round(ratio, 4), "amt": amt, "qty": qty,
                        })
                        rec.raw = {**raw, "partial_close_flagged": True}
                        db.commit()
                        handled += 1
                    logger.warning(
                        "仓位被缩减但未检到止盈成交 %s：剩余比例 %.2f ≤0.75，保留 OPENED"
                        "（不误标 TP1_HIT），仅执行止损存活保护", rec.symbol, ratio,
                    )
            elif rec.status == "TP1_HIT":
                if tp2_hit and ratio <= 0.35:
                    realized = _record_realized(db, rt, rec)
                    tranche = _tranche_pnl(rt, rec, raw)
                    _add_event(db, rec, "TP2_FILL", {"qty": raw.get("qty_tp2"),
                                                     "pnl": tranche,
                                                     "cum_pnl": realized})
                    rec.status = "TP2_HIT"
                    # 全平模式残仓兜底（docs/23 T14e）：TP2 数量被最小步长取整、
                    # 或旧单 TP2 只覆盖部分仓位时，仍会余下尾仓。补平成功就不再
                    # 给已平掉的仓位挂跟进止损（否则会挂出无仓位可保的孤儿单）
                    if not _enforce_full_exit_residual(db, rt, rec, amt, ratio):
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k,
                                         swing_order=swing_order)
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
                        _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k,
                                         swing_order=swing_order)
                    db.commit()
                    handled += 1
            elif rec.status == "TP2_HIT":
                # 全平模式残仓兜底（docs/23 T14e）：兜住部署前已升到 TP2_HIT 的
                # 历史单（raw 有 tp_full_exit 且无补平标记），每个单只补一次
                if _enforce_full_exit_residual(db, rt, rec, amt, ratio):
                    db.commit()
                    handled += 1
                    continue
                # 每小时跟进止损（只收紧不放松）
                _move_sl_trailing(db, rt, rec, interval, open_ids, atr_k=atr_k,
                                  swing_order=swing_order)
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
                if send_app_card(_open_feishu_card(rec), testnet=bool(rec.testnet)):
                    sent += 1
            except Exception as e:
                logger.warning("开仓卡片发送失败 %s: %s", tid, e)
    finally:
        db.close()
    return sent


def _live_off_card(
    a: AIAnalysis, img_key: str = "", open_account_key: str = "",
    both_off: bool = False, pos: Optional[float] = None, pos_min: float = 0.0,
) -> dict:
    """自动开单关闭提醒卡片（2026-10-09）：价格栅格复用开仓卡片风格，
    橙色头区分（蓝=开仓成功、橙=达标信号错过开仓）。

    2026-10-15 增强：内嵌服务端渲染的 K 线图（img_key 非空时挂 img 元素，
    K线 + EMA21/55/144 + 入场/止损/止盈计划线，图由 kline_chart 画、feishu_bot
    上传）；正式网仅一个启用账号且配了按钮令牌时附「开单」按钮（回调走
    feishu_bot._exec_open_live → manual_open_from_analysis，与网页开单同源）。
    两者任一不可用都退化为原纯文字卡片。

    2026-10-27：both_off=True 表示**双网自动交易全关**（此前这种状态在
    ai_tasks._dispatch_immediate_open 直接 return，达标信号连提醒都没有，
    18:00 那批 9 条 suggest 静默丢失）。文案随之改为「双网均关」，按钮仍
    只指向正式网单账号——测试网自动开单本就不该由卡片代劳。

    2026-10-28（docs/16 §18.5 D4）：补「入场位置 pos」一行。双网全关时每笔都是
    手动单，而手动单 force_market=True 按设计跳过 entry_pos 闸——ELSAUSDT 就是
    pos=0.31 一路放行（§14.3）。卡片是用户点「开单」前唯一看到的界面，必须把
    「这一单自动开仓会被拦」提前告知，否则用户在不知情下开了一笔系统本不认的单。
    """
    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    is_long = a.direction == "long"
    recmd = f"{float(a.recommendation):.0f} 分" if a.recommendation is not None else "—"
    rr = f"{float(a.risk_reward_ratio):.1f}" if a.risk_reward_ratio else "—"
    elements: list = [
        {"tag": "div", "text": {"tag": "lark_md", "content": (
            "该信号已达标可开单，但**双网自动开单均已关闭**，本次未下任何单。"
            if both_off else
            "该信号已达标可开单，但**正式网自动开单开关关闭**，本次未开实盘单（测试网照常）。"
        )}},
    ]
    if img_key:
        elements.append({
            "tag": "img", "img_key": img_key,
            "alt": {"tag": "plain_text", "content": f"{a.symbol} K线图（EMA 均线与开单计划）"},
        })
    elements += [
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
    ]
    if pos is not None:
        _below = pos_min > 0 and pos < pos_min
        _txt = (f"入场位置 **pos {pos:.2f}**"
                + (f"（低于自动开仓门槛 {pos_min:.2f}，**自动开仓会被拦下**；"
                   "手动开单不会被拦）" if _below
                   else (f"（≥ 门槛 {pos_min:.2f}）" if pos_min > 0 else "")))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": (
            f"<font color='red'>{_txt}</font>" if _below else _txt)}})
    # 「开单」按钮：正式网仅一个启用账号（多账号群聊里点一下不知道开哪个，不给
    # 误开实盘的机会）且配了回调令牌时才挂；点击 → open_live 回调分支
    if open_account_key and settings.FEISHU_ACTION_TOKEN:
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "type": "primary",
            "text": {"tag": "plain_text",
                     "content": f"开单 · 正式网 {open_account_key}"},
            "value": {"act": "open_live", "token": settings.FEISHU_ACTION_TOKEN,
                      "analysis_id": str(a.id), "account_key": open_account_key},
        }]})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
     "content": (f"战法 {a.tactic_name} · " if a.tactic_name else "")
     + ("如需开单可在扫描结果/交易记录手动开单，或开启任一网络的自动开单"
        if both_off else
        "如需实盘可在扫描结果/交易记录手动开单，或开启正式网自动开单")}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": (
                f"⚠️ 可开单未下单（双网均关）· {a.symbol}" if both_off
                else f"⚠️ 正式网未开单 · {a.symbol}"
            )},
            "template": "orange",
        },
        "elements": elements,
    }


def _live_off_img_key(a: AIAnalysis, cfg) -> str:
    """正式网未开单卡片的 K 线图 img_key（拉 K 线 → 渲染 PNG → 上传飞书）。

    任一环节失败返回 ""（调用方降级为纯文字卡片）——画图/上传是锦上添花，
    不能因它让「信号达标却没开实盘单」这条提醒整体丢失。K 线周期取系统配置
    周期（与该信号的分析/扫描同源），300 根供 EMA144 递推；allow_stale
    容忍交易所瞬断（画一张略旧的图好过没图）。
    """
    from app.services.exchange_pool import ExchangePool
    from app.services.feishu_bot import upload_image
    from app.services.kline_chart import render_plan_chart

    interval = (getattr(cfg, "kline_interval", None) or "1h") if cfg is not None else "1h"
    try:
        klines = ExchangePool().get_klines(a.symbol, interval, 300, allow_stale=True)
        png = render_plan_chart(
            klines, symbol=a.symbol, direction=a.direction,
            entry=float(a.entry_price), stop_loss=float(a.stop_loss),
            take_profit_1=float(a.take_profit_1),
            take_profit_2=float(a.take_profit_2) if a.take_profit_2 else None,
            interval=interval,
            ai_score=float(a.recommendation) if a.recommendation is not None else None,
        )
        return upload_image(png) if png else ""
    except Exception:
        logger.exception("正式网未开单卡片 K 线图渲染/上传失败（降级纯文字卡）")
        return ""


def _live_off_pos(a: AIAnalysis, cfg) -> tuple[Optional[float], float]:
    """卡片上的「入场位置 pos 与其门槛」（2026-10-28，D4）。

    返回 (pos, pos_min)。pos 为 None 表示算不出来（K 线拉取失败、币价异常、
    信号价落在区间外），此时卡片不显示该行——宁可少一行，也不猜。
    pos_min 取 settings.ENTRY_POS_MIN_RATIO（0=闸门关闭，卡片只报数值不报结论）。
    """
    try:
        from app.services.entry_diagnostics import bar_position, closed_bars
        from app.services.exchange_pool import ExchangePool

        pos_min = float(getattr(settings, "ENTRY_POS_MIN_RATIO", 0.0) or 0.0)
        price = float(a.entry_price or 0)
        if price <= 0:
            return None, pos_min
        interval = (getattr(cfg, "kline_interval", None) or "1h") if cfg is not None else "1h"
        klines = ExchangePool().get_klines(a.symbol, interval, 60, allow_stale=True)
        pos = bar_position(closed_bars(klines), price)
        return (round(pos, 4) if pos is not None else None), pos_min
    except Exception:
        logger.warning("卡片入场位置计算失败（省略该行） symbol=%s", a.symbol, exc_info=True)
        return None, 0.0


def _off_card_suppressed(symbol: str, direction: str) -> bool:
    """「双网全关·可开单」卡片按 (币种, 方向, UTC 日) 去重（2026-10-27）。

    双网全关时每轮扫描都可能有 5~10 条达标建议，逐条推会变成整点刷屏；
    同一币种同一方向在一个 UTC 日内只提醒一次。Redis 不可用/异常时**放行**
    （宁可多一条，也不能因为缓存故障把提醒整体吞掉）。
    """
    try:
        from datetime import datetime as _dt

        from redis import Redis

        key = f"feishu:off_card:{symbol}:{direction}:{_dt.utcnow():%Y%m%d}"
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        # SET NX：第一次写入成功=本日首推；已存在=今天推过，跳过
        return not bool(r.set(key, "1", nx=True, ex=86400))
    except Exception:
        logger.warning("可开单提醒去重缓存不可用，放行推送 symbol=%s", symbol, exc_info=True)
        return False


def notify_live_auto_open_off(
    db: Session, cfg, analysis_id: str, both_off: bool = False,
) -> None:
    """自动开单开关关闭 → 达标信号错过开仓，飞书提醒（2026-10-09）。

    「可以开单」口径 = 开仓资格快速过滤（analysis_open_eligible，与开仓闸门
    同源；评分门槛分发层已过，此处复验防门槛中途上调）。不含账号维度闸门
    ——自动开单关着本就没跑到账号闸门那一步，无法也不必预判其结果。
    该分析正式网任一账号已有成交（含此前提醒后手动开的单）则不提醒。
    每条分析至多随任务分发提醒一次；发送失败仅记日志不影响开仓扇出。

    both_off=True（2026-10-27）：双网自动交易全关时由 ai_tasks.
    _dispatch_immediate_open 在提前返回前调用——此前这种状态直接 return，
    达标信号连提醒都没有（18:00 那批 9 条 suggest 静默丢失）。

    2026-10-15：卡片内嵌 K 线图（K线 + EMA 均线 + 开单计划线，见 kline_chart）
    并在正式网仅一个启用账号时附「开单」按钮（点击 → feishu_bot._exec_open_live）。
    画图/上传失败自动退化为纯文字卡片；本函数在竞态上安全——它不持开仓锁，
    K 线拉取与图片上传（约 1~2s）只延后本任务后续的账号扇出，不影响开仓结果。
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
        # 正式网启用账号恰好一个时才挂「开单」按钮（多账号群聊里点一下不知道
        # 开哪个账号，不给误开实盘单的机会）
        from app.services.account_registry import live_enabled_accounts
        live_accs = live_enabled_accounts(db)
        open_acc = live_accs[0].key if len(live_accs) == 1 else ""
        # 双网全关态整点批量推送，按 (币种, 方向, UTC 日) 去重防刷屏
        if both_off and _off_card_suppressed(a.symbol, a.direction or ""):
            logger.info("可开单提醒今日已推送，跳过 symbol=%s analysis=%s", a.symbol, analysis_id)
            return
        img_key = _live_off_img_key(a, cfg)
        _pos, _pos_min = _live_off_pos(a, cfg)
        if send_app_card(_live_off_card(
            a, img_key=img_key, open_account_key=open_acc, both_off=both_off,
            pos=_pos, pos_min=_pos_min,
        )):
            logger.info(
                "已发送%s提醒 analysis=%s symbol=%s 图=%s 按钮=%s",
                "双网自动开单全关" if both_off else "正式网开关关闭",
                analysis_id, a.symbol, bool(img_key), open_acc or "无",
            )
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
        # 内部字段：发送侧日志与测试网通知过滤用（飞书忽略未知键，
        # 调用方发送前 pop 掉再入卡；见 trade_tasks.run_auto_trade_task）
        "_symbol": rec.symbol,
        "_hours": hours,
        "_testnet": bool(rec.testnet),
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
                   stop_loss: float, tp1: float = 0, tp2: float = 0) -> TradeRecord:
    """导入持仓：把交易所已有手动仓接管为系统在跑单（status=OPENED）。

    流程：持仓仍在校验 → 无同账号同币在跑记录 → 三价方向校验（按现价；止盈位
    必填，2026-10-12 起无止盈位的纯跟踪接管随策略二退役）→ 撤原手动条件单 →
    按系统规则重挂 SL + TP1/TP2（失败尽力恢复原 SL，绝不把用户裸仓留在市场）
    → 落库（opened_at=接管时刻，管理范围从此刻起）。
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

    # ③ 三价方向校验（按现价；导入单不做盈亏比/风险预算闸门——尊重用户既定计划）。
    # 止盈位必填（2026-10-12）：无止盈位的纯跟踪（trail）接管随策略二退役
    stop_loss, tp1, tp2 = float(stop_loss), float(tp1 or 0), float(tp2 or 0)
    price_now = trader.price(symbol)
    if stop_loss <= 0:
        raise ValueError("止损价必填")
    if tp1 <= 0:
        raise ValueError("止盈价必填：无止盈位的纯跟踪（trail）接管已随策略二退役（2026-10-12）")
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
    qty_tp1, qty_tp2, _tp_full = _tp_order_quantities(
        trader, symbol, qty, tp2, _adopt_cfg, min_qty=f["min_qty"])
    tp1_order = None
    tp2_order = None
    if qty_tp1 >= f["min_qty"]:
        try:
            tp1_order = trader.take_profit_reduce(
                symbol, close_side, qty_tp1,
                trader.round_price(symbol, tp1))
        except Exception as e:
            logger.warning("导入 %s：TP1 挂单失败（SL 已就位，仅缺自动止盈）: %s", symbol, e)
    # 全平模式允许两档合计恰好等于全仓；关闭时须严格小于（保留跟进尾仓）
    if tp2 > 0 and tp1_order and qty_tp2 >= f["min_qty"] and _tp_sum_ok(
            qty_tp1, qty_tp2, qty, _tp_full):
        try:
            tp2_order = trader.take_profit_reduce(
                symbol, close_side, qty_tp2,
                trader.round_price(symbol, tp2))
        except Exception as e:
            logger.warning("导入 %s：TP2 挂单失败: %s", symbol, e)

    # ⑤ 落库：opened_at=接管时刻（管理范围从此刻起，接管前盈亏不回溯）
    now = datetime.utcnow()
    notional = qty * entry
    margin_used = float(pos.get("initial_margin") or 0)
    # 接管无下单计划，三口径按接管时刻风险回填（后续 TP/移损同步 remaining_risk）
    adopt_risk = round(abs(entry - stop_loss) * qty, 4)
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
        risk_amount=adopt_risk,  # 实际风险 = 止损距离 × 数量（接入即初始风险）
        planned_risk=adopt_risk,
        initial_risk=adopt_risk,
        remaining_risk=adopt_risk,
        risk_budget_mode=(
            (_adopt_cfg.risk_budget_mode or "pct")
            if _adopt_cfg is not None else "pct"
        ),  # 接管时刻模式快照（无 AI 分析行的手动单同样记录）
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2 if tp2_order else 0,
        trailing_mode=TRAIL_MODE_THREE_LINE,  # 接管单同默认三线止损（2026-10-21 拍板改回；HL/LH 保留可切换）
        status="OPENED",
        opened_at=now,
        raw={
            "adopted": True,
            "sl_order_id": sl_order.get("algoId") or sl_order.get("orderId"),
            "tp1_order_id": (tp1_order.get("algoId") or tp1_order.get("orderId")) if tp1_order else None,
            "tp2_order_id": (tp2_order.get("algoId") or tp2_order.get("orderId")) if tp2_order else None,
            "qty_tp1": qty_tp1 if tp1_order else 0,
            "qty_tp2": qty_tp2 if tp2_order else 0,
            # 止盈全平模式快照（2026-10-28）：同开仓路径，巡检按本单口径结算
            "tp_full_exit": _tp_full,
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
        "risk_amount": adopt_risk,
        "planned_risk": adopt_risk, "initial_risk": adopt_risk,
        "remaining_risk": adopt_risk,
        "entry_price": entry,
        "note": "手动持仓导入（管理范围自接管时点起）",
    })
    db.commit()
    logger.info("已导入持仓 %s %s qty=%s entry=%s sl=%s tp1=%s tp2=%s（风险 %s USDT）",
                symbol, direction, qty, entry, stop_loss,
                tp1, tp2 if tp2_order else 0, rec.risk_amount)
    return rec


def auto_adopt_positions(db: Session, trader: BinanceTrader) -> list[str]:
    """自动导入手动持仓（2026-09-24，随每小时结算巡检执行）：
    交易所非零持仓中未被系统在跑记录接管的 = 用户在币安 App 手动开的单，
    逐一尝试 adopt_position 接管（SL/TP 取 App 现挂条件单原值）。

    止盈位必填（2026-10-12）：无止盈位的手动仓不再自动接管（adopt_position
    抛 ValueError → 本函数记日志跳过）。跳过：已在跑记录的、无止损条件单的
    （裸仓不入册，只记日志）、无止盈条件单的、三价校验不过的。需在开仓锁内
    调用。返回新导入的 symbol 列表。
    """
    out: list[str] = []
    for row in list_adoptable_positions(db, trader):
        if row["tracked"]:
            continue
        sym = row["symbol"]
        pre = row["prefill"]
        if not pre["stop_loss"]:
            logger.info("自动导入跳过 %s：币安未设置止损条件单（裸仓不入册）", sym)
            continue
        try:
            adopt_position(db, trader, sym, pre["stop_loss"],
                           pre["tp1"] or 0, pre["tp2"] or 0)
            out.append(sym)
        except ValueError as e:
            logger.info("自动导入跳过 %s：%s", sym, e)
        except Exception:
            logger.exception("自动导入失败 %s", sym)
    return out
