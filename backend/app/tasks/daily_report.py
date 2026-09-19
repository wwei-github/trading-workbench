"""每日飞书日报（2026-09-20）

日线收盘（UTC 00:00 = 北京 08:00）后推送：
- 三大主流币（BTC/ETH/BNB）当日日线收盘价
- 当日自动交易：开单数、闭单数、止盈/止损笔数、净收益

统计口径（与交易记录页一致）：
- 「当日」按北京时间自然日切
- 止盈 = 已闭单且净收益 > 0；止损 = 已闭单且净收益 <= 0（保本/微亏归止损侧）
- 净收益 = 当日闭单 realized_pnl 之和（含手续费/资金费），在跑单浮动盈亏不计
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.trade import TradeRecord
from app.services.notify import send_feishu_text

logger = logging.getLogger(__name__)

_TZ_BJ = timezone(timedelta(hours=8))
_MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")


def _daily_close(symbol: str) -> tuple[str, str, str]:
    """最新已收盘日线 (币种名, 收盘价文本, 24h涨跌幅文本)。失败返回 '获取失败'，不中断整体。

    涨跌幅 = (收盘-开盘)/开盘，取刚收的这根日线（[-2]）的开/收盘价。
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
        arrow = "🔺" if chg > 0 else ("🔻" if chg < 0 else "➖")
        return name, f"{close:,.2f}", f"{arrow} {chg:+.2f}%"
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

    # 当日交易统计
    db = SessionLocal()
    try:
        rows = db.execute(
            select(TradeRecord).where(
                TradeRecord.opened_at >= day_start.replace(tzinfo=None),
                TradeRecord.opened_at < day_end.replace(tzinfo=None),
            )
        ).scalars().all()
        opened = len(rows)

        closed_rows = db.execute(
            select(TradeRecord).where(
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
    finally:
        db.close()

    def _net_str(v: float) -> str:
        sign = "+" if v > 0 else ""
        return f"{sign}{v:,.2f} USDT"

    # 胜率（止盈占已闭单比例；无闭单不显示）
    win_rate = f"{win / closed * 100:.0f}%" if closed else "—"

    # ── 飞书文本排版：分区线 + 双列行情（价格 + 涨跌幅）──
    sep = "━━━━━━━━━━━━━━━━━━━━"
    lines = [
        f"📊 每日交易日报 · {date_label}",
        "",
        "📈 主流币日收盘",
        *[
            # 币种 | 收盘价 | 涨跌幅，三列对齐
            f"　{name:<4}{price:<14}{chg}"
            for name, price, chg in prices
        ],
        sep,
        "💼 交易统计",
        f"　开单　{opened} 笔　·　在跑 {running}　·　失败 {failed}",
        f"　闭单　{closed} 笔",
        f"　止盈　{win} 笔　✅",
        f"　止损　{loss} 笔",
        f"　胜率　{win_rate}",
        sep,
        f"💰 净收益　{_net_str(net)}",
    ]
    text = "\n".join(lines)
    ok = send_feishu_text(text)
    logger.info("每日日报已%s（%s）", "发送" if ok else "跳过/失败", date_label)
    return {"date": date_label, "opened": opened, "closed": closed,
            "win": win, "loss": loss, "net": round(net, 2), "notified": ok}
