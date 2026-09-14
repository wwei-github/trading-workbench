"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

- Schema 强约束（Pydantic）
- 方向-价格一致性、盈亏比复算（≥1.5 铁律）、止损范围（[0.3×ATR, 3×ATR] + 强平安全上限，
  见 liquidation_gate_pct——超上限的开仓闸门必拒，风控侧前置打回）、
  止损锚定（2026-09-13：入场与 N 根极值之间有同类关键位则程序定锚其区域外沿±缓冲，
  无则回退影线极值锚——多单严格低于最低点、空单严格高于最高点）；
  止盈最近优先强制（2026-09-14）：止盈一必须挂第一关键位区域近轨外侧（多单=压力位下轨 zone_low
  下方、空单=支撑位上轨 zone_high 上方，留余地），止盈二挂下一档区域同规则；摆动点仅在
  前方无任何区域时回退使用（锚定价位本身）；AI 挂到更远结构位/空档/区域内部时程序直接改挂，
  到第一结构位盈亏比不足由 RR 铁律打回；
  方向铁律（docs/03 §8）：只在支撑位做多、只在压力位做空——入场价必须落在对应角色关键位
  区域内（±0.25×ATR 容差）；例外：放量突破 breakout / 手动搜索 manual_search / 无关键位；
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



_TP_ANCHOR_TOL = 0.015   # 止盈锚定容差：距锚定点 1.5% 以内视为锚定（留余地）
_TP_OVERSHOOT = 0.002    # 允许略越过锚定点的幅度（程序直接拉回到位外侧 0.2%，不回炉）

# 逐仓强平闸门（docs/07 §8-A4）：止损距离超过 1/杠杆 − 维持保证金率 时，
# 价格未到止损价保证金就先亏光 → 被强平（20× 即 4.5%）。risk_guard 止损上限
# 与 trade_engine 开仓闸门（_attempt_open）共用此口径
LIQ_GATE_MMR = 0.005
# 风控侧安全系数：开仓时现价对 AI 入场最多可漂移 1%（开仓闸门），闸门按开仓现价
# 复算止损距离——此处按 AI 入场价计，收紧 10% 留出漂移余量（20× 即 4.05%）
LIQ_GATE_SAFETY = 0.9


def liquidation_gate_pct(leverage: Optional[int] = None) -> float:
    """逐仓强平闸门：止损距离占价格的最大安全比例（1/杠杆 − 维持保证金率）"""
    lev = leverage or settings.TRADING_LEVERAGE
    return 1 / lev - LIQ_GATE_MMR


def _structural_candidates(signal: dict, klines: list, direction: str, entry: float) -> list[dict]:
    """入场方向前方的止盈锚定候选，每个候选 {price, anchor, far}：

    只用关键位区域作候选（2026-09-14 改版）：止盈一=第一压力/支撑位、止盈二=下一档
    （用户规则）。此前摆动点前高与区域混排，前高插在第一压力区前面把止盈一、二
    都压在第一压力位下方（FLOCKUSDT 案例：两档止盈分别锚在前高 0.0695/0.0719，
    都在第一压力区 0.0746 之下）。入场方向前方没有任何区域时（如创新高突破）
    才回退摆动点（anchor=价位本身，far=外侧 0.2%）。
    - anchor 锚定点：关键位取区域近轨（多单=压力位下轨 zone_low，空单=支撑位上轨 zone_high），
      止盈挂在近轨外侧留余地——价格常在区域边缘反弹，深入区域才触发的止盈大概率落空。
    - far 匹配上界：关键位=区域远轨（近轨与远轨之间视为"略越过近轨"，程序拉回）。
    多单返回按 anchor 升序，空单降序。
    """
    cands: list[dict] = []
    for lv in (signal.get("key_levels") or []):
        try:
            price = float(lv.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        anchor_field = "zone_low" if direction == "long" else "zone_high"
        far_field = "zone_high" if direction == "long" else "zone_low"
        try:
            anchor = float(lv[anchor_field])
            far = float(lv[far_field])
        except (KeyError, TypeError, ValueError):
            # 旧数据无区域字段：退化为价位锚定（略越位 0.2% 内仍可拉回）
            anchor = price
            far = price * (1 + _TP_OVERSHOOT) if direction == "long" else price * (1 - _TP_OVERSHOOT)
        if (far - anchor) * (1 if direction == "long" else -1) < 0:
            far = anchor  # 区域数据异常兜底
        # 近轨须仍在入场方向前方，否则该关键位对止盈无意义（入场已在区域内/越过区域）
        if (anchor - entry) * (1 if direction == "long" else -1) <= 0:
            continue
        cands.append({"price": price, "anchor": anchor, "far": far})
    if not cands:
        # 前方无任何关键位区域（如创新高突破）：回退摆动点作结构位
        swings = recent_swings(klines, order=settings.SWING_ORDER, n=50)
        for s in (swings["highs"] if direction == "long" else swings["lows"]):
            p = float(s["price"])
            if (direction == "long" and p <= entry) or (direction == "short" and p >= entry):
                continue
            far = p * (1 + _TP_OVERSHOOT) if direction == "long" else p * (1 - _TP_OVERSHOOT)
            cands.append({"price": p, "anchor": p, "far": far})
    cands.sort(key=lambda c: c["anchor"], reverse=(direction == "short"))
    return cands


def _anchored(tp: float, cands: list[dict], direction: str) -> Optional[dict]:
    """TP 是否锚定某结构位近轨并留余地（多单=近轨下方~远轨间，空单对称），返回匹配候选"""
    for c in cands:
        if direction == "long":
            if c["anchor"] * (1 - _TP_ANCHOR_TOL) <= tp <= c["far"]:
                return c
        else:
            if c["far"] <= tp <= c["anchor"] * (1 + _TP_ANCHOR_TOL):
                return c
    return None


def _clamp_before_level(tp: float, anchor: float, direction: str) -> float:
    """越过区域近轨的止盈拉回近轨外侧 0.2%（多单=下轨下方、空单=上轨上方）——程序直接修正不回炉"""
    return anchor * (1 - _TP_OVERSHOOT) if direction == "long" else anchor * (1 + _TP_OVERSHOOT)


def validate_decision(
    d: TradeDecision, signal: dict, klines: list,
) -> tuple[bool, list[str], dict]:
    """校验并修正决策。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
    """
    violations: list[str] = []

    def _zone_edge(lv: dict, field: str) -> float:
        # 区域字段缺失（旧数据）时退化为价位本身（止损/方向铁律块共用）
        try:
            return float(lv[field])
        except (KeyError, TypeError, ValueError):
            return float(lv.get("price") or 0)

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

    atr = calc_atr(klines)

    # ── 方向铁律（docs/03 §8）：只在支撑位做多、只在压力位做空 ──
    # 入场价必须落在信号方向对应角色的关键位区域内（±0.25×ATR 容差，防 AI 贴着区域边缘报价）。
    # 例外：breakout（放量突破顺势追，入场贴近现价）/ manual_search（用户手动指定，人工兜底）/
    # 无关键位（无法校验，交由其余规则约束）
    levels = signal.get("key_levels") or []
    if (
        d.direction in ("long", "short") and levels
        and signal.get("signal_type") not in ("breakout", "manual_search")
    ):
        tol = 0.25 * atr if atr else 0.0
        wanted_role = "support" if d.direction == "long" else "resistance"
        side_levels = [
            lv for lv in levels
            if lv.get("role") == wanted_role and float(lv.get("price") or 0) > 0
        ]

        if not any(
            _zone_edge(lv, "zone_low") - tol <= d.entry_price <= _zone_edge(lv, "zone_high") + tol
            for lv in side_levels
        ):
            if side_levels:
                near = ", ".join(f"{float(lv['price']):g}" for lv in side_levels[:5])
                violations.append(
                    f"方向铁律违规：{'做多入场必须落在某个支撑位区域内' if d.direction == 'long' else '做空入场必须落在某个压力位区域内'}"
                    f"（±0.25×ATR 容差），唯一例外是放量突破信号。"
                    f"可用{'支撑位' if d.direction == 'long' else '压力位'}：{near}"
                )
            else:
                violations.append(
                    f"方向铁律违规：当前关键位中没有{'支撑位，做多不成立' if d.direction == 'long' else '压力位，做空不成立'}"
                    f"（唯一例外是放量突破信号）"
                )

    if close > 0:
        dev = abs(d.entry_price - close) / close
        if dev > 0.02:
            violations.append(f"入场价偏离当前价 {dev*100:.2f}% > 2%（入场应接近现价）")

    # ── 止损锚定（2026-09-13 更新：关键位优先，影线极值兜底；2026-09-14 补强：两锚取更远者）──
    # 多单：入场价下方最近的支撑位区域下轨（不限是否在 10 根影线范围内）为候选锚，
    # 最终锚 = 「区域下轨与 recent_low 中更低者」（AI 给值仅参考）——入场贴着关键位区域是
    # 区间边缘类开单的常态，只锚区域近轨会把止损放进近期波动区间内部（AKEUSDT 案例：
    # 空单止损 1.48% 比黄昏星高点还近），必须同时越过影线极值；跨极值的关键位比极值更远，
    # 同样参与定锚（关键位优先）。最终锚距 <0.3×ATR 时不用关键位锚（回退极值锚路径）。
    # 无候选关键位时退回极值锚：严格低于 recent_low 且留缓冲（不足自动推远，未越过打回）。
    # 空单对称（区域上轨与 recent_high 取更高者）。
    closed = klines[:-1] if len(klines) >= 2 else klines
    n = min(settings.STOP_LOSS_RECENT_BARS, len(closed))
    if n > 0:
        if d.direction == "long":
            recent_low = min(float(k[3]) for k in closed[-n:])
            anchor = None
            for lv in signal.get("key_levels") or []:
                if (lv.get("role") or lv.get("kind")) != "support":
                    continue
                edge = _zone_edge(lv, "zone_low")
                if 0 < edge < d.entry_price and (anchor is None or edge > anchor):
                    anchor = edge
            base = min(anchor, recent_low) if anchor is not None else None
            if base is not None and not (
                atr and d.entry_price and d.entry_price - base < 0.3 * atr
            ):
                d.stop_loss = base * (1 - settings.STOP_LOSS_BUFFER_PCT)
            else:
                if d.stop_loss >= recent_low:
                    violations.append(
                        f"做多止损 {d.stop_loss} 必须严格低于最近{n}根K线最低点 {recent_low}"
                        f"（影线极值，且至少低 {settings.STOP_LOSS_BUFFER_PCT:.1%} 缓冲，不能正好用最低点）"
                    )
                elif d.stop_loss > recent_low * (1 - settings.STOP_LOSS_BUFFER_PCT):
                    d.stop_loss = recent_low * (1 - settings.STOP_LOSS_BUFFER_PCT)
        elif d.direction == "short":
            recent_high = max(float(k[2]) for k in closed[-n:])
            anchor = None
            for lv in signal.get("key_levels") or []:
                if (lv.get("role") or lv.get("kind")) != "resistance":
                    continue
                edge = _zone_edge(lv, "zone_high")
                if edge > d.entry_price and (anchor is None or edge < anchor):
                    anchor = edge
            # 同多单：两锚取更远者（区域上轨与 recent_high 中更高者）
            base = max(anchor, recent_high) if anchor is not None else None
            if base is not None and not (
                atr and d.entry_price and base - d.entry_price < 0.3 * atr
            ):
                d.stop_loss = base * (1 + settings.STOP_LOSS_BUFFER_PCT)
            else:
                if d.stop_loss <= recent_high:
                    violations.append(
                        f"做空止损 {d.stop_loss} 必须严格高于最近{n}根K线最高点 {recent_high}"
                        f"（影线极值，且至少高 {settings.STOP_LOSS_BUFFER_PCT:.1%} 缓冲，不能正好用最高点）"
                    )
                elif d.stop_loss < recent_high * (1 + settings.STOP_LOSS_BUFFER_PCT):
                    d.stop_loss = recent_high * (1 + settings.STOP_LOSS_BUFFER_PCT)

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0
    # 注意：3% 属仓位维度（单笔触止损的账户亏损预算，由下方仓位公式保证），
    # 不对止损价格距离设百分比红线；止损宽度由结构位决定，仅用 ATR 上下界约束合理性
    # （atr 已在方向铁律块前算好）
    if atr and d.entry_price:
        if stop_dist < 0.3 * atr:
            violations.append(
                f"止损距离 {stop_dist:.6g} < 0.3×ATR({0.3*atr:.6g})，过近易被扫损"
            )
        if stop_dist > 3 * atr:
            violations.append(
                f"止损距离 {stop_dist:.6g} > 3×ATR({3*atr:.6g})，过远盈亏比崩塌"
            )
    # 强平闸门前置（docs/07 §8-A4）：止损距离超过强平安全上限的交易，开仓闸门必拒
    # （价格未到止损保证金先亏光）——与其让分析通过后每轮在开仓处空转，这里直接打回：
    # AI 若无法在结构允许范围内收紧止损，应输出 skip（固定亏损法对此类距离无解）
    if d.entry_price:
        stop_cap = liquidation_gate_pct() * LIQ_GATE_SAFETY
        if stop_pct >= stop_cap:
            violations.append(
                f"止损距离 {stop_pct*100:.2f}% 超过强平安全上限 {stop_cap*100:.2f}%"
                f"（{settings.TRADING_LEVERAGE}× 逐仓：1/{settings.TRADING_LEVERAGE}−维持保证金率，"
                f"再×{LIQ_GATE_SAFETY:g} 留开仓漂移余量），该距离下价格未到止损必先被强平——"
                f"请收紧止损到上限内，结构上做不到就直接 skip"
            )

    # ── 止盈锚定校验（2026-09-14 改为最近优先强制）：止盈一必须挂在第一结构位近轨外侧，
    # 止盈二挂在下一档结构位近轨外侧（用户规则）。AI 挂在更远结构位或空档时程序直接改挂
    # （修正不回炉）——此前允许"最近位盈亏比不足就取下一档"，AI 会跳过第一压力位去锚更远
    # 的摆动点，止盈一、二扎堆（SCRUSDT 案例：两档仅差 0.37%）。到第一结构位盈亏比不足
    # 1.5 时由下方 RR 铁律打回——第一压力/支撑都够不着 1.5R 的交易本就不值得做。
    # 前方无任何结构位时回退固定盈亏比。
    if d.direction in ("long", "short") and d.take_profit_1 > 0:
        cands = _structural_candidates(signal, klines, d.direction, d.entry_price)
        if not cands:
            # 前方无任何结构位可锚定（如创新高突破）：回退固定盈亏比——
            # TP1 = 入场 ± RR_MIN×止损距离（乘 1.001 留浮点余量，防复算恰等于阈值被判负），
            # TP2 清零，仅设一档
            fb = settings.AI_RR_MIN * stop_dist * 1.001
            d.take_profit_1 = d.entry_price + fb if d.direction == "long" else d.entry_price - fb
            d.take_profit_2 = 0.0
        else:
            first = cands[0]
            # 两种改挂：挂在空档/更远结构位（未锚定第一结构位），或挂进区域内部/
            # 越过近轨（价格常在区域边缘反弹，深入区域才触发的止盈大概率落空）——
            # 都直接改挂第一结构位近轨外侧 0.2%
            if _anchored(d.take_profit_1, [first], d.direction) is None or (
                (d.direction == "long" and d.take_profit_1 > first["anchor"])
                or (d.direction == "short" and d.take_profit_1 < first["anchor"])
            ):
                d.take_profit_1 = _clamp_before_level(d.take_profit_1, first["anchor"], d.direction)
            second = cands[1] if len(cands) > 1 else None
            if second is None or d.take_profit_2 <= 0:
                d.take_profit_2 = 0.0  # 更前方已无结构位（或 AI 仅设一档）
            elif _anchored(d.take_profit_2, [second], d.direction) is None or (
                (d.direction == "long" and d.take_profit_2 > second["anchor"])
                or (d.direction == "short" and d.take_profit_2 < second["anchor"])
            ):
                d.take_profit_2 = _clamp_before_level(d.take_profit_2, second["anchor"], d.direction)
                if (d.direction == "long" and d.take_profit_2 <= d.take_profit_1) or (
                    d.direction == "short" and d.take_profit_2 >= d.take_profit_1
                ):
                    d.take_profit_2 = 0.0  # 改挂后与止盈一倒挂/贴死：仅设一档

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
