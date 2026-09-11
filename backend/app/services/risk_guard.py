"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

- Schema 强约束（Pydantic）
- 方向-价格一致性、盈亏比复算（≥1.5 铁律）、止损范围（[0.3×ATR, 3×ATR] + ≤3% 绝对红线）
- 仓位公式化：AI 不自报仓位，由风险预算与止损距离计算
- 返回具体违规明细，供"校验失败带错误反馈重试"
"""
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)


class TradeDecision(BaseModel):
    """AI 决策契约（P0：AI 报绝对价格；P1 升级为结构位锚点 level_ref+offset）"""

    trade_decision: Literal["suggest", "skip"]
    skip_reason: str = ""
    direction: Optional[Literal["long", "short"]] = None
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


def calc_atr(klines: list, period: int = 14) -> Optional[float]:
    """ATR(14)：最近 period 根已收盘 K 线的真实波幅均值（klines[-1] 未收盘）"""
    closed = klines[:-1] if len(klines) >= 2 else klines
    if len(closed) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l = float(closed[i][2]), float(closed[i][3])
        prev_c = float(closed[i - 1][4])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs)


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
    if d.direction == "long" and d.take_profit_2 > 0 and d.take_profit_2 <= d.take_profit_1:
        violations.append("做多要求 tp2 > tp1")
    if d.direction == "short" and d.take_profit_2 > 0 and d.take_profit_2 >= d.take_profit_1:
        violations.append("做空要求 tp2 < tp1")

    if close > 0:
        dev = abs(d.entry_price - close) / close
        if dev > 0.02:
            violations.append(f"入场价偏离当前价 {dev*100:.2f}% > 2%（入场应接近现价）")

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0
    if stop_pct > settings.RISK_STOP_MAX_PCT:
        violations.append(
            f"止损距离 {stop_pct*100:.2f}% 超过 {settings.RISK_STOP_MAX_PCT*100:.0f}% 绝对红线"
        )
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
    position_pct = 0.0
    if stop_pct > 0:
        position_pct = round(
            min(max(settings.RISK_BUDGET_PCT / stop_pct, 0.5), 10.0), 2
        )
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
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
