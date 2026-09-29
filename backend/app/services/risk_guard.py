"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

校验链已收缩（2026-09-15 拍板：② AI 分析校验链仅保留两条——RR 铁律 + 双评委辩论复核）：
- Schema 强约束（Pydantic）+ suggest 基本项（方向/开单类型/三价为正）
- **RR 铁律**：按 AI 给的三价复算盈亏比 ≥ AI_RR_MIN(1.5)，声称值不信
- 仓位公式化（固定亏损法）：仓位 = 风险预算 ÷ 止损距离。风险预算两种口径
  （2026-09-27 止损金额模式，resolve 见 risk_budget.py）：pct=触止损恰好亏
  RISK_BUDGET_PCT（3%，默认）；fixed=亏固定 USDT 实额（position_pct 不折算），
  AI 不自报仓位

已移除的校验（方法论降级为 AI 提示词纪律，程序不再打回/改挂）：
- 入场偏离现价 ≤2%（2026-09-15 移除）
- 方向铁律入场贴近结构位 ±0.3%（2026-09-15 早些时候移除）
- 止损锚定（结构锚 vs 影线极值取更远者 + 0.3% 缓冲）、止损宽度 [0.3×ATR, 3×ATR]、
  强平安全上限前置、止盈最近优先强制（docs/09 的锚定设计保留为提示词方法论）

方向-价格一致性（2026-09-16 恢复为硬校验，不属于方法论）：多单 SL<entry<TP1、
空单 TP1<entry<SL（TP2>0 时同侧递延）。2026-09-15 随校验链收缩移除后，
ETHUSDT 空单反向止损（SL=2373 < 入场 2391.89）靠 abs() 口径的 RR 铁律
畅通落库，到挂 SL 才会被交易所拒单——这是可行性铁律而非结构偏好，不可省。

放水后的兜底在开仓闸门（trade_engine._attempt_open）：方向-价格复核、现价偏离
≤1%、RR 按现价复算 ≥1.5、强平闸门（liquidation_gate_pct，本模块提供共用口径）、
保证金可用性——交易所侧最后一道防线。
"""
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

# 开单类型（结构打法归类，docs/04 §4 Stage 3；skip 时为空）
# 2026-09-12 起三种破位/回踩类打法（123法则·N字结构·2B法则）合并为 structure_break
TRADE_TYPE_LABELS = {
    "trend_follow": "顺势交易",
    "structure_break": "结构破位回踩",
    "range_edge": "区间边缘反转",
}
TRADE_TYPES = tuple(TRADE_TYPE_LABELS)

# 合并前的历史类型（旧分析/旧交易快照仍可能携带，仅作展示，不再接受 AI 新输出）
LEGACY_TRADE_TYPE_LABELS = {
    "rule_123": "123法则（已并入结构破位回踩）",
    "n_structure": "N字结构（已并入结构破位回踩）",
    "rule_2b": "2B法则（已并入结构破位回踩）",
}


class TradeDecision(BaseModel):
    """AI 决策契约（P0：AI 报绝对价格；P1 升级为结构位锚点 level_ref+offset）"""

    trade_decision: Literal["suggest", "skip"]
    skip_reason: str = ""
    direction: Optional[Literal["long", "short"]] = None
    trade_type: Optional[str] = None  # 开单类型：trend_follow / structure_break / range_edge
    # 止损锚类型（docs/23，仅突破类信号需要）：structure=结构锚（默认）/ near=突破K极值近锚
    stop_anchor: Optional[Literal["structure", "near"]] = None
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_reward_ratio: float = 0.0
    position_pct: float = 0.0
    recommendation: float = 0.0
    analysis: str = ""


_NUM_FIELDS = (
    "entry_price", "stop_loss", "take_profit_1",
    "take_profit_2", "risk_reward_ratio", "position_pct", "recommendation",
)


def normalize_raw(raw: dict) -> TradeDecision:
    """宽松归一化：AI 可能输出字符串数字 / direction 空串等，先转成严格类型"""
    data = dict(raw or {})
    for f in _NUM_FIELDS:
        v = data.get(f)
        if isinstance(v, str):
            try:
                data[f] = float(v.strip())
            except ValueError:
                data[f] = 0.0
        elif v is None:
            data[f] = 0.0
    if data.get("direction") == "":
        data["direction"] = None
    # stop_anchor 非法值回退 None（suggest 落库时再回退 structure；docs/23）
    if data.get("stop_anchor") not in ("structure", "near", None, ""):
        data["stop_anchor"] = None
    if data.get("trade_decision") not in ("suggest", "skip"):
        raise ValueError(f"trade_decision 非法: {data.get('trade_decision')!r}")
    return TradeDecision(**data)


# 逐仓强平闸门口径（docs/07 §8-A4）：止损距离超过 1/杠杆 − 维持保证金率 时，
# 价格未到止损价保证金就先亏光 → 被强平。
# MMR 必须按币种实际值取（trade_engine 经 binance_trader.bracket_mmr 查
# /fapi/v1/leverageBracket）：BTC 0.4%、主流币 0.5%、小币第 1 档普遍 1.5%-2.5%。
# 写死 0.005 的旧口径放行了 4USDT（MMR=1.5%）止损 4.2% 的单子——实际跌 3.5%
# 即被强平，SL 形同虚设（2026-09-17 案例，实际亏损超风险预算 10%）。
# 缓冲 LIQ_GATE_BUFFER 覆盖：开平 taker 手续费（约 0.1% 名义）+ 持仓资金费
# （小币单笔可达 0.2%+ 名义，swing 持仓多日）+ 强平触发价差。
# trade_engine 开仓闸门（_attempt_open）使用此口径；risk_guard 侧的前置打回
# 已随"仅保留 RR 铁律"移除（2026-09-15），强平保护由开仓闸门承担
LIQ_GATE_MMR = 0.005  # mmr 未提供时的默认（主流币口径）
LIQ_GATE_MMR_FALLBACK = 0.025  # bracket API 拿不到时的保守兜底（覆盖小币高 MMR）
LIQ_GATE_BUFFER = 0.005

# 入场偏离容差（system_config.pullback_market_pct 可页面覆盖，NULL=默认 0.01）：
# 开仓时现价对 AI 入场价的最大偏离。2026-09-18 起一兼两职：
# ① 普通单的开仓漂移上限（原写死 1%）；② 待回踩单的市价入场判定——
# 回踩距离 ≤ 容差时市价进场（RR 按现价复算仍须 ≥AI_RR_MIN，见开仓闸门），
# 超容差才维持「待回踩不下单」。回踩不远时市价进也能满足盈亏比，不必干等
PULLBACK_MARKET_PCT = 0.01

# 近锚一致性校验（docs/23，2026-09-29）：突破类信号 AI 声明 stop_anchor=near 时，
# 止损必须贴近突破K线极值（信号K线，klines[-2]；klines[-1] 未收盘）——提示词要求
# 突破K极值外 0.3% 缓冲，容差 0.5% 覆盖合理偏移；AI 挂近锚名义报远离止损（把
# 结构锚止损伪装成近锚）时程序打回。数据不足时放行（防数据异常锁死开单，
# 同 MAX_SLIPPAGE 接口抖动不阻断的哲学），仅记 warning
NEAR_ANCHOR_BUFFER = 0.003  # 与提示词口径一致：极值外 0.3%
NEAR_ANCHOR_TOLERANCE = 0.005  # 止损对锚价的最大相对偏离
BREAKOUT_SIGNAL_TYPES = ("trigger_breakout", "trigger_trend_breakout")

# 开仓前盘口滑点实测上限（system_config.max_slippage_pct 可页面覆盖，NULL=默认 0.003）：
# 按本单数量走订单簿逐层吃的加权均价对最优价的偏离（0.003=0.3%）。GRIFFAIN 案例：
# 簿薄的小币吃单滑点 0.637% → 实际止损距离拉宽 17%、风险超预算。None 返回（簿异常）
# 不阻断，避免接口抖动锁死开单
MAX_SLIPPAGE_PCT = 0.003


def liquidation_gate_pct(
    leverage: Optional[int] = None,
    mmr: Optional[float] = None,
    buffer: Optional[float] = None,
) -> float:
    """逐仓强平闸门：止损距离占价格的最大安全比例（1/杠杆 − MMR − 缓冲）"""
    lev = leverage or settings.TRADING_LEVERAGE
    return 1 / lev - (LIQ_GATE_MMR if mmr is None else mmr) - (LIQ_GATE_BUFFER if buffer is None else buffer)


def is_pullback_wait(ai_result: dict, current_price) -> bool:
    """AI 建议等回踩判定（2026-09-15）：suggest 且入场价在现价的回踩侧——
    多单入场低于现价、空单高于现价（提示词契约：等回踩时 entry 锚定回踩结构位）。

    限价委托已放弃（docs/10），此类建议无法按 AI 计划价位成交——仅落
    "待回踩"标识供展示，程序不为其下单（即时开仓分发与开仓快速过滤都会跳过）。
    """
    if ai_result.get("trade_decision") != "suggest":
        return False
    direction = (ai_result.get("direction") or "").lower()
    try:
        entry, cur = float(ai_result.get("entry_price") or 0), float(current_price or 0)
    except (TypeError, ValueError):
        return False
    if direction not in ("long", "short") or entry <= 0 or cur <= 0:
        return False
    return entry < cur if direction == "long" else entry > cur


def validate_decision(
    d: TradeDecision, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
) -> tuple[bool, list[str], dict]:
    """校验并修正决策（2026-09-15 拍板收缩：仅保留 RR 铁律 + 仓位复算）。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
    signal / klines 保留入参以稳定调用方接口（当前校验不再使用）。
    risk_budget（2026-09-27 止损金额模式）：fixed 模式下为单笔固定亏损 USDT
    实额（调用方从 system_config.risk_fixed_amount 传入），此时 position_pct
    置 None（仓位%依赖钱包，分析期不可得）；None 时按 RISK_BUDGET_PCT 百分比
    口径复算（pct 模式，现行默认）。
    """
    violations: list[str] = []

    # ── skip：只要求理由非空（recommendation 超标仅修正，不浪费重试）──
    if d.trade_decision == "skip":
        if not d.skip_reason.strip():
            violations.append("skip 时必须给出 skip_reason 理由")
            return False, violations, {}
        return True, [], {
            "trade_decision": "skip",
            "skip_reason": d.skip_reason.strip(),
            "direction": None,
            "trade_type": None,
            "analysis": d.analysis or "",
            "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
            "take_profit_2": 0, "risk_reward_ratio": 0,
            "position_pct": 0,
            "recommendation": min(d.recommendation, 30),
        }

    # ── suggest 基本项 ──
    if not d.direction:
        violations.append("suggest 时必须给出 direction (long/short)")
    if d.trade_type not in TRADE_TYPES:
        violations.append(
            f"suggest 时必须归类开单类型 trade_type（{'/'.join(TRADE_TYPES)} 之一）"
        )
    if d.entry_price <= 0 or d.stop_loss <= 0 or d.take_profit_1 <= 0:
        violations.append("suggest 时 entry/stop_loss/tp1 必须为正数价格")
    if violations:
        return False, violations, {}

    # ── 方向-价格一致性硬校验（2026-09-16 恢复为程序强制；ETHUSDT 案例）──
    # 多单 SL<entry<TP1、空单 TP1<entry<SL 是交易所可挂性铁律：反向止损的
    # 条件单会被拒单（-2021 would immediately trigger），走到开仓→挂SL失败→
    # 紧急平仓空转。RR 铁律全程 abs() 对方向盲，反向止损照样达标
    # （ETHUSDT 空单 SL=2373 低于入场 2391.89，RR 复算 1.58 畅通通过）。
    # TP2>0 时须与方向同侧且比 TP1 更远
    if d.direction == "long" and not d.stop_loss < d.entry_price < d.take_profit_1:
        violations.append(
            f"多单价格序矛盾：须 SL < 入场 < TP1"
            f"（实得 SL={d.stop_loss}，入场={d.entry_price}，TP1={d.take_profit_1}）"
        )
    if d.direction == "short" and not d.take_profit_1 < d.entry_price < d.stop_loss:
        violations.append(
            f"空单价格序矛盾：须 TP1 < 入场 < SL"
            f"（实得 TP1={d.take_profit_1}，入场={d.entry_price}，SL={d.stop_loss}）"
        )
    if d.take_profit_2 > 0:
        if d.direction == "long" and d.take_profit_2 <= d.take_profit_1:
            violations.append(
                f"多单 TP2 须比 TP1 更远（TP2={d.take_profit_2} ≤ TP1={d.take_profit_1}）"
            )
        if d.direction == "short" and d.take_profit_2 >= d.take_profit_1:
            violations.append(
                f"空单 TP2 须比 TP1 更远（TP2={d.take_profit_2} ≥ TP1={d.take_profit_1}）"
            )
    if violations:
        return False, violations, {}

    # ── 近锚一致性校验（docs/23）：突破类 AI 声明 stop_anchor=near 时，
    # 止损必须贴近突破K线极值 ±0.5%——防 AI 挂近锚名义用远止损绕过
    # "仅 RR 不达标才降级" 的降级链前提 ──
    if (
        d.stop_anchor == "near"
        and (signal.get("signal_type") or "") in BREAKOUT_SIGNAL_TYPES
        and len(klines) >= 2
    ):
        sig_candle = klines[-2]  # klines[-1] 未收盘，突破K = 最后一根已收盘
        try:
            sig_high, sig_low = float(sig_candle[2]), float(sig_candle[3])
        except (IndexError, TypeError, ValueError):
            sig_high = sig_low = 0.0
        if sig_high > 0 and sig_low > 0:
            if d.direction == "long":
                anchor = sig_low * (1 - NEAR_ANCHOR_BUFFER)
            else:
                anchor = sig_high * (1 + NEAR_ANCHOR_BUFFER)
            if abs(d.stop_loss - anchor) / anchor > NEAR_ANCHOR_TOLERANCE:
                violations.append(
                    f"stop_anchor=near 但止损偏离突破K极值锚过远"
                    f"（锚={anchor:.8g}，SL={d.stop_loss}）：近锚仅允许突破K线"
                    f"{'最低' if d.direction == 'long' else '最高'}价外 {NEAR_ANCHOR_BUFFER:.1%} 缓冲"
                )
                return False, violations, {}
        else:
            logger.warning("near 校验跳过：突破K线数据异常 %s", signal.get("symbol"))

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0

    # ── RR 铁律（唯一保留的价格校验）：按 AI 三价复算，声称值不信 ──
    rr = 0.0
    if stop_dist > 0:
        rr = abs(d.take_profit_1 - d.entry_price) / stop_dist
        if rr < settings.AI_RR_MIN:
            violations.append(
                f"盈亏比复算 {rr:.2f} < {settings.AI_RR_MIN}（交易系统铁律）"
            )
    if violations:
        return False, violations, {}

    # ── 修正：程序复算值落库，AI 声称值不信 ──
    # 固定亏损仓位法：仓位（名义价值占账户%）= 风险预算 ÷ 止损距离。
    # 风险预算两种口径（2026-09-27 止损金额模式，与开仓引擎 trade_engine 同源）：
    # pct（默认）= RISK_BUDGET_PCT%（3%）× 账户资金 → 触止损恰好亏账户 3%；
    # fixed      = risk_budget 实额 USDT（由调用方从 system_config 传入）。
    # 止损越远仓位越小，不设 clamp。例：止损距离 2% + 3% 口径 → 仓位 150%
    # （10×杠杆下占用保证金 15%）
    position_pct = 0.0
    if stop_pct > 0:
        if risk_budget is not None and risk_budget > 0:
            # fixed 模式（2026-09-27）：仓位%依赖钱包余额，分析期（多账号扇出前）
            # 无账号上下文，不做折算——置 None 让卡片「仓位建议」显示 -，
            # 真实仓位由开仓引擎按 固定金额÷止损距离 实算（trade_engine）
            position_pct = None
        else:
            position_pct = round(settings.RISK_BUDGET_PCT / stop_pct, 2)
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
        "trade_type": d.trade_type,
        # 止损锚类型落库（docs/23）：near 仅突破类有效（AI 在回踩类误报时按
        # structure 落库防分组污染）；None（未声明/非法值）同样按 structure 理解
        "stop_anchor": (
            "near"
            if d.stop_anchor == "near"
            and (signal.get("signal_type") or "") in BREAKOUT_SIGNAL_TYPES
            else "structure"
        ),
        "analysis": d.analysis or "",
        "entry_price": round(d.entry_price, 8),
        "stop_loss": round(d.stop_loss, 8),
        "take_profit_1": round(d.take_profit_1, 8),
        "take_profit_2": round(d.take_profit_2, 8),
        "risk_reward_ratio": round(rr, 2),
        "position_pct": position_pct,
        "recommendation": float(min(max(d.recommendation, 50), 100)),
    }
    return True, [], fixed


def build_forced_skip(reason: str, violations: Optional[list[str]] = None) -> dict:
    """校验/重试耗尽后的强制 skip 结果（保证任何路径都有确定结论落库）"""
    detail = ""
    if violations:
        detail = "；".join(f"{i+1}. {v}" for i, v in enumerate(violations[:3]))
    skip_reason = f"{reason}" + (f"（{detail}）" if detail else "")
    return {
        "trade_decision": "skip",
        "skip_reason": skip_reason[:500],
        "direction": None,
        "analysis": "1. 该信号未通过程序风控校验，已自动跳过，未消耗人工判断成本。",
        "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
        "take_profit_2": 0, "risk_reward_ratio": 0,
        "position_pct": 0, "recommendation": 10,
    }


def parse_and_validate(
    raw: dict, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
) -> tuple[bool, list[str], dict]:
    """normalize + pydantic 校验 + 业务校验 的一步入口（预算参数透传 validate_decision）"""
    try:
        d = normalize_raw(raw)
    except (ValidationError, ValueError) as e:
        return False, [f"输出格式不合法: {str(e)[:200]}"], {}
    return validate_decision(d, signal, klines, risk_budget=risk_budget)
