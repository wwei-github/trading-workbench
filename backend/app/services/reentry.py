"""止损后再入场检测与提醒（2026-10-31，仅提醒，不自动开单）。

来源：正式网 CLOSED + exit_reason="sl" 的交易。平仓时两道闸——① 止损那根
1h 必须带足够反向影线（≥N×实体 且 ≥M 全幅，见 settings.REENTRY_WICK_*）；
② 结构按 CARD-07 同口径（EMA 排列 / ADX≥20 / R²≥0.5）。两道都过才进入
watching，任一不过落对应终态。每小时 :06 巡检一次，只用**已收盘 1h** 的
high/low 判断是否回到原开仓价（entry ± θR），成立发一张飞书卡。
窗口内结构破坏、同币已有在跑仓、到期未触发或人工忽略则落对应终态。

硬约束：
- 只提醒，不下单、不建 TradeRecord、不改 SL/TP、不 bump RULE_VERSION；
- 仅正式网建监控（testnet=False）；
- #12 预注册回放 FAIL → 卡片与页面必须带免责声明。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

BAR_MS = 3_600_000  # 1h
CARD_DISCLAIMER = "仅提醒，不会自动开单；回测该再入场腿期望为负"


def compute_structure(bars: list[list], direction: str) -> Optional[dict]:
    """平仓/巡检时的 1h 结构快照（与 shadow.compute_reentry_precond 同口径）。

    bars 为时间升序 1h K 线（调用方负责已收盘口径）。三件全过才算结构完好：
    ① EMA 排列（多头 e21>e55，e144 可用时加 e55>e144；空头对称）
    ② ADX(14,1h) ≥ 20（analyze_adx 内部剔尾，与 shadow 一致）
    ③ R² ≥ 0.5（最近 48 根收盘线性回归）
    数据不可判（<66 根 / EMA 不可算）返回 None（本功能不打标、不建监控）。
    """
    from app.services.shadow import REENTRY_ADX_MIN, REENTRY_R2_BARS, REENTRY_R2_MIN
    from app.services.strategy.ema import calc_ema
    from app.services.strategy.swing import linear_regression
    from app.services.strategy.technical import analyze_adx

    if not bars or len(bars) < 66:
        return None
    closes = np.array([float(k[4]) for k in bars], dtype=float)
    e21 = calc_ema(closes, 21)
    e55 = calc_ema(closes, 55)
    e144 = calc_ema(closes, 144)
    if e21 is None or e55 is None:
        return None
    d = 1 if (direction or "long") == "long" else -1
    f, m = float(e21[-1]), float(e55[-1])
    if d > 0:
        ema_ok = f > m and (e144 is None or m > float(e144[-1]))
    else:
        ema_ok = f < m and (e144 is None or m < float(e144[-1]))
    adx_v = analyze_adx(bars, period=14)
    adx = float(adx_v["adx"]) if adx_v and adx_v.get("adx") is not None else None
    adx_ok = adx is not None and adx >= REENTRY_ADX_MIN
    win = closes[-REENTRY_R2_BARS:]
    _slope, _icpt, r2 = linear_regression(np.arange(len(win)), win)
    r2_ok = float(r2) >= REENTRY_R2_MIN
    return {
        "ema_ok": bool(ema_ok),
        "ema144_used": e144 is not None,
        "adx": round(adx, 2) if adx is not None else None,
        "adx_ok": bool(adx_ok),
        "r2": round(float(r2), 4),
        "r2_ok": bool(r2_ok),
        "structure_ok": bool(ema_ok and adx_ok and r2_ok),
    }


def _bar_open_ms(ts_ms: int) -> int:
    """包含 ts_ms 的 1h 根开盘时间；整点边界归前一根（止损常在根收盘瞬间）。"""
    return (int(ts_ms) - 1) // BAR_MS * BAR_MS


def _ms(dt: datetime) -> int:
    """库内 naive UTC 时间 → epoch 毫秒。

    必须显式按 UTC 解释：直接用 dt.timestamp() 会按宿主时区换算，非 UTC
    机器（如本地 macOS Asia/Shanghai）会整体偏移，导致找不到止损根。
    """
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def compute_stop_wick(bars: list[list], direction: str, closed_ms: int) -> Optional[dict]:
    """止损那根 1h 的反向影线质量（2026-10-31，用户拍板）。

    多头止损（向下跌穿）看下影 min(open,close)−low；空头看上涨影
    high−max(open,close)。合格 = 影线 ≥ N×实体 且 ≥ M 全幅（阈值
    settings.REENTRY_WICK_BODY_MULT / REENTRY_WICK_RANGE_MIN）——对应
    「插针打止损后快速收回」；影线不够长说明是有效跌破/突破，不做再入场。
    找不到止损根（K 线未覆盖/对齐异常）返回 None（不可判，不建监控）。
    """
    if not bars:
        return None
    target = _bar_open_ms(closed_ms)
    bar = next((k for k in bars if int(k[0]) == target), None)
    if bar is None:
        return None
    o, h, l, c = (float(bar[i]) for i in (1, 2, 3, 4))
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    shadow = (min(o, c) - l) if (direction or "long") == "long" else (h - max(o, c))
    shadow = max(0.0, shadow)
    ok = (shadow >= float(settings.REENTRY_WICK_BODY_MULT) * body
          and shadow >= float(settings.REENTRY_WICK_RANGE_MIN) * rng)
    return {
        "ok": bool(ok),
        "pct": round(shadow / rng, 4),
        "mult": round(shadow / body, 2) if body > 0 else None,
        "shadow": round(shadow, 8),
        "bar_open_ms": target,
    }


def _acc_key(account_key: Optional[str], testnet: bool) -> str:
    return account_key or ("testnet-1" if testnet else "live-1")


def register_reentry_watches(db, now: Optional[datetime] = None, limit: int = 20) -> int:
    """为最近止损平仓的正式网交易建监控（幂等：source_trade_id 唯一）。

    先看止损那根 1h 的反向影线（compute_stop_wick），再按止损根收盘时的
    结构判断：影线不合格 → wick_miss 终态；影线合格但结构不满足 →
    structure_broken 终态（两类终态都供页面解释为何没提醒）；K 线拿不到
    （影线/结构不可判）或没有初始止损快照 → 跳过不落行。逐笔提交，单笔
    异常回滚并吞掉，不影响其他笔。
    """
    from sqlalchemy import select

    from app.models.trade import ReentryWatch, TradeEvent, TradeRecord
    from app.services.exchange_pool import ExchangePool
    from app.services.shadow import REENTRY_LOOKBACK_H

    now = now or datetime.utcnow()
    since = now - timedelta(hours=settings.REENTRY_WINDOW_HOURS)
    rows = db.execute(
        select(TradeRecord).where(
            TradeRecord.status == "CLOSED",
            TradeRecord.testnet.is_(False),
            TradeRecord.excluded.is_(False),
            TradeRecord.exit_reason == "sl",
            TradeRecord.closed_at.isnot(None),
            TradeRecord.closed_at >= since,
            TradeRecord.closed_at <= now,
        ).order_by(TradeRecord.closed_at.asc()).limit(limit)
    ).scalars().all()
    if not rows:
        return 0
    existing = set(db.execute(
        select(ReentryWatch.source_trade_id).where(
            ReentryWatch.source_trade_id.in_([t.id for t in rows]))
    ).scalars().all())
    candidates = [t for t in rows if t.id not in existing]
    if not candidates:
        return 0
    stop_map: dict = {}
    evs = db.execute(
        select(TradeEvent.trade_record_id, TradeEvent.detail).where(
            TradeEvent.trade_record_id.in_([t.id for t in candidates]),
            TradeEvent.event_type == "OPEN",
        )
    ).all()
    for tr_id, detail in evs:
        try:
            sl = float((detail or {}).get("stop_loss") or 0)
        except (TypeError, ValueError):
            sl = 0.0
        if sl > 0:
            stop_map[tr_id] = sl

    pool = ExchangePool()
    created = 0
    theta = float(settings.REENTRY_TRIGGER_THETA_R or 0)
    now_ms = _ms(now)
    for rec in candidates:
        try:
            entry = float(rec.entry_price or 0)
            stop0 = stop_map.get(rec.id)
            if entry <= 0 or not stop0:
                continue  # 无开仓价/初始止损快照：R 空间不可算
            r_unit = abs(entry - stop0)
            if r_unit <= 0:
                continue
            closed_ms = _ms(rec.closed_at)
            # 止损根 = 包含 closed_at 的那根 1h；K 线拉到该根收盘为止
            bar_open_ms = _bar_open_ms(closed_ms)
            fetch_end_ms = bar_open_ms + BAR_MS
            if fetch_end_ms > now_ms:
                continue  # 止损根还没收盘（止损发生在当前小时），下轮再建
            kls = pool.get_klines_range(
                rec.symbol, "1h",
                REENTRY_LOOKBACK_H + settings.REENTRY_WINDOW_HOURS, fetch_end_ms,
            ) or []
            wick = compute_stop_wick(kls, rec.direction, closed_ms)
            if wick is None:
                continue  # 止损根拿不到，影线不可判
            struct_bars = [k for k in kls if int(k[0]) + BAR_MS <= fetch_end_ms]
            structure = compute_structure(struct_bars, rec.direction)
            if structure is None:
                continue
            structure["stop_wick"] = wick
            d = 1 if (rec.direction or "long") == "long" else -1
            trigger_price = round(entry + d * theta * r_unit, 8)
            expires_at = rec.closed_at.replace(tzinfo=None) + timedelta(
                hours=settings.REENTRY_WINDOW_HOURS)
            if not wick["ok"]:
                status, reason = "wick_miss", "stop_wick_weak"
            elif not structure["structure_ok"]:
                status, reason = "structure_broken", "structure_broken_at_close"
            else:
                status, reason = "watching", None
            db.add(ReentryWatch(
                source_trade_id=rec.id, symbol=rec.symbol,
                direction=rec.direction, account_key=rec.account_key,
                testnet=False, rule_version=rec.rule_version,
                entry_price=entry, stop_initial=stop0, r_unit=r_unit,
                theta_r=theta, trigger_price=trigger_price,
                status=status, structure_json=structure, expires_at=expires_at,
                resolve_reason=reason,
                resolved_at=now if status != "watching" else None,
            ))
            db.commit()
            created += 1
            if status == "wick_miss":
                logger.info("再入场监控 %s 止损根影线不足（%s%% 全幅），不提醒",
                            rec.symbol, wick["pct"] * 100)
            elif status == "structure_broken":
                logger.info("再入场监控 %s 结构已破（平仓时），不提醒", rec.symbol)
            else:
                logger.info("再入场监控 %s %s 建立（entry=%s stop=%s 触发=%s）",
                            rec.symbol, rec.direction, entry, stop0, trigger_price)
        except Exception as e:
            db.rollback()
            logger.debug("再入场监控建立失败（忽略）%s: %s",
                         getattr(rec, "symbol", "?"), e)
    return created


def _px(v) -> str:
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return "-"


def build_reentry_card(watch) -> dict:
    """触发提醒卡（header/elements，发送通道由 feishu_bot 包装）。"""
    side = "多" if watch.direction == "long" else "空"
    st = watch.structure_json or {}
    ok = lambda b: "✓" if b else "✗"  # noqa: E731
    theta = float(watch.theta_r or 0)
    trig_note = "回到开仓价" if theta == 0 else f"开仓价 ± {theta:g}R"
    closed_at = (watch.expires_at - timedelta(hours=settings.REENTRY_WINDOW_HOURS)) \
        if watch.expires_at else None
    # 库内为 naive UTC，卡片按北京时间展示（与扫描卡/持仓卡口径一致）
    closed_txt = (closed_at + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M') \
        if closed_at else '-'
    sw = st.get("stop_wick") or {}
    wick_txt = "—"
    if sw:
        mult = sw.get("mult")
        mult_txt = f"{float(mult):.2f}×实体" if mult is not None else "实体≈0"
        pct_txt = f"{float(sw.get('pct') or 0) * 100:.0f}% 全幅"
        wick_txt = f"{'下影' if watch.direction == 'long' else '上影'} {mult_txt}｜{pct_txt} {ok(sw.get('ok'))}"
    lines = [
        f"**原单**：{watch.symbol} {side}｜入场 {_px(watch.entry_price)}｜止损 {_px(watch.stop_initial)}",
        f"**触发**：{_px(watch.trigger_price)}（{trig_note}）｜1h 收盘 {_px(watch.current_price)}",
        f"**原单平仓**：{closed_txt}（北京）"
        f"｜来源单 {watch.source_trade_id}",
        f"**止损根影线**：{wick_txt}",
        f"**结构**：EMA排列 {ok(st.get('ema_ok'))}｜ADX {st.get('adx') if st.get('adx') is not None else '-'} {ok(st.get('adx_ok'))}"
        f"｜R² {st.get('r2') if st.get('r2') is not None else '-'} {ok(st.get('r2_ok'))}",
    ]
    return {
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"再入场提醒 {watch.symbol} {side}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": CARD_DISCLAIMER},
            ]},
        ],
    }


def notify_triggered(db, watch) -> bool:
    """发触发卡；成功才写 notified_at（失败保持空，下轮重试）。"""
    from app.services.notify import send_feishu_card

    try:
        sent = send_feishu_card(build_reentry_card(watch), testnet=False)
    except Exception as e:
        logger.warning("再入场提醒卡发送失败 %s: %s", watch.symbol, e)
        return False
    if not sent:
        return False
    watch.notified_at = datetime.utcnow()
    db.commit()
    return True


def sweep_reentry_watches(db, now: Optional[datetime] = None) -> dict:
    """先建监控，再巡检（每小时 :06）：触发优先 → 到期 → 已有在跑仓 → 结构破坏。

    触发口径 = **小时收盘确认**：只用已收盘 1h，多头 high≥触发价、空头
    low≤触发价（不再读 ticker 现价，避免盘中插针误报）；从 last_checked_at
    补扫到 now，窗口外（根收盘 > expires_at）的根不认。止损那根自身不参与
    触发（根内先后顺序不可知）。
    """
    from sqlalchemy import select

    from app.models.trade import ReentryWatch, TradeRecord
    from app.services.exchange_pool import ExchangePool

    now = now or datetime.utcnow()
    counts = {"registered": 0, "triggered": 0, "expired": 0,
              "broken": 0, "superseded": 0}
    try:
        counts["registered"] = register_reentry_watches(db, now=now)
    except Exception as e:
        db.rollback()
        logger.debug("再入场监控建立整轮失败（忽略）: %s", e)

    rows = db.execute(
        select(ReentryWatch).where(ReentryWatch.status.in_(("watching", "triggered")))
    ).scalars().all()
    if not rows:
        return counts

    # 在跑仓集合（同账号同 symbol）：行情巡检不顺带改仓位，只用于判定
    held: dict[str, set] = {}
    for acc_key, testnet, symbol in db.execute(
        select(TradeRecord.account_key, TradeRecord.testnet, TradeRecord.symbol).where(
            TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
            TradeRecord.testnet.is_(False),
        )
    ).all():
        held.setdefault(_acc_key(acc_key, testnet), set()).add(symbol)

    pool = ExchangePool()
    now_ms = _ms(now)
    window_h = settings.REENTRY_WINDOW_HOURS

    for w in rows:
        try:
            # 已触发但卡没发出去：只补发，不再改状态
            if w.status == "triggered":
                if w.notified_at is None and notify_triggered(db, w):
                    logger.info("再入场提醒补发成功 %s", w.symbol)
                continue

            try:
                kls = pool.get_klines(w.symbol, "1h", 400) or []
            except Exception as e:
                logger.debug("再入场巡检拉 K 线失败 %s（保持监控）: %s", w.symbol, e)
                continue
            closed = kls[:-1] if kls else []  # 丢当前形成中根，只看已收盘 1h
            if not closed:
                continue

            current_price = float(closed[-1][4])
            d = 1 if w.direction == "long" else -1
            trig = float(w.trigger_price)
            closed_at = (w.expires_at - timedelta(hours=window_h)
                         if w.expires_at is not None else None)
            stop_bar_open = (_bar_open_ms(_ms(closed_at)) if closed_at else None)
            since = w.last_checked_at or closed_at
            since_ms = _ms(since) if since else 0
            exp_ms = _ms(w.expires_at) if w.expires_at is not None else None

            hit = None
            for k in closed:
                open_ms = int(k[0])
                close_ms = open_ms + BAR_MS
                if stop_bar_open is not None and open_ms <= stop_bar_open:
                    continue  # 止损那根本身不参与触发（根内先后不可知）
                if close_ms <= since_ms or close_ms > now_ms:
                    continue
                if exp_ms is not None and close_ms > exp_ms:
                    break  # 窗口外（之后的根一律不认）
                hi, lo = float(k[2]), float(k[3])
                if (hi >= trig) if d > 0 else (lo <= trig):
                    hit = k
                    break

            prev = w.structure_json or {}
            structure = compute_structure(closed, w.direction)
            if structure is not None:
                if isinstance(prev.get("stop_wick"), dict):
                    structure["stop_wick"] = prev["stop_wick"]  # 保留止损根影线快照
                w.structure_json = structure

            w.current_price = current_price
            w.last_checked_at = now
            db.commit()

            if hit is not None:
                w.status = "triggered"
                w.triggered_at = now
                db.commit()
                counts["triggered"] += 1
                notify_triggered(db, w)
                logger.info("再入场触发 %s %s｜1h 收盘 %s（触发价 %s）",
                            w.symbol, w.direction, current_price, trig)
                continue

            if w.expires_at is not None and now >= w.expires_at:
                w.status, w.resolve_reason, w.resolved_at = "expired", "ttl", now
                db.commit()
                counts["expired"] += 1
                continue

            if w.symbol in held.get(_acc_key(w.account_key, False), set()):
                w.status, w.resolve_reason, w.resolved_at = "superseded", "open_position", now
                db.commit()
                counts["superseded"] += 1
                continue

            if structure is not None and not structure["structure_ok"]:
                w.status, w.resolve_reason, w.resolved_at = "structure_broken", "structure_break", now
                counts["broken"] += 1
                logger.info("再入场结构破坏 %s，停止监控", w.symbol)
                db.commit()
        except Exception as e:
            db.rollback()
            logger.debug("再入场巡检单条失败（忽略）%s: %s",
                         getattr(w, "symbol", "?"), e)
    return counts
