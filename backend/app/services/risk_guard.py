"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

- Schema 强约束（Pydantic）
- 方向-价格一致性、盈亏比复算（≥1.5 铁律）、止损范围（[0.3×ATR, 3×ATR]，价格距离不设百分比红线）、
  止损须越过最近 N 根已收盘K线极值（多单严格低于最低点，空单严格高于最高点）；
  止盈须锚定前方结构位（多单=上方高点/关键位，空单=下方低点/关键位，留余地；TP2 更远一档）；
  仓位公式保证触止损账户亏损 ≤ 风险预算（RISK_BUDGET_PCT=3%，仓位维度的"3%止损"）
- 仓位公式化（固定亏损法）：仓位 = 风险预算 ÷ 止损距离，触止损恰好亏 RISK_BUDGET_PCT（3%），AI 不自报仓位
- 返回具体违规明细，供"校验失败带错误反馈重试"
"""
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.strategy import recent_swings
from app.services.strategy.key_levels import calc_atr

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
    trade_type: Optional[str] = None  # 开单类型：trend_follow / rule_123 / n_structure / rule_2b / range_edge
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



_TP_ANCHOR_TOL = 0.015   # 止盈锚定容差：距结构位 1.5% 以内视为锚定（留余地）
_TP_OVERSHOOT = 0.002    # 允许略越过结构位的幅度（防止贪心把止盈挂到结构位外过远）


def _structural_candidates(signal: dict, klines: list, direction: str, entry: float) -> list[float]:
    """入场方向前方的结构位候选：摆动高/低点 + 关键位价格。

    多单止盈看上方（前高/压力类关键位），空单看下方（前低/支撑类关键位）。
    多单返回升序，空单返回降序。摆动 order 用全局 SWING_ORDER（与 fact pack 近似同源）。
    """
    swings = recent_swings(klines, order=settings.SWING_ORDER, n=50)
    prices = [float(s["price"]) for s in (swings["highs"] if direction == "long" else swings["lows"])]
    for lv in (signal.get("key_levels") or []):
        try:
            prices.append(float(lv.get("price") or 0))
        except (TypeError, ValueError):
            continue
    if direction == "long":
        return sorted({p for p in prices if p > entry})
    return sorted({p for p in prices if p < entry}, reverse=True)


def _anchored(tp: float, cands: list[float], direction: str) -> Optional[float]:
    """TP 是否锚定某结构位（留余地方向随多空：多单略低于高位、空单略高于低位），返回匹配位"""
    for c in cands:
        if direction == "long":
            if c * (1 - _TP_ANCHOR_TOL) <= tp <= c * (1 + _TP_OVERSHOOT):
                return c
        else:
            if c * (1 - _TP_OVERSHOOT) <= tp <= c * (1 + _TP_ANCHOR_TOL):
                return c
    return None


def _clamp_before_level(tp: float, level: float, direction: str) -> float:
    """越过结构位的止盈拉回位前 0.2%（多单位下方、空单位上方）——程序直接修正不回炉"""
    return level * (1 - _TP_OVERSHOOT) if direction == "long" else level * (1 + _TP_OVERSHOOT)


def validate_decision(
    d: TradeDecision, signal: dict, klines: list,
) -> tuple[bool, list[str], dict]:
    """校验并修正决策。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
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

    # ── suggest ──
    close = float(signal.get("current_price") or 0)
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

    if d.direction == "long" and not (d.stop_loss < d.entry_price < d.take_profit_1):
        violations.append(
            f"做多价格关系错误：要求 止损({d.stop_loss}) < 入场({d.entry_price}) < 止盈1({d.take_profit_1})"
        )
    if d.direction == "short" and not (d.stop_loss > d.entry_price > d.take_profit_1):
        violations.append(
            f"做空价格关系错误：要求 止损({d.stop_loss}) > 入场({d.entry_price}) > 止盈1({d.take_profit_1})"
        )
    # tp2 与 tp1 的大小关系检查放在止盈锚定块之后（回退/清零可能改变 tp1/tp2，见下）

    if close > 0:
        dev = abs(d.entry_price - close) / close
        if dev > 0.02:
            violations.append(f"入场价偏离当前价 {dev*100:.2f}% > 2%（入场应接近现价）")

    # ── 止损须越过最近N根已收盘K线极值：多单严格低于最低点、空单严格高于最高点 ──
    # 不能正好用极值本身（插针必扫损）；violations 会反馈给 AI 回炉重试
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = min(settings.STOP_LOSS_RECENT_BARS, len(closed))
    if n > 0:
        if d.direction == "long":
            recent_low = min(float(k[3]) for k in closed[-n:])
            if d.stop_loss >= recent_low:
                violations.append(
                    f"做多止损 {d.stop_loss} 必须严格低于最近{n}根K线最低点 {recent_low}（在其下方留缓冲，不能正好用最低点）"
                )
        elif d.direction == "short":
            recent_high = max(float(k[2]) for k in closed[-n:])
            if d.stop_loss <= recent_high:
                violations.append(
                    f"做空止损 {d.stop_loss} 必须严格高于最近{n}根K线最高点 {recent_high}（在其上方留缓冲，不能正好用最高点）"
                )

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0
    # 注意：3% 属仓位维度（单笔触止损的账户亏损预算，由下方仓位公式保证），
    # 不对止损价格距离设百分比红线；止损宽度由结构位决定，仅用 ATR 上下界约束合理性
    atr = calc_atr(klines)
    if atr and d.entry_price:
        if stop_dist < 0.3 * atr:
            violations.append(
                f"止损距离 {stop_dist:.6g} < 0.3×ATR({0.3*atr:.6g})，过近易被扫损"
            )
        if stop_dist > 3 * atr:
            violations.append(
                f"止损距离 {stop_dist:.6g} > 3×ATR({3*atr:.6g})，过远盈亏比崩塌"
            )

    # ── 止盈锚定校验：TP1 必须是前方第一个（或因盈亏比退一档的）结构位，TP2 更远一档 ──
    # 多单锚定上方的高点/关键位，空单锚定下方的低点/关键位；留余地 = 距结构位容差内、
    # 不得显著越过（越过者程序直接拉回位前 0.2%）。前方无任何结构位时回退固定盈亏比；
    # 其余违规列出可用结构位供 AI 重试
    if d.direction in ("long", "short") and d.take_profit_1 > 0:
        side = "上方" if d.direction == "long" else "下方"
        cands = _structural_candidates(signal, klines, d.direction, d.entry_price)
        c1 = _anchored(d.take_profit_1, cands, d.direction)
        if c1 is None:
            if not cands:
                # 前方无任何结构位可锚定（如创新高突破）：回退固定盈亏比——
                # TP1 = 入场 ± RR_MIN×止损距离（乘 1.001 留浮点余量，防复算恰等于阈值被判负），
                # TP2 清零，仅设一档
                fb = settings.AI_RR_MIN * stop_dist * 1.001
                d.take_profit_1 = d.entry_price + fb if d.direction == "long" else d.entry_price - fb
                d.take_profit_2 = 0.0
            else:
                near = ", ".join(f"{c:g}" for c in cands[:5])
                violations.append(
                    f"止盈一 {d.take_profit_1} 未锚定结构位：必须是前一个高点/低点或关键位并留余地"
                    f"（{'做多看上方高点/关键位' if d.direction == 'long' else '做空看下方低点/关键位'}）。"
                    f"可用结构位（{side}）：{near}"
                )
        elif (d.direction == "long" and d.take_profit_1 > c1) or (
            d.direction == "short" and d.take_profit_1 < c1
        ):
            # 用户规则：止盈必须留在结构位之前（多=位下方、空=位上方）——价格常在位前反弹，
            # 挂到位外大概率不成交。AI 偶尔把 TP 挂过结构位（容差内 ≤0.2%），程序拉回位前 0.2%
            d.take_profit_1 = _clamp_before_level(d.take_profit_1, c1, d.direction)
        if d.take_profit_2 > 0 and c1 is not None:
            further = [c for c in cands if (c > c1 if d.direction == "long" else c < c1)]
            if not further:
                d.take_profit_2 = 0.0  # 更前方已无结构位：仅设一档
            else:
                c2 = _anchored(d.take_profit_2, further, d.direction)
                if c2 is None:
                    near = ", ".join(f"{c:g}" for c in further[:5])
                    violations.append(
                        f"止盈二 {d.take_profit_2} 未锚定比止盈一更远的一档结构位。可用结构位（{side}）：{near}"
                    )
                elif (d.direction == "long" and d.take_profit_2 > c2) or (
                    d.direction == "short" and d.take_profit_2 < c2
                ):
                    d.take_profit_2 = _clamp_before_level(d.take_profit_2, c2, d.direction)

    # 止盈锚定块的回退/清零可能改变 tp1/tp2，关系检查以修正后值为准（清除原始值误报）
    if d.direction == "long" and d.take_profit_2 > 0 and d.take_profit_2 <= d.take_profit_1:
        violations.append("做多要求 tp2 > tp1")
    if d.direction == "short" and d.take_profit_2 > 0 and d.take_profit_2 >= d.take_profit_1:
        violations.append("做空要求 tp2 < tp1")

    if stop_dist > 0:
        rr = abs(d.take_profit_1 - d.entry_price) / stop_dist
        if rr < settings.AI_RR_MIN:
            violations.append(
                f"盈亏比复算 {rr:.2f} < {settings.AI_RR_MIN}（交易系统铁律）"
            )
    else:
        rr = 0.0

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
