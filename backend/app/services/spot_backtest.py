"""现货回测引擎（docs/18 §10，P1 第一优先——先验证再下单）

同构铁律（§10.2）：模拟日检与实盘 spot_tasks 调用同一个 evaluate_day，
本模块只做「日历重放 + 记账」，不实现任何指标/评分逻辑。

回测与实盘的唯一差异（§10.4，全量显式）：
- AI 周度研判：不生成（纯展示不影响决策）
- 大额 AI 复核：跳过 → 3 份信号满额执行（实盘可能被降档）
- 成交价：当日收盘价（实盘为执行时市价）
- 手续费：按 fee_rate 计入（默认 0.1%）

无未来函数防线：逐日截断喂入——每个交易日 T 只把 ≤T 的收盘数组
传给 evaluate_day（D[:-N] 截尾），T 之后的数据根本不在输入里。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.models.spot import SPOT_SYMBOLS
from app.services.spot_engine import MIN_NOTIONAL_USDT, SpotDefaults, evaluate_day


@dataclass
class _Pos:
    """单币模拟持仓（币本位记账）。"""

    qty: float = 0.0          # 持有数量（净）
    cost_usdt: float = 0.0    # 累计投入（买入含费）
    realized_usdt: float = 0.0  # 卖出回款（净）


def run_backtest(
    symbols: List[str],
    daily: Dict[str, List[dict]],
    weekly: Dict[str, List[dict]],
    monthly: Dict[str, List[dict]],
    budget: float,
    settings: Optional[SpotDefaults] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """单币或多币回测（日历重放）。

    参数：
    - symbols：参与回测的币种列表（SPOT_SYMBOLS 子集）
    - daily/weekly/monthly：{symbol: [{t, c}, ...]}（旧→新；t=ms open_time，
      c=收盘价——与 spot_kline_cache 精简列一致）
    - budget：总 USDT 预算（多币共享现金池）
    - start/end：YYYY-MM-DD 闭区间（按日K收盘的北京日期过滤；None=全量）

    返回：{summary, daily_series, orders}（落 spot_backtests 三列）。
    """
    cfg = settings or SpotDefaults()
    invalid = [s for s in symbols if s not in SPOT_SYMBOLS]
    if invalid:
        raise ValueError(f"非法币种: {invalid}")
    if not symbols:
        raise ValueError("symbols 为空")

    # ---- 预处理：每个日K bar 的收盘北京日期（复用实盘口径）----
    from app.tasks.spot_tasks import _bar_close_date

    dates = [_bar_close_date(int(k["t"]), "1d") for k in daily[symbols[0]]]
    idx = list(range(len(dates)))
    if start:
        idx = [i for i in idx if dates[i] >= start]
    if end:
        idx = [i for i in idx if dates[i] <= end]
    if not idx:
        raise ValueError("日期区间内无K线数据")

    # 各币周期数组预提取（旧→新），重放时按游标截断
    d_closes = {s: [float(k["c"]) for k in daily[s]] for s in symbols}
    w_closes = {s: [float(k["c"]) for k in weekly.get(s, [])] for s in symbols}
    m_closes = {s: [float(k["c"]) for k in monthly.get(s, [])] for s in symbols}

    # 周/月 bar 的收盘日期数组（重放游标定位用）
    w_dates = {s: [_bar_close_date(int(k["t"]), "1w") for k in weekly.get(s, [])] for s in symbols}
    m_dates = {s: [_bar_close_date(int(k["t"]), "1M") for k in monthly.get(s, [])] for s in symbols}

    cash = budget
    pos = {s: _Pos() for s in symbols}
    orders: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []
    invested = 0.0  # 累计买入含费支出（现金口径）
    sold_total = 0.0  # 累计卖出含费回款（现金口径）

    fee = cfg.fee_rate

    def _buy(s: str, i: int, usdt: float, trigger: str, score: int) -> None:
        nonlocal cash, invested
        price = d_closes[s][i]
        cost = usdt * (1 + fee)
        if cost > cash + 1e-9:
            return  # 现金不足放弃（保守口径，不部分成交）
        qty = usdt / price
        if usdt < MIN_NOTIONAL_USDT:
            return
        cash -= cost
        invested += cost
        p = pos[s]
        p.qty += qty
        p.cost_usdt += cost
        orders.append({
            "date": dates[i], "symbol": s, "side": "BUY", "trigger": trigger,
            "qty": round(qty, 8), "price": price, "quote": round(usdt, 2),
            "fee": round(usdt * fee, 4), "score": score,
        })

    def _sell(s: str, i: int, lots: int, score: int) -> None:
        nonlocal cash, sold_total
        price = d_closes[s][i]
        p = pos[s]
        if p.qty <= 0:
            return
        # 只卖波段仓：底仓 ≥70% 永不卖（docs/18 §3.3）→ 波段可卖上限 = 数量 30%
        max_qty = p.qty * 0.30
        # 卖出份数对应金额 = lots × per_order_usdt，换算数量
        qty = min(lots * cfg.per_order_usdt / price, max_qty)
        if qty <= 0:
            return
        proceeds = qty * price * (1 - fee)
        cash += proceeds
        sold_total += proceeds
        p.qty -= qty
        p.cost_usdt -= p.cost_usdt * (qty / (p.qty + qty))  # 按比例减成本
        p.realized_usdt += proceeds
        orders.append({
            "date": dates[i], "symbol": s, "side": "SELL", "trigger": "signal",
            "qty": round(qty, 8), "price": price,
            "quote": round(qty * price, 2), "fee": round(qty * price * fee, 4),
            "score": score,
        })

    # ---- 日历重放 ----
    w_cursor = {s: 0 for s in symbols}  # 周/月 bar 游标（≤当前日 的最大下标）
    m_cursor = {s: 0 for s in symbols}
    dca_done_weeks: set = set()

    for step, i in enumerate(idx):
        date = dates[i]
        day_decisions: Dict[str, Dict[str, Any]] = {}

        for s in symbols:
            # 逐日截断（未来函数防线）：只喂 ≤T 的数组
            d_slice = d_closes[s][: i + 1]
            # 周/月游标推进：收盘日期 ≤ 当前日的 bar 数
            while w_cursor[s] < len(w_dates[s]) and w_dates[s][w_cursor[s]] <= date:
                w_cursor[s] += 1
            while m_cursor[s] < len(m_dates[s]) and m_dates[s][m_cursor[s]] <= date:
                m_cursor[s] += 1
            w_slice = w_closes[s][: w_cursor[s]]
            m_slice = m_closes[s][: m_cursor[s]]

            if len(d_slice) < 2:
                continue
            dec = evaluate_day(s, date, d_slice, w_slice, m_slice, cfg)
            day_decisions[s] = dec

            # ---- 执行信号（AI 复核跳过 → 满额执行，§10.4）----
            if dec["action"] == "BUY":
                usdt = dec["lots"] * cfg.per_order_usdt
                _buy(s, i, usdt, "signal", dec["score"]["total"])
            elif dec["action"] == "SELL":
                _sell(s, i, dec["lots"], dec["score"]["total"])

            # ---- 周度定投（周一执行，无信号也买；§5 定投兜底）----
            if cfg.dca_enabled and _is_monday(date):
                week_key = date[:7] + "-" + date[8:10]  # 同日去重足够（逐日重放）
                if week_key not in dca_done_weeks:
                    dca_done_weeks.add(week_key)
                    _buy(s, i, cfg.dca_amount, "dca", dec["score"]["total"])

        # ---- 每日快照（曲线用）----
        total_mv = 0.0
        for s in symbols:
            total_mv += pos[s].qty * d_closes[s][i]
        series.append({
            "date": date,
            "cash": round(cash, 2),
            "market_value": round(total_mv, 2),
            "equity": round(cash + total_mv, 2),
            "coin_qty": {s: round(pos[s].qty, 8) for s in symbols},
            "invested": round(invested, 2),
        })

    # ---- 汇总：币本位 KPI（docs/18 §13：vs 定投基准超额）----
    summary = _summarize(
        symbols, pos, d_closes, w_closes, m_closes, dates, idx,
        budget, cfg, orders, fee, invested, sold_total,
    )
    summary["equity_curve_end"] = series[-1]["equity"] if series else budget
    # 最大回撤（U 本位权益曲线口径；币本位数量曲线单调非降——只买不卖底仓）
    peak, max_dd = 0.0, 0.0
    for pt in series:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        elif peak > 0:
            dd = (eq / peak - 1.0)
            if dd < max_dd:
                max_dd = dd
    summary["max_drawdown"] = round(max_dd, 4)
    return {"summary": summary, "daily_series": series, "orders": orders}


def _is_monday(date_str: str) -> bool:
    from datetime import date as _d

    y, m, d = map(int, date_str.split("-"))
    return _d(y, m, d).weekday() == 0


def _benchmark_dca(
    symbols: List[str],
    daily: Dict[str, List[float]],
    dates: List[str],
    idx: List[int],
    per_week: float,
    fee: float,
) -> Dict[str, float]:
    """定投基准：每周一各币投入 per_week，按当日收盘买入（含费），持有到期末。"""
    qty = {s: 0.0 for s in symbols}
    invested = 0.0
    for i in idx:
        if _is_monday(dates[i]):
            for s in symbols:
                spend = per_week * (1 + fee)
                qty[s] += spend / daily[s][i]
                invested += spend
    return {"invested": round(invested, 2), "qty": {s: round(q, 8) for s, q in qty.items()}}


def _summarize(
    symbols, pos, d_closes, w_closes, m_closes, dates, idx,
    budget, cfg: SpotDefaults, orders, fee, invested, sold_total,
) -> Dict[str, Any]:
    """期末汇总：净增币 / vs 定投基准超额 / 现金守恒 / 评分分布。"""
    last = idx[-1]
    total_qty = sum(pos[s].qty for s in symbols)
    total_mv = sum(pos[s].qty * d_closes[s][last] for s in symbols)
    total_cost = sum(pos[s].cost_usdt for s in symbols)
    total_realized = sum(pos[s].realized_usdt for s in symbols)

    # 定投基准（同预算同节奏：每周一每币 dca_amount）
    bench = _benchmark_dca(
        symbols, d_closes, dates, idx,
        cfg.dca_amount if cfg.dca_enabled else cfg.per_order_usdt, fee,
    )
    bench_qty = sum(bench["qty"].values())

    action_dist: Dict[str, int] = {}
    for o in orders:
        key = f"{o['side']}_{o['trigger']}"
        action_dist[key] = action_dist.get(key, 0) + 1

    return {
        "symbols": symbols,
        "bars": len(idx),
        "start": dates[idx[0]],
        "end": dates[idx[-1]],
        "budget": budget,
        "total_qty": round(total_qty, 8),
        "coin_unit": "混合" if len(symbols) > 1 else symbols[0].replace("USDT", ""),
        "total_market_value": round(total_mv, 2),
        "total_cost": round(total_cost, 2),
        "total_realized": round(total_realized, 2),
        # 现金守恒：预算 − 累计买入含费支出 + 累计卖出含费回款
        "cash_left": round(budget - invested + sold_total, 2),
        "invested": round(invested, 2),
        "orders_count": len(orders),
        "action_dist": action_dist,
        "benchmark": {
            "invested": bench["invested"],
            "total_qty": bench_qty,
            "qty_per_symbol": bench["qty"],
        },
        # 币本位超额：系统净增币 − 同期定投基准净增币（混合币种时按各自币对比再按市值折 U）
        "excess_qty": {s: round(pos[s].qty - bench["qty"][s], 8) for s in symbols},
    }
