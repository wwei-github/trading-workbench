"""每日飞书日报（2026-09-20）

日线收盘（UTC 00:00 = 北京 08:00）后推送：
- 三大主流币（BTC/ETH/BNB）当日日线收盘价
- 当日自动交易：开单数、闭单数、止盈/止损笔数、净收益

统计口径（与交易记录页一致）：
- 「当日」按北京时间自然日切
- 2026-10-19：交易统计与净收益按【正式网 / 测试网】分列（此前为双网合计）。
  双网同时在跑（2026-10-03 删环境切换后），测试网是模拟撮合且仓位大数倍，
  混网合计会淹没真钱表现；分列后各网独立呈现（口径对齐交易记录页 scope）。
- 止盈 = 已闭单且净收益 > 0；止损 = 已闭单且净收益 <= 0（保本/微亏归止损侧）
- 净收益 = 当日闭单 realized_pnl 之和（含手续费/资金费），在跑单浮动盈亏不计
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.trade import TradeRecord
from app.services.notify import send_feishu_card, send_feishu_text

logger = logging.getLogger(__name__)

_TZ_BJ = timezone(timedelta(hours=8))
_MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")


def _daily_close(symbol: str) -> tuple[str, str, str]:
    """最新已收盘日线 (币种名, 收盘价文本, 24h涨跌幅 lark_md 文本)。失败不中断整体。

    涨跌幅 = (收盘-开盘)/开盘，取刚收的这根日线（[-2]）的开/收盘价。
    颜色：涨绿 <font color='info'>、跌红 <font color='warning'>（卡片内渲染）。
    """
    name = symbol.replace("USDT", "")
    try:
        from app.services.exchange_pool import ExchangePool

        klines = ExchangePool().get_klines(symbol, "1d", 2)
        # klines[-1] 未收盘，[-2] 为刚收的日线；[1]=open [4]=close
        k = klines[-2]
        close, open_ = float(k[4]), float(k[1])
        if open_ <= 0:
            return name, f"{close:,.2f}", "—"
        chg = (close - open_) / open_ * 100
        if chg > 0:
            chg_md = f"<font color='success'>{chg:+.2f}%</font>"   # 涨绿
        elif chg < 0:
            chg_md = f"<font color='red'>{chg:+.2f}%</font>"       # 跌红
        else:
            chg_md = f"{chg:+.2f}%"
        return name, f"{close:,.2f}", chg_md
    except Exception as e:
        logger.warning("日线收盘价获取失败 %s: %s", symbol, e)
        return name, "获取失败", ""


def _env_day_stats(db, day_start: datetime, day_end: datetime, is_testnet: bool) -> dict:
    """单网（正式网 / 测试网）当日交易统计 + 该网全部历史已闭单胜率口径。

    口径与交易记录页一致：异常单（excluded）剔除；止盈 = 已闭单且净收益 > 0，
    止损 = 其余已闭单（保本/微亏归止损侧）；净收益 = 当日闭单 realized_pnl 之和
    （含手续费/资金费），在跑单浮动盈亏不计。
    """
    base = TradeRecord.testnet.is_(is_testnet)
    ds, de = day_start.replace(tzinfo=None), day_end.replace(tzinfo=None)
    rows = db.execute(
        select(TradeRecord).where(
            TradeRecord.opened_at >= ds,
            TradeRecord.opened_at < de,
            TradeRecord.excluded.is_(False),
            base,
        )
    ).scalars().all()
    closed_rows = db.execute(
        select(TradeRecord).where(
            TradeRecord.closed_at >= ds,
            TradeRecord.closed_at < de,
            TradeRecord.status == "CLOSED",
            TradeRecord.excluded.is_(False),
            base,
        )
    ).scalars().all()
    win = sum(1 for r in closed_rows if (r.realized_pnl or 0) > 0)
    # 胜率口径：该网全部历史已闭单（不只当日），与交易记录页概览一致
    total_closed = db.execute(
        select(func.count(TradeRecord.id)).where(
            TradeRecord.status == "CLOSED",
            TradeRecord.excluded.is_(False),
            base,
        )
    ).scalar() or 0
    total_win = db.execute(
        select(func.count(TradeRecord.id)).where(
            TradeRecord.status == "CLOSED",
            TradeRecord.excluded.is_(False),
            TradeRecord.realized_pnl > 0,
            base,
        )
    ).scalar() or 0
    return {
        "opened": len(rows),
        "closed": len(closed_rows),
        "win": win,
        "loss": len(closed_rows) - win,
        "net": sum(float(r.realized_pnl or 0) for r in closed_rows),
        "running": sum(1 for r in rows if r.status in ("OPENED", "TP1_HIT", "TP2_HIT")),
        "failed": sum(1 for r in rows if r.status == "FAILED"),
        "total_closed": total_closed,
        "total_win": total_win,
    }


@celery_app.task(name="app.tasks.daily_report.notify_daily_report")
def notify_daily_report() -> dict:
    """每日 08:05（北京）日报：主流币收盘价 + 昨日交易统计。"""
    from app.services.task_log import close_task, open_task

    task_id = open_task("review", "每日交易日报", trigger="scheduled")
    try:
        return _notify_daily_report_body(task_id)
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("每日日报任务失败")
        raise


def _notify_daily_report_body(task_id: str) -> dict:
    """日报主体（任务记录由外层管理）：行情 + 统计 + 卡片组装 + 推送。"""
    now = datetime.now(_TZ_BJ)
    # 日报在收盘后 5 分钟跑，统计「昨天」一整天（北京自然日）
    day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    date_label = day_start.strftime("%Y-%m-%d")

    # 主流币收盘价（并发拉省时）
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        prices = list(ex.map(_daily_close, _MAJOR_SYMBOLS))

    # 当日交易统计（2026-10-19）：正式网 / 测试网分列——双网同时在跑，
    # 测试网是模拟撮合且仓位大数倍，混网合计会淹没真钱表现。
    db = SessionLocal()
    try:
        live = _env_day_stats(db, day_start, day_end, is_testnet=False)
        testnet = _env_day_stats(db, day_start, day_end, is_testnet=True)
    finally:
        db.close()

    def _net_str(v: float) -> str:
        sign = "+" if v > 0 else ""
        return f"{sign}{v:,.2f} USDT"

    def _win_rate(s: dict) -> str:
        """胜率：该网全部历史已闭单口径（total_win/total_closed）；无闭单显示 —"""
        return f"{s['total_win'] / s['total_closed'] * 100:.0f}%" if s["total_closed"] else "—"

    def _stat_fields(s: dict) -> list[dict]:
        """单网统计 6 格（原生 fields 两列栅格，与旧版布局一致）。"""
        return [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**开单**　{s['opened']} 笔"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**闭单**　{s['closed']} 笔"}},
            {"is_short": True, "text": {"tag": "lark_md",
             "content": f"**止盈**　{s['win']} 笔 <font color='success'>✔</font>"}},
            {"is_short": True, "text": {"tag": "lark_md",
             "content": f"**止损**　{s['loss']} 笔 <font color='red'>✖</font>"}},
            {"is_short": True, "text": {"tag": "lark_md",
             "content": f"**胜率**　{_win_rate(s)}（{s['total_win']}/{s['total_closed']}）"}},
            {"is_short": True, "text": {"tag": "lark_md",
             "content": f"**在跑**　{s['running']} 笔 · 失败 {s['failed']}"}},
        ]

    # ── 飞书消息卡片（interactive）：列对齐由卡片布局引擎负责。
    #    文本消息是比例字体，空格补位永远对不齐价格列/个位数，卡片栅格才是真对齐 ──
    def _col(title: str, *lines: str) -> dict:
        """等宽 column 单元：标题加粗 + 下方数据行（行情区用）。"""
        return {
            "tag": "column", "width": "weighted", "weight": 1,
            "elements": [{"tag": "div", "text": {"tag": "lark_md",
                         "content": "\n".join([f"**{title}**", *lines])}}],
        }

    # 头部：以正式网（真钱）净收益定色——为正绿/为负红/持平蓝，emoji 跟随。
    #    2026-10-19 分网后，标题不再取双网合计（混入模拟盘会误导真钱表现判断）
    head_net = live["net"]
    head_emoji = "📈" if head_net > 0 else ("📉" if head_net < 0 else "📊")

    # 行情列权重：BTC 价格位数多给大列，避免挤压换行
    price_weight = {"BTC": 4, "ETH": 3, "BNB": 3}
    price_cols = []
    for name, price, chg in prices:
        price_cols.append({
            "tag": "column", "width": "weighted",
            "weight": price_weight.get(name, 3),
            "elements": [{"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{name}**\n**{price}**\n{chg or '—'}"}}],
        })

    # ── 影子实验附录（docs/14 §六）：只统计不决策；无任何影子样本时整段省略。
    #    铁律：影子任何异常不得影响日报主流程，全部吞掉 ──
    shadow_lines: list[str] = []
    try:
        from app.tasks.shadow_tasks import shadow_report_lines
        shadow_lines = shadow_report_lines()
    except Exception as e:
        logger.debug("影子附录构建失败（忽略）: %s", e)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{head_emoji} 每日交易日报 · {date_label}"},
            "template": "green" if head_net > 0 else ("red" if head_net < 0 else "blue"),
        },
        "elements": [
            # ── 行情区：3 列（按价格位数加权），币种/收盘价(粗体)/涨跌幅 ──
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📈 主流币日收盘**"}},
            {"tag": "column_set", "flex_mode": "stretch", "columns": price_cols},
            {"tag": "hr"},
            # ── 统计区：正式网 / 测试网分列（各 6 格原生 fields 两列栅格）──
            {"tag": "div", "text": {"tag": "lark_md", "content": "**💼 交易统计**"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**正式网**"}},
            {"tag": "div", "fields": _stat_fields(live)},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**测试网**"}},
            {"tag": "div", "fields": _stat_fields(testnet)},
            {"tag": "hr"},
            # ── 收益区：分网净收益，各占一行加粗突出 ──
            {"tag": "div", "text": {"tag": "lark_md",
             "content": (f"💰 **净收益**\n"
                         f"**正式网**　{_net_str(live['net'])}\n"
                         f"**测试网**　{_net_str(testnet['net'])}")}},
            *([
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": "**🔬 影子实验**（只统计不决策，达标≠升格）\n" + "\n".join(shadow_lines)}},
            ] if shadow_lines else []),
            # ── 口径脚注（小字灰显）──
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": ("统计范围 = 正式网 / 测试网分列 · 胜率 = 止盈笔数 ÷ 该网全部已闭单 · "
                         "净收益只计已闭单（含手续费/资金费），在跑单浮动盈亏不计")}]},
        ],
    }
    ok = send_feishu_card(card)
    if not ok:
        # 卡片发送失败降级为纯文本（保底不让日报丢消息，对齐性让位于送达）
        text = "\n".join([
            f"📊 每日交易日报 · {date_label}",
            *[f"{name} 收盘 {price} {chg}" for name, price, chg in prices],
            # 分网统计（每网一行摘要，与卡片同口径）
            f"【正式网】开单 {live['opened']}（在跑 {live['running']} · 失败 {live['failed']}）"
            f"/ 闭单 {live['closed']}",
            f"【正式网】止盈 {live['win']} / 止损 {live['loss']} / "
            f"胜率 {_win_rate(live)}（{live['total_win']}/{live['total_closed']}）",
            f"【正式网】净收益 {_net_str(live['net'])}",
            f"【测试网】开单 {testnet['opened']}（在跑 {testnet['running']} · "
            f"失败 {testnet['failed']}）/ 闭单 {testnet['closed']}",
            f"【测试网】止盈 {testnet['win']} / 止损 {testnet['loss']} / "
            f"胜率 {_win_rate(testnet)}（{testnet['total_win']}/{testnet['total_closed']}）",
            f"【测试网】净收益 {_net_str(testnet['net'])}",
            *(["", "🔬 影子实验（只统计不决策）", *shadow_lines] if shadow_lines else []),
        ])
        ok = send_feishu_text(text)
    logger.info("每日日报已%s（%s）", "发送" if ok else "跳过/失败", date_label)
    from app.services.task_log import close_task

    close_task(
        task_id, "completed",
        summary=(f"正式网 开{live['opened']}/闭{live['closed']} 净{_net_str(live['net'])} · "
                 f"测试网 开{testnet['opened']}/闭{testnet['closed']} 净{_net_str(testnet['net'])}"
                 f"（飞书{'已推送' if ok else '未推送'}）"),
        detail={"date": date_label,
                "live_opened": live["opened"], "live_closed": live["closed"],
                "live_win": live["win"], "live_loss": live["loss"],
                "live_net": round(live["net"], 2),
                "tn_opened": testnet["opened"], "tn_closed": testnet["closed"],
                "tn_win": testnet["win"], "tn_loss": testnet["loss"],
                "tn_net": round(testnet["net"], 2),
                "notified": ok},
    )
    return {"date": date_label, "live": live, "testnet": testnet, "notified": ok}
