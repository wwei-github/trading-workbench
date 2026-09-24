"""每日飞书日报（2026-09-20）

日线收盘（UTC 00:00 = 北京 08:00）后推送：
- 三大主流币（BTC/ETH/BNB）当日日线收盘价
- 当日自动交易：开单数、闭单数、止盈/止损笔数、净收益

统计口径（与交易记录页一致）：
- 「当日」按北京时间自然日切
- 环境跟随 system_config.trading_testnet 当前交易环境（2026-09-24）：
  测试网模式统计测试网数据、正式网模式统计正式网（live 聚合）数据，
  卡片标题标注环境；环境切换后次日日报自动跟随
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


@celery_app.task(name="app.tasks.daily_report.notify_daily_report")
def notify_daily_report() -> dict:
    """每日 08:05（北京）日报：主流币收盘价 + 昨日交易统计。"""
    now = datetime.now(_TZ_BJ)
    # 日报在收盘后 5 分钟跑，统计「昨天」一整天（北京自然日）
    day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    date_label = day_start.strftime("%Y-%m-%d")

    # 主流币收盘价（并发拉省时）
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        prices = list(ex.map(_daily_close, _MAJOR_SYMBOLS))

    # 当日交易统计（按当前交易环境过滤：测试网/正式网 live 聚合）
    db = SessionLocal()
    try:
        from app.models.system_config import SystemConfig

        cfg = db.get(SystemConfig, 1)
        is_testnet = bool(cfg.trading_testnet) if cfg is not None else True
        env_label = "测试网" if is_testnet else "正式网"
        env_filter = TradeRecord.testnet == is_testnet

        rows = db.execute(
            select(TradeRecord).where(
                env_filter,
                TradeRecord.opened_at >= day_start.replace(tzinfo=None),
                TradeRecord.opened_at < day_end.replace(tzinfo=None),
            )
        ).scalars().all()
        opened = len(rows)

        closed_rows = db.execute(
            select(TradeRecord).where(
                env_filter,
                TradeRecord.closed_at >= day_start.replace(tzinfo=None),
                TradeRecord.closed_at < day_end.replace(tzinfo=None),
                TradeRecord.status == "CLOSED",
            )
        ).scalars().all()
        closed = len(closed_rows)
        win = sum(1 for r in closed_rows if (r.realized_pnl or 0) > 0)
        loss = closed - win
        net = sum(float(r.realized_pnl or 0) for r in closed_rows)

        running = sum(1 for r in rows if r.status in ("OPENED", "TP1_HIT", "TP2_HIT"))
        failed = sum(1 for r in rows if r.status == "FAILED")

        # 胜率口径：该环境全部历史已闭单（不只当日），与交易记录页概览一致
        total_closed = db.execute(
            select(func.count(TradeRecord.id)).where(
                env_filter, TradeRecord.status == "CLOSED"
            )
        ).scalar() or 0
        total_win = db.execute(
            select(func.count(TradeRecord.id)).where(
                env_filter,
                TradeRecord.status == "CLOSED",
                TradeRecord.realized_pnl > 0,
            )
        ).scalar() or 0
    finally:
        db.close()

    def _net_str(v: float) -> str:
        sign = "+" if v > 0 else ""
        return f"{sign}{v:,.2f} USDT"

    # 胜率：全部历史已闭单口径（total_win/total_closed）；无闭单显示 —
    win_rate = f"{total_win / total_closed * 100:.0f}%" if total_closed else "—"

    # ── 飞书消息卡片（interactive）：列对齐由卡片布局引擎负责。
    #    文本消息是比例字体，空格补位永远对不齐价格列/个位数，卡片栅格才是真对齐 ──
    def _col(title: str, *lines: str) -> dict:
        """等宽 column 单元：标题加粗 + 下方数据行（行情区用）。"""
        return {
            "tag": "column", "width": "weighted", "weight": 1,
            "elements": [{"tag": "div", "text": {"tag": "lark_md",
                         "content": "\n".join([f"**{title}**", *lines])}}],
        }

    # 头部：净收益为正绿/为负红/持平蓝，emoji 跟随
    head_emoji = "📈" if net > 0 else ("📉" if net < 0 else "📊")

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

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{head_emoji} 每日交易日报 · {date_label} · {env_label}"},
            "template": "green" if net > 0 else ("red" if net < 0 else "blue"),
        },
        "elements": [
            # ── 行情区：3 列（按价格位数加权），币种/收盘价(粗体)/涨跌幅 ──
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📈 主流币日收盘**"}},
            {"tag": "column_set", "flex_mode": "stretch", "columns": price_cols},
            {"tag": "hr"},
            # ── 统计区：原生 fields 两列栅格，每格独立单元格（单双位数不挤歪）──
            {"tag": "div", "text": {"tag": "lark_md", "content": "**💼 交易统计**"}},
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**开单**　{opened} 笔"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**闭单**　{closed} 笔"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**止盈**　{win} 笔 <font color='success'>✔</font>"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**止损**　{loss} 笔 <font color='red'>✖</font>"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**胜率**　{win_rate}（{total_win}/{total_closed}）"}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": f"**在跑**　{running} 笔 · 失败 {failed}"}},
            ]},
            {"tag": "hr"},
            # ── 收益区：独立一行加粗突出 ──
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"💰 **净收益　{_net_str(net)}**"}},
            # ── 口径脚注（小字灰显）──
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": (f"统计范围 = 当前交易环境（{env_label}）· 胜率 = 止盈笔数 ÷ 全部已闭单 · "
                         "净收益只计已闭单（含手续费/资金费），在跑单浮动盈亏不计")}]},
        ],
    }
    ok = send_feishu_card(card)
    if not ok:
        # 卡片发送失败降级为纯文本（保底不让日报丢消息，对齐性让位于送达）
        text = "\n".join([
            f"📊 每日交易日报 · {date_label} · {env_label}",
            *[f"{name} 收盘 {price} {chg}" for name, price, chg in prices],
            f"开单 {opened}（在跑 {running} · 失败 {failed}）/ 闭单 {closed}",
            f"止盈 {win} / 止损 {loss} / 胜率 {win_rate}（{total_win}/{total_closed}）",
            f"净收益 {_net_str(net)}",
        ])
        ok = send_feishu_text(text)
    logger.info("每日日报已%s（%s）", "发送" if ok else "跳过/失败", date_label)
    return {"date": date_label, "opened": opened, "closed": closed,
            "win": win, "loss": loss, "net": round(net, 2), "notified": ok}
