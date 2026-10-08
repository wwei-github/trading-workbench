"""实盘归因统计（复盘重设计 2026-09-18）

复盘主轴从「模拟回放 AI 建议」转向「实盘交易 + 计划对照」：
- 总览：胜率/盈亏比/期望值/持仓时长/费用拆分
- 计划 vs 实际：计划风险（risk_amount 合计）对照实际盈亏，偏差归因到费用
- 分组：开单类型 / 方向 / 出场原因 / 错误标签 / 开仓时段（北京时间 4h 桶）
- 明细：逐笔带 AI 归因结论，联动交易复盘K线

存量平仓单的 fee/funding 在结算字段上线前未落库，此处惰性回补
（income 明细按 symbol+opened_at 从交易所拉取，回写后一次性生效）。
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trade import TradeRecord
from app.services.binance_trader import BinanceTrader
from app.services.strategy.candlestick import PATTERN_LABEL_MAP
from app.services.strategy.trigger import TOUCH_LABELS as _TOUCH_LABELS
from app.services.strategy.trigger import TOUCH_ORDER as _TOUCH_ORDER

logger = logging.getLogger(__name__)

# 开仓时段桶（北京时间，4h 一档）：交易记录 opened_at 为 UTC
HOUR_BUCKETS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]

# 金K形态分组固定顺序（2026-10-01-3 完整 12 金K：看涨 6 + 看跌 6，与
# GOLDEN_12 同源同序）；展示名取 PATTERN_LABEL_MAP，防两处漂移
GOLDEN_PATTERN_ORDER = (
    "hammer", "bullish_engulfing", "morning_star",
    "piercing_line", "bullish_harami", "dragonfly_doji",
    "shooting_star", "bearish_engulfing", "evening_star",
    "dark_cloud_cover", "bearish_harami", "gravestone_doji",
)

# 结算费用回补单轮上限（防一次性拉爆交易所限额；未补完的下轮统计继续）
_BACKFILL_LIMIT = 20


def backfill_costs(db: Session, trader: BinanceTrader, limit: int = _BACKFILL_LIMIT) -> int:
    """为 fee_total 为空的已平仓交易回补手续费/资金费（一次性，回写后不再查）。

    按记录归属账号取 income（docs/17）：account_key 缺失旧行按 testnet 回落；
    账号已从 .env 移除时回落环境默认账号。"""
    rows = db.execute(
        select(TradeRecord)
        .where(
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_reason.isnot(None),
            TradeRecord.fee_total.is_(None),
        )
        .order_by(TradeRecord.closed_at.desc())
        .limit(limit)
    ).scalars().all()
    traders: dict[str, BinanceTrader] = {}

    def _rec_trader(rec: TradeRecord) -> BinanceTrader:
        key = rec.account_key or ("testnet-1" if rec.testnet else "live-1")
        if key not in traders:
            from app.services.account_registry import get_account
            traders[key] = BinanceTrader(key) if get_account(key) else (
                trader if trader.testnet == rec.testnet else BinanceTrader(rec.testnet)
            )
        return traders[key]

    n = 0
    for rec in rows:
        try:
            since_ms = int(
                rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            ) - 5_000 if rec.opened_at else 0
            income = _rec_trader(rec).income_rows(rec.symbol, since_ms)
            rec.fee_total = round(sum(
                float(r.get("income") or 0) for r in income
                if r.get("incomeType") == "COMMISSION"), 6)
            rec.funding_total = round(sum(
                float(r.get("income") or 0) for r in income
                if r.get("incomeType") == "FUNDING_FEE"), 6)
            db.commit()
            n += 1
        except Exception as e:
            logger.warning("费用回补失败 %s: %s（按 0 处理，避免反复重试）", rec.symbol, e)
            rec.fee_total, rec.funding_total = 0.0, 0.0
            db.commit()
            n += 1
    return n


def _trade_type_of(rec: TradeRecord) -> str:
    return (rec.ai_snapshot or {}).get("trade_type") or "unknown"


# 触发器家族映射（/review/stats 与实盘归因分组共用同源常量，含旧管线别名；
# NULL/未识别 → 「未记录」，兼容导入持仓与 signal_type 列上线前的旧行）
TRIGGER_FAMILY = {
    "trigger_breakout": "同向突破",
    "trigger_trend_breakout": "趋势突破",
    "breakout": "同向突破",            # 旧管线·放量突破
    "downtrend_breakout": "同向突破",  # 旧管线·下跌突破
    "trigger_pullback": "回踩金K确认",
    "trigger_pullback_confirm": "回踩金K确认",
    "uptrend_pullback": "回踩金K确认",  # 旧管线·上涨回调
    "trigger_candlestick": "同向形态",  # 2026-09-29 纯金K下线，仅历史行
}


def _signal_family_of(rec: TradeRecord) -> str:
    return TRIGGER_FAMILY.get(rec.signal_type or "") or "未记录"


def _hold_hours(rec: TradeRecord) -> float:
    if not rec.opened_at or not rec.closed_at:
        return 0.0
    return round((rec.closed_at - rec.opened_at).total_seconds() / 3600, 1)


def _bucket_label(rec: TradeRecord) -> str:
    """开仓时段（北京时间 4h 桶）"""
    if not rec.opened_at:
        return "未知"
    cst = rec.opened_at + timedelta(hours=8)
    for lo, hi in HOUR_BUCKETS:
        if lo <= cst.hour < hi:
            return f"北京 {lo:02d}-{hi:02d} 时"
    return "未知"


def _rec_bucket(rec: TradeRecord) -> str:
    """AI 评分分桶（docs/24 D4）：TRADING_MIN_RECOMMENDATION=60 是开仓铁闸，
    分桶×盈亏交叉表检验评分与后续盈亏的相关性（无相关性→评分体系失效）"""
    r = (rec.ai_snapshot or {}).get("recommendation")
    if r is None:
        return "未知"
    r = float(r)
    if r < 60:
        return "<60"
    if r < 70:
        return "60~69"
    if r < 80:
        return "70~79"
    if r < 90:
        return "80~89"
    return ">=90"


# 评分分桶展示顺序（字符串排序会乱序，显式列出）
_REC_BUCKETS = ("<60", "60~69", "70~79", "80~89", ">=90", "未知")


def _volume_bucket(rec: TradeRecord) -> str:
    """量比分桶（docs/24 A5b）：量比 = 信号K线量 ÷ 近20根基准量（中位量）。
    1.0 缩量线（缩量突破无近锚资格 docs/23）、1.2 放量加分线（scan_filters
    breakout_vol_ratio 默认）——分组验证"放量突破质量更高"假设"""
    v = (rec.ai_snapshot or {}).get("volume_ratio")
    if v is None:
        return "未知"
    v = float(v)
    if v < 1.0:
        return "缩量 <1.0"
    if v < 1.2:
        return "常量 1.0~1.2"
    return "放量 >=1.2"


# 量比分桶展示顺序
_VOLUME_BUCKETS = ("缩量 <1.0", "常量 1.0~1.2", "放量 >=1.2", "未知")

# 回踩触点分组（2026-10-08，docs/22）：固定顺序与中文标签同源自 trigger.TOUCH_*，
# 突破类无触点/旧行快照缺 → 「未记录」兜底行排最后
_TOUCH_BUCKETS = tuple(_TOUCH_LABELS[t] for t in _TOUCH_ORDER) + ("未记录",)


def _touch_bucket(rec: TradeRecord) -> str:
    raw = (rec.ai_snapshot or {}).get("touch_ref")
    return _TOUCH_LABELS.get(raw) if raw in _TOUCH_LABELS else "未记录"


def _agg(trades: list) -> dict:
    # Numeric 列经 SQLAlchemy 返回 Decimal，统一转 float 再聚合——
    # 否则空 wins 时 avg_win=0.0(float) / avg_loss=Decimal 触发
    # "unsupported operand type(s) for /: 'float' and 'decimal.Decimal'"
    def _pnl(t) -> float:
        return float(t.realized_pnl or 0)

    wins = [t for t in trades if _pnl(t) > 0]
    losses = [t for t in trades if _pnl(t) < 0]
    pnl = round(sum(_pnl(t) for t in trades), 2)
    avg_win = round(sum(_pnl(t) for t in wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(_pnl(t) for t in losses) / len(losses), 2) if losses else 0.0
    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        # 净胜率（docs/24 A2 主口径）：pnl>0 / 全部闭仓数，不做 win_tp1/expired 之类的口径美化
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "pnl": pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": round(abs(avg_win / avg_loss), 2) if (wins and losses) else None,  # 盈亏比（需双侧样本）
        "ev_per_trade": round(pnl / len(trades), 2) if trades else 0.0,  # 期望值/笔 USDT
    }


def build_actual_review(db: Session, days: int, extra_filter=None) -> dict:
    """实盘归因统计主入口（API 层直接返回 dict）

    extra_filter: 可选 scope 过滤条件（api/trades._account_scope_filter 产物，
    live/testnet=环境聚合、账号 key=指定账号；None=全部，兼容旧行为）
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = select(TradeRecord).where(
        TradeRecord.status == "CLOSED",
        TradeRecord.exit_reason.isnot(None),
        TradeRecord.closed_at >= cutoff,
    )
    if extra_filter is not None:
        q = q.where(extra_filter)
    trades = db.execute(
        q.order_by(TradeRecord.closed_at.asc())
    ).scalars().all()

    overview = _agg(trades)
    total_fee = round(sum(t.fee_total or 0 for t in trades), 2)
    total_funding = round(sum(t.funding_total or 0 for t in trades), 2)
    planned_risk = round(sum(float(t.risk_amount or 0) for t in trades), 2)

    # 双网镜像单（2026-10-04）：同一 AI 决策扇出双网的两笔 CLOSED 单，all 口径
    # 下被当独立样本重复计入。返回计数供前端在 all 视图提示「归因结论以正式网
    # 口径为准」；不做合并剔除——双网执行差异（滑点/费用/成交价）是免费的执行
    # 质量对照组，且正式网样本本就少
    by_analysis: dict = {}
    for t in trades:
        if t.ai_analysis_id:
            by_analysis.setdefault(t.ai_analysis_id, set()).add(bool(t.testnet))
    mirror_trades = sum(
        1 for t in trades
        if t.ai_analysis_id and len(by_analysis[t.ai_analysis_id]) > 1
    )

    overview.update({
        "days": days,
        "total_fee": total_fee,
        "total_funding": total_funding,
        "planned_risk_total": planned_risk,
        # 计划 vs 实际：亏超计划的部分 = |实际净亏| − 计划风险合计（差额主要来自费用；
        # ≤0 即未超计划，钳为 0——前端 plan_gap>0.5 展示"亏超计划"）
        "plan_gap": round(max(0.0, -min(overview["pnl"], 0) - planned_risk), 2),
        "avg_hold_hours": round(
            sum(_hold_hours(t) for t in trades) / len(trades), 1) if trades else 0.0,
        "attributed": sum(1 for t in trades if t.attributed_at),  # 已归因笔数
        # 双网镜像单笔数（同一 AI 决策双网都开了的 CLOSED 单；仅 all 口径非 0）
        "mirror_trades": mirror_trades,
    })

    # 分组：维度 × 桶
    groups: list[dict] = []

    def push(dim: str, key: str, bucket: list):
        if not bucket:
            return
        groups.append({"dim": dim, "key": key, **_agg(bucket)})

    for dim, keyfn in (
        ("开单类型", _trade_type_of),
        ("信号类型", _signal_family_of),
        ("方向", lambda t: t.direction or "未知"),
        ("出场原因", lambda t: t.exit_reason or "未知"),
        ("开仓时段", _bucket_label),
        # 网络（2026-10-04）：测试网 vs 正式网分桶——模拟撮合的成绩证明不了策略
        # 期望，归因考核锚定正式网；all 口径的双网对比 = 执行质量对照（滑点/费用）
        ("网络", lambda t: "测试网" if t.testnet else "正式网"),
        # 止损锚类型（docs/24 A5b）：structure/near 分组对比近锚单是否劣化
        ("止损锚", lambda t: (t.ai_snapshot or {}).get("stop_anchor") or "structure"),
        # 战法（2026-10-02，仅借鉴标注）：按命中战法分组对比实盘胜率/EV——
        # 哪个战法真能打，数据说话；未命中/旧行归「未指定」
        ("战法", lambda t: (t.ai_snapshot or {}).get("tactic_name") or "未指定"),
        # 规则版本（docs/24 A0）：同版本内分组对比，legacy=标记上线前旧行；
        # 历史跨版本混合成绩不具证明力（docs/24 修订 v2）
        ("规则版本", lambda t: t.rule_version or "legacy"),
    ):
        buckets: dict[str, list] = {}
        for t in trades:
            buckets.setdefault(keyfn(t), []).append(t)
        for key, bucket in sorted(buckets.items()):
            push(dim, key, bucket)

    # 有序分桶维度（docs/24 A5b/D4）：按桶的自然顺序输出而非字典序
    for dim, keyfn, order in (
        ("评分分桶", _rec_bucket, _REC_BUCKETS),
        ("量比分桶", _volume_bucket, _VOLUME_BUCKETS),
        # 回踩触点（2026-10-08，docs/22）：回踩单质量分层——浅触点（突破回测位）
        # 与深回撤（EMA144）的胜率/EV 对比，验证「回踩越深越稳」假设
        ("回踩位置", _touch_bucket, _TOUCH_BUCKETS),
    ):
        buckets: dict[str, list] = {}
        for t in trades:
            buckets.setdefault(keyfn(t), []).append(t)
        for key in order:
            if key in buckets:
                push(dim, key, buckets.pop(key))
        for key, bucket in sorted(buckets.items()):  # 防御：未登记桶名兜底
            push(dim, key, bucket)

    # 金K形态维度（2026-10-01-3 用户拍板）：完整 12 金K全量列出（0 笔也占行，
    # 12 维度齐全便于横向对比各形态样本）；突破类无金K形态、旧行 pattern 为
    # NULL → 「未记录」兜底行排最后
    pat_buckets: dict[str, list] = {}
    for t in trades:
        pat_buckets.setdefault(t.pattern or "未记录", []).append(t)
    for p in GOLDEN_PATTERN_ORDER:
        groups.append({"dim": "金K形态",
                       "key": PATTERN_LABEL_MAP.get(p, p),
                       **_agg(pat_buckets.pop(p, []))})
    for key, bucket in sorted(pat_buckets.items()):
        push("金K形态", key, bucket)

    # 错误标签维度：一笔多标签拆行（仅统计已归因的）
    tag_buckets: dict[str, list] = {}
    for t in trades:
        for tag in (t.error_tags or []):
            tag_buckets.setdefault(str(tag), []).append(t)
    for key, bucket in sorted(tag_buckets.items()):
        push("错误标签", key, bucket)

    items = [{
        "id": str(t.id),
        "symbol": t.symbol,
        "direction": t.direction,
        "trade_type": _trade_type_of(t),
        "exit_reason": t.exit_reason,
        "entry_plan": (t.ai_snapshot or {}).get("entry_price"),
        "sl_plan": (t.ai_snapshot or {}).get("stop_loss"),
        "tp1_plan": (t.ai_snapshot or {}).get("take_profit_1"),
        "entry_actual": float(t.entry_price) if t.entry_price else None,
        "realized_pnl": float(t.realized_pnl) if t.realized_pnl is not None else None,
        "pnl_pct": float(t.pnl_pct) if t.pnl_pct is not None else None,
        "risk_amount": float(t.risk_amount) if t.risk_amount is not None else None,
        "fee_total": float(t.fee_total) if t.fee_total is not None else None,
        "funding_total": float(t.funding_total) if t.funding_total is not None else None,
        "recommendation": (t.ai_snapshot or {}).get("recommendation"),
        # 止损锚类型（docs/23）：structure/near，供归因按锚类型对比近锚单胜率/EV
        "stop_anchor": (t.ai_snapshot or {}).get("stop_anchor") or "structure",
        # 战法命中标注（2026-10-02，仅借鉴）：随快照自持，逐笔展示+分组数据源
        "tactic_name": (t.ai_snapshot or {}).get("tactic_name"),
        "tactic_reason": (t.ai_snapshot or {}).get("tactic_reason"),
        # 量比（docs/24 A5b）：信号K线量 ÷ 近20根基准量，量比门槛分组数据源
        "volume_ratio": (t.ai_snapshot or {}).get("volume_ratio"),
        # 回踩触点（docs/22，2026-10-08）：逐笔展示 + 分组数据源（原始值
        # retest/swing/ema21/55/144，旧行/突破类 None）
        "touch_ref": (t.ai_snapshot or {}).get("touch_ref"),
        # 规则版本（docs/24 A0）：逐笔规则集快照，legacy=标记上线前旧行
        "rule_version": t.rule_version or "legacy",
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "hold_hours": _hold_hours(t),
        "error_tags": t.error_tags or [],
        "error_lesson": t.error_lesson,
        "attributed": bool(t.attributed_at),
    } for t in trades]

    return {"overview": overview, "groups": groups, "items": items}
