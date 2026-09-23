"""现货屯币决策引擎（docs/18 §3.3 + §4.0，纯函数——零侵入/回测同构铁律）

- evaluate_day 是唯一决策入口：实盘日检任务（spot_tasks）与回测循环（P1）
  调用同一函数，禁止任何一方单独实现指标计算（docs/18 §10.2 PR 一票否决）
- 输入只含 ≤ as_of 日的收盘价数组（旧→新），引擎内部不读时钟、不做日期过滤
  ——未来函数防线在调用方的截断逻辑 + §10.6 单测
- 输出可解释：每个得分项带 当前值/阈值/得分，页面评分明细直接渲染
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services import spot_indicators as ind

# 币安现货最小名义值（USDT）
MIN_NOTIONAL_USDT = 10.0


@dataclass(frozen=True)
class SpotDefaults:
    """docs/18 §5 默认配置（DB spot_settings 为 NULL 时的兜底口径）"""

    per_order_usdt: float = 100.0  # 单份金额
    max_total_usdt: float = 3000.0  # 单币累计投入上限
    dca_enabled: bool = True
    dca_amount: float = 50.0
    usdt_reserve_pct: float = 0.2  # 买入后 USDT 保留垫（占现货预算）
    fee_rate: float = 0.001  # 手续费假设（回测口径，BNB 抵扣实际更低）


# ---------------- 档位与评分（全部显式常量，可解释性的唯一事实源） ----------------

# 日RSI 超卖档：<30 / <25 / <20 → 1/2/3 份
RSI_BUY_TIERS = ((20.0, 3), (25.0, 2), (30.0, 1))
# 30日回撤档：>15% / >25% / >35% → 1/2/3 份（回撤用负值比较）
DRAWDOWN_TIERS = ((-0.35, 3), (-0.25, 2), (-0.15, 1))
# MA200 上方时的更严 RSI 门槛（牛市半山腰少接货）
ABOVE_MA200_RSI_GATE = 28.0
# 日RSI 超买档：>70 / >80 → 卖 1/2 份
RSI_SELL_TIERS = ((80.0, 2), (70.0, 1))
# 30日反弹档：>30% / >50% → 卖 1/2 份
REBOUND_SELL_TIERS = ((0.50, 2), (0.30, 1))
# 周K牛熊线乖离卖出：现价 > MA30周 × 1.3
BULL_LINE_DEVIATION_SELL = 0.30

# 评分映射 → 买入份数（≥70→3 / ≥55→2 / ≥40→1 / <40→0）
SCORE_LOTS_BANDS = ((70.0, 3), (55.0, 2), (40.0, 1))


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


def _buy_tier_from_rules(daily_closes: List[float]) -> Dict[str, Any]:
    """日K 买入档位：RSI 档与回撤档取高（不叠加），布林下轨 +1，单日上限 3。"""
    hits: List[str] = []
    rsi14 = ind.rsi(daily_closes, 14)
    rsi_tier = 0
    if rsi14 is not None:
        for th, tier in RSI_BUY_TIERS:
            if rsi14 < th:
                rsi_tier = tier
                hits.append(f"日RSI {rsi14:.1f} < {th}（{tier}份档）")
                break

    dd30 = ind.drawdown_from_high(daily_closes, 30)
    dd_tier = 0
    if dd30 is not None:
        for th, tier in DRAWDOWN_TIERS:
            if dd30 <= th:
                dd_tier = tier
                hits.append(f"30日回撤 {dd30 * 100:.1f}%（{tier}份档）")
                break

    tier = max(rsi_tier, dd_tier)
    boll_res = ind.boll(daily_closes, 20, 2.0)
    below_lower = False
    if boll_res is not None and daily_closes[-1] < boll_res[2]:
        below_lower = True
        tier += 1
        hits.append("收盘 < 布林下轨（+1份）")
    return {
        "tier": min(tier, 3),
        "hits": hits,
        "rsi14": _r(rsi14, 1),
        "drawdown30": _r(dd30),
        "boll": {"mid": _r(boll_res[0], 6), "upper": _r(boll_res[1], 6), "lower": _r(boll_res[2], 6)} if boll_res else None,
        "below_lower": below_lower,
    }


def _sell_tier_from_rules(daily_closes: List[float], vs_ma30w: Optional[float]) -> Dict[str, Any]:
    """日K 卖出档位：RSI 档与反弹档取高，布林上轨/牛熊线乖离 +1，单日上限 2。"""
    hits: List[str] = []
    rsi14 = ind.rsi(daily_closes, 14)
    rsi_tier = 0
    if rsi14 is not None:
        for th, tier in RSI_SELL_TIERS:
            if rsi14 > th:
                rsi_tier = tier
                hits.append(f"日RSI {rsi14:.1f} > {th}（卖{tier}份档）")
                break

    reb30 = ind.rebound_from_low(daily_closes, 30)
    reb_tier = 0
    if reb30 is not None:
        for th, tier in REBOUND_SELL_TIERS:
            if reb30 > th:
                reb_tier = tier
                hits.append(f"30日反弹 {reb30 * 100:.1f}%（卖{tier}份档）")
                break

    tier = max(rsi_tier, reb_tier)
    boll_res = ind.boll(daily_closes, 20, 2.0)
    above_upper = False
    if boll_res is not None and daily_closes[-1] > boll_res[1]:
        above_upper = True
        tier += 1
        hits.append("收盘 > 布林上轨（+1份）")
    if vs_ma30w is not None and vs_ma30w > BULL_LINE_DEVIATION_SELL:
        tier += 1
        hits.append(f"MA30周乖离 +{vs_ma30w * 100:.1f}%（+1份）")
    return {
        "tier": min(tier, 2),
        "hits": hits,
        "rebound30": _r(reb30),
        "above_upper": above_upper,
    }


def evaluate_day(
    symbol: str,
    as_of_date: str,
    daily_closes: List[float],
    weekly_closes: List[float],
    monthly_closes: List[float],
    settings: Optional[SpotDefaults] = None,
) -> Dict[str, Any]:
    """单币单日决策（纯函数）。

    参数：截至 as_of 当日收盘的收盘价数组（旧→新）；monthly_closes 传全历史
    （ATH 口径）。返回可解释决策 dict（结构见模块 docstring / 前端 SpotScore）。
    """
    cfg = settings or SpotDefaults()
    if not daily_closes:
        raise ValueError(f"{symbol} 日K 为空")
    price = float(daily_closes[-1])

    # ---- 月K 环境层（30 分）----
    vs_ma12m = ind.price_vs_ma(monthly_closes, 12)
    ath = max(monthly_closes) if monthly_closes else None
    ath_dd = (price / ath - 1.0) if ath else None
    monthly_bear = vs_ma12m is not None and vs_ma12m < 0
    deep_value = ath_dd is not None and ath_dd <= -0.50
    env_score = (20 if not monthly_bear else 5) if vs_ma12m is not None else 0
    if deep_value:
        env_score += 10
    env_detail = {
        "label": "月K环境",
        "items": [
            {"name": "现价 vs MA12月", "value": _r(vs_ma12m), "threshold": "> 0 牛市", "score": 20 if vs_ma12m is not None and not monthly_bear else (5 if vs_ma12m is not None else 0)},
            {"name": "距历史最高回撤", "value": _r(ath_dd), "threshold": "≤ -50% 深度价值", "score": 10 if deep_value else 0},
        ],
        "score": env_score,
    }

    # ---- 周K 仓位层（30 分）----
    vs_ma30w = ind.price_vs_ma(weekly_closes, 30)
    weekly_rsi = ind.rsi(weekly_closes, 14) if len(weekly_closes) >= 15 else None
    weekly_macd = ind.macd_golden_cross(weekly_closes)
    weekly_above = vs_ma30w is not None and vs_ma30w > 0
    pos_score = (15 if weekly_above else 5) if vs_ma30w is not None else 0
    if weekly_rsi is not None and weekly_rsi < 40:
        pos_score += 10
    if weekly_macd:
        pos_score += 5
    pos_detail = {
        "label": "周K仓位",
        "items": [
            {"name": "现价 vs MA30周（牛熊线）", "value": _r(vs_ma30w), "threshold": "> 0 多头", "score": 15 if weekly_above else (5 if vs_ma30w is not None else 0)},
            {"name": "周RSI(14)", "value": _r(weekly_rsi, 1), "threshold": "< 40", "score": 10 if weekly_rsi is not None and weekly_rsi < 40 else 0},
            {"name": "周MACD", "value": "金叉后" if weekly_macd else ("未金叉" if weekly_macd is not None else "数据不足"), "threshold": "金叉", "score": 5 if weekly_macd else 0},
        ],
        "score": pos_score,
    }

    # ---- 日K 择时层（40 分）----
    buy = _buy_tier_from_rules(daily_closes)
    sell = _sell_tier_from_rules(daily_closes, vs_ma30w)
    rsi14 = buy["rsi14"]
    rsi_tier_score = 0
    if rsi14 is not None:
        if rsi14 < 20:
            rsi_tier_score = 20
        elif rsi14 < 25:
            rsi_tier_score = 15
        elif rsi14 < 30:
            rsi_tier_score = 10
    dd30 = buy["drawdown30"]
    dd_tier_score = 0
    if dd30 is not None:
        if dd30 <= -0.35:
            dd_tier_score = 15
        elif dd30 <= -0.25:
            dd_tier_score = 10
        elif dd30 <= -0.15:
            dd_tier_score = 5
    timing_score = rsi_tier_score + dd_tier_score + (5 if buy["below_lower"] else 0)
    timing_detail = {
        "label": "日K择时",
        "items": [
            {"name": "日RSI 超卖档", "value": rsi14, "threshold": "<20/25/30", "score": rsi_tier_score},
            {"name": "30日回撤档", "value": _r(dd30), "threshold": "≤-15%/-25%/-35%", "score": dd_tier_score},
            {"name": "布林下轨", "value": "跌破" if buy["below_lower"] else "未破", "threshold": "跌破", "score": 5 if buy["below_lower"] else 0},
        ],
        "score": timing_score,
    }

    score_total = env_score + pos_score + timing_score

    # ---- 闸门与份数合成 ----
    gates: List[str] = []
    buy_tier = buy["tier"]
    # MA200 过滤：牛上半场只在更极端超卖时接货
    vs_ma200 = ind.price_vs_ma(daily_closes, 200)
    above_ma200 = vs_ma200 is not None and vs_ma200 > 0
    if above_ma200 and (rsi14 is None or rsi14 >= ABOVE_MA200_RSI_GATE):
        if buy_tier > 0:
            gates.append(f"MA200上方：要求日RSI < {ABOVE_MA200_RSI_GATE}（现 {rsi14}），买入降为 0")
        buy_tier = 0
    # 力度加档：月线深度价值 / 周线牛熊线下方折价各 +1（上限 3）
    boost = 0
    if deep_value and buy_tier > 0:
        boost += 1
        gates.append("月线深度价值区（距ATH ≤-50%）：买入力度 +1 档")
    if (not weekly_above) and vs_ma30w is not None and buy_tier > 0:
        boost += 1
        gates.append("周线牛熊线下方：折价买入力度 +1 档")
    buy_tier = min(buy_tier + boost, 3)
    # 熊市环境：买入上限 2 份
    if monthly_bear and buy_tier > 2:
        buy_tier = 2
        gates.append("月K熊市环境：买入上限 2 份")

    # 卖出闸门（docs/18 §8 #4：月K熊市禁止卖出；周K牛熊线下方只买不卖）
    sell_gate_block = None
    if monthly_bear:
        sell_gate_block = "月K熊市环境：禁止卖出"
    elif vs_ma30w is not None and not weekly_above:
        sell_gate_block = "周K牛熊线下方：只买不卖"
    sell_tier = 0 if sell_gate_block else sell["tier"]
    if sell_gate_block and sell["tier"] > 0:
        gates.append(f"{sell_gate_block}（原卖出档 {sell['tier']} 已拦）")

    # 评分映射：实际买入份数 = min(规则档, 评分映射)
    score_lots = 0
    for th, lots in SCORE_LOTS_BANDS:
        if score_total >= th:
            score_lots = lots
            break
    final_buy_lots = min(buy_tier, score_lots)

    if final_buy_lots > 0:
        action, lots = "BUY", final_buy_lots
    elif sell_tier > 0:
        action, lots = "SELL", sell_tier
    else:
        action, lots = "HOLD", 0
        if buy_tier > 0 and score_lots == 0:
            gates.append(f"评分 {score_total} < 40：规则档 {buy_tier} 份被评分映射拦下")

    reasons = buy["hits"] if action == "BUY" else (sell["hits"] if action == "SELL" else [])
    return {
        "symbol": symbol,
        "as_of": as_of_date,
        "price": price,
        "action": action,
        "lots": lots,
        "quote_amount": round(lots * cfg.per_order_usdt, 2) if action == "BUY" else None,
        "score": {
            "total": score_total,
            "env": env_detail,
            "position": pos_detail,
            "timing": timing_detail,
        },
        "env": {
            "monthly_bear": monthly_bear,
            "deep_value": deep_value,
            "weekly_above": weekly_above,
            "vs_ma12m": _r(vs_ma12m),
            "vs_ma30w": _r(vs_ma30w),
            "ath_drawdown": _r(ath_dd),
            "weekly_rsi": _r(weekly_rsi, 1),
            "weekly_macd_golden": weekly_macd,
            "vs_ma200": _r(vs_ma200),
        },
        "rules": {
            "buy_tier": buy_tier,
            "score_lots": score_lots,
            "final_buy_lots": final_buy_lots,
            "sell_tier": sell_tier,
            "buy_hits": buy["hits"],
            "sell_hits": sell["hits"],
            "gates": gates,
            "plan_text": _plan_text(monthly_bear, weekly_above, boost),
        },
    }


def _plan_text(monthly_bear: bool, weekly_above: Optional[bool], boost: int) -> str:
    """三周期环境状态拼装成一句话（前端「当前计划」展示，docs/18 §9.2.2）。"""
    parts: List[str] = []
    if monthly_bear:
        parts.append("熊市环境·只买不卖")
    else:
        parts.append("月线非熊")
    if weekly_above is None:
        parts.append("周线数据不足")
    elif weekly_above:
        parts.append("牛熊线上方·可卖")
    else:
        parts.append("牛熊线下方·只买不卖")
    if boost > 0:
        parts.append(f"买入力度+{boost}档")
    else:
        parts.append("正常力度")
    return " · ".join(parts)
