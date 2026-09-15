"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

校验链已收缩（2026-09-15 拍板：② AI 分析校验链仅保留两条——RR 铁律 + 双评委辩论复核）：
- Schema 强约束（Pydantic）+ suggest 基本项（方向/开单类型/三价为正）
- **RR 铁律**：按 AI 给的三价复算盈亏比 ≥ AI_RR_MIN(1.5)，声称值不信
- 仓位公式化（固定亏损法）：仓位 = 风险预算 ÷ 止损距离，触止损恰好亏
  RISK_BUDGET_PCT（3%），AI 不自报仓位

已移除的校验（方法论降级为 AI 提示词纪律，程序不再打回/改挂）：
- 方向-价格一致性、入场偏离现价 ≤2%（2026-09-15 移除）
- 方向铁律入场贴近结构位 ±0.3%（2026-09-15 早些时候移除）
- 止损锚定（结构锚 vs 影线极值取更远者 + 0.3% 缓冲）、止损宽度 [0.3×ATR, 3×ATR]、
  强平安全上限前置、止盈最近优先强制（docs/09 的锚定设计保留为提示词方法论）

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
    if data.get("trade_decision") not in ("suggest", "skip"):
        raise ValueError(f"trade_decision 非法: {data.get('trade_decision')!r}")
    return TradeDecision(**data)


# 逐仓强平闸门口径（docs/07 §8-A4）：止损距离超过 1/杠杆 − 维持保证金率 时，
# 价格未到止损价保证金就先亏光 → 被强平（20× 即 4.5%）。
# trade_engine 开仓闸门（_attempt_open）使用此口径；risk_guard 侧的前置打回
# 已随"仅保留 RR 铁律"移除（2026-09-15），强平保护由开仓闸门承担
LIQ_GATE_MMR = 0.005


def liquidation_gate_pct(leverage: Optional[int] = None) -> float:
    """逐仓强平闸门：止损距离占价格的最大安全比例（1/杠杆 − 维持保证金率）"""
    lev = leverage or settings.TRADING_LEVERAGE
    return 1 / lev - LIQ_GATE_MMR


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
) -> tuple[bool, list[str], dict]:
    """校验并修正决策（2026-09-15 拍板收缩：仅保留 RR 铁律 + 仓位复算）。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
    signal / klines 保留入参以稳定调用方接口（当前校验不再使用）。
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
    # 固定亏损仓位法：仓位（名义价值占账户%）= 风险预算 ÷ 止损距离，
    # 触发止损时账户恰好亏损 RISK_BUDGET_PCT（3%）。止损越远仓位越小，不设 clamp。
    # 例：止损距离 2% → 仓位 150%（10×杠杆下占用保证金 15%）
    position_pct = 0.0
    if stop_pct > 0:
        position_pct = round(settings.RISK_BUDGET_PCT / stop_pct, 2)
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
        "trade_type": d.trade_type,
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


def parse_and_validate(raw: dict, signal: dict, klines: list) -> tuple[bool, list[str], dict]:
    """normalize + pydantic 校验 + 业务校验 的一步入口"""
    try:
        d = normalize_raw(raw)
    except (ValidationError, ValueError) as e:
        return False, [f"输出格式不合法: {str(e)[:200]}"], {}
    return validate_decision(d, signal, klines)
