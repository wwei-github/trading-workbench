"""飞书自建应用机器人（AI 持仓建议交互卡片，2026-09-21）。

webhook 自定义机器人收不到卡片按钮点击，故用自建应用 + 长连接（WebSocket，
无需公网回调）。backend lifespan 起守护线程。

交互流：点击「市价平仓」→ 长连接收到 card.action.trigger → 校验令牌 →
秒回 toast「指令已接收」（回调须快速返回）→ 后台线程执行平仓 →
会话内发送**新的**结果卡片（不更新原卡片）。

配置：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_TARGET_ID（群 oc_ 或个人
ou_，按前缀自动识别发送方式）+ FEISHU_ACTION_TOKEN（按钮 value 校验）；
未配置时通知退回 webhook 纯展示。
"""
import json
import logging
import threading
import time
from uuid import uuid4

import httpx
from redis import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_OPEN = "https://open.feishu.cn/open-apis"
_token_cache: dict = {"token": "", "expire_at": 0.0}


def configured() -> bool:
    """自建应用三件套是否齐全"""
    return bool(
        settings.FEISHU_APP_ID and settings.FEISHU_APP_SECRET and settings.FEISHU_TARGET_ID
    )


def _tenant_token() -> str:
    """tenant_access_token（内置缓存，提前 60s 过期）"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] > now:
        return _token_cache["token"]
    r = httpx.post(f"{_OPEN}/auth/v3/tenant_access_token/internal", json={
        "app_id": settings.FEISHU_APP_ID,
        "app_secret": settings.FEISHU_APP_SECRET,
    }, timeout=10)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {str(data)[:200]}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + int(data.get("expire", 7200)) - 60
    return _token_cache["token"]


def _post_message(msg_type: str, content: str) -> bool:
    """统一发消息（interactive/text）到目标会话；失败仅记日志不抛错"""
    if not configured():
        return False
    rid_type = "chat_id" if settings.FEISHU_TARGET_ID.startswith("oc_") else "open_id"
    try:
        r = httpx.post(
            f"{_OPEN}/im/v1/messages",
            params={"receive_id_type": rid_type},
            headers={"Authorization": f"Bearer {_tenant_token()}"},
            json={
                "receive_id": settings.FEISHU_TARGET_ID,
                "msg_type": msg_type,
                "content": content,
            },
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            return True
        logger.warning("飞书应用消息发送失败: %s", str(data)[:300])
        return False
    except Exception as e:
        logger.warning("飞书应用消息发送异常: %s", e)
        return False


def send_app_card(card: dict) -> bool:
    """以自建应用身份发 interactive 卡片（webhook 发的卡片收不到回调）。

    目标按前缀自动识别：oc_ → 群（chat_id），ou_ → 个人单聊（open_id）
    """
    return _post_message("interactive", json.dumps(card, ensure_ascii=False))


def send_app_text(text: str) -> bool:
    """以自建应用身份发纯文本（降级保底用）"""
    return _post_message("text", json.dumps({"text": text}, ensure_ascii=False))


def start_card_listener() -> None:
    """启动长连接守护线程（backend lifespan 调用；未配置/缺 SDK 静默跳过）"""
    if not configured():
        logger.info("飞书自建应用未配置，卡片回调长连接不启动")
        return
    threading.Thread(target=_run_ws, name="feishu-card-ws", daemon=True).start()


def _run_ws() -> None:
    """长连接主循环：client.start() 在连接断开（网络抖动/被同凭证新连接顶掉）时
    会静默返回而非自动重连，故外层加重连循环；WARNING 级日志落 app_logs 可观测"""
    import time as _time

    try:
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(_on_card_action)
            .build()
        )
        while True:
            client = lark.ws.Client(
                settings.FEISHU_APP_ID, settings.FEISHU_APP_SECRET,
                event_handler=handler, log_level=lark.LogLevel.WARNING,
            )
            logger.warning("飞书卡片回调长连接连接中（app_id=%s…）", settings.FEISHU_APP_ID[:8])
            client.start()  # 阻塞；连接断开时返回
            logger.warning("飞书卡片长连接断开，30s 后重连")  # 落 app_logs，前端系统日志可见
            _time.sleep(30)
    except Exception:
        logger.exception("飞书卡片回调长连接异常退出（5 分钟后重试）")
        threading.Timer(300, _run_ws, daemon=True).start()


def _toast(content: str, toast_type: str = "success"):
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )
    resp = P2CardActionTriggerResponse()
    resp.toast = CallBackToast()
    resp.toast.type = toast_type
    resp.toast.content = content
    return resp


def _on_card_action(data):
    """卡片按钮回调：校验令牌 → 后台线程执行 → toast 即时反馈

    act 分支：close_market（合约平仓）/ spot_buy、spot_sell（现货屯币一键确认，
    docs/18 P1 挂载点式扩展，合约逻辑零改动）。
    """
    try:
        value = dict((getattr(data.event, "action", None) and data.event.action.value) or {})
        act = value.get("act")
        if act == "close_market":
            if not settings.FEISHU_ACTION_TOKEN or value.get("token") != settings.FEISHU_ACTION_TOKEN:
                return _toast("无效操作令牌", "error")
            trade_id = str(value.get("trade_id") or "")
            if not trade_id:
                return _toast("缺少交易 ID", "error")
            threading.Thread(target=_exec_close, args=(trade_id,), daemon=True).start()
            return _toast("指令已接收，平仓执行中…")
        if act in ("spot_buy", "spot_sell"):
            if not settings.FEISHU_ACTION_TOKEN or value.get("token") != settings.FEISHU_ACTION_TOKEN:
                return _toast("无效操作令牌", "error")
            if not str(value.get("symbol") or ""):
                return _toast("缺少币种", "error")
            threading.Thread(target=_exec_spot_confirm, args=(act, dict(value)), daemon=True).start()
            return _toast("指令已接收，现货下单执行中…")
        return _toast("未知操作", "error")
    except Exception:
        logger.exception("卡片回调处理失败")
        return _toast("处理异常，请查看系统日志", "error")


def _exec_spot_confirm(act: str, value: dict) -> None:
    """后台执行现货一键确认下单 → 会话内发结果卡片（新消息）。

    docs/18 P1 半自动闭环：日检卡片按钮 → exec_spot_order 统一风控与锁，
    trigger_type=signal（人工确认的信号单，受单币每日一次冷却，重复点击被拒）。
    BUY 带金额（push 时评估价，按金额买价格无关）；SELL 只带 lots，
    执行时按现价折算并裁剪到波段仓 30% 上限。
    """
    from app.database import SessionLocal
    from app.services.spot_exec import exec_spot_order

    symbol = str(value.get("symbol") or "")
    side = "BUY" if act == "spot_buy" else "SELL"
    try:
        lots = int(str(value.get("lots") or "")) or None
    except ValueError:
        lots = None
    try:
        quote = float(str(value.get("quote") or "")) or None
    except ValueError:
        quote = None

    db = SessionLocal()
    try:
        out = exec_spot_order(
            db, symbol, side, quote_amount=quote, lots=lots,
            trigger_type="signal",
            trigger_detail={"source": "feishu_card"},
        )
    finally:
        db.close()

    if out["ok"]:
        qty = float(out.get("executed_qty") or 0)
        spent = float(out.get("quote_spent") or 0)
        avg = out.get("avg_price")
        env = "测试网" if out.get("testnet") else "正式网"
        body = (
            f"买入金额 **{spent:,.2f} U** → 成交 **{qty:.8f}**"
            + (f"（均价 {avg:,.2f}）" if avg else "")
            if side == "BUY"
            else f"卖出数量 **{qty:.8f}**" + (f"（均价 {avg:,.2f}）" if avg else "")
            + f" · 回款 {spent:,.2f} U"
        )
        for note in out.get("risk_notes") or []:
            body += f"\n⚠️ {note}"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"✅ 现货买入成功 · {symbol}" if side == "BUY" else f"✅ 现货卖出成功 · {symbol}"},
                "template": "green" if side == "BUY" else "orange",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": f"一键确认 · {env} · 详情见现货订单记录"}]},
            ],
        }
    else:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 现货下单未执行"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": str(out.get("reason") or "未知错误")}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": "风控拒绝或下单失败，可在工作台「现货 → 持仓总览」手动重试"}]},
            ],
        }
    try:
        send_app_card(card)
    except Exception:
        logger.exception("现货确认结果卡片发送失败")


def _exec_close(trade_id: str) -> None:
    """后台执行平仓（开仓锁互斥 + 锁内重读防竞态）→ 会话内发结果卡片（新消息）"""
    from app.database import SessionLocal
    from app.models.trade import TradeRecord
    from app.services.binance_trader import BinanceTrader
    from app.services.trade_engine import close_market_now
    from app.tasks.trade_tasks import _acquire_open_lock, _OPEN_LOCK_TTL_S, _release_open_lock

    res = {"ok": False, "error": "未知错误"}
    db = SessionLocal()
    try:
        rec = db.get(TradeRecord, trade_id)
        if not rec:
            res = {"ok": False, "error": "交易记录不存在"}
        elif rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
            res = {"ok": False, "error": f"该交易已不在跑（{rec.status}），无需平仓"}
        else:
            # docs/17：按开单账号结算；account_key 缺失的旧行回落 testnet 标记
            trader = BinanceTrader(rec.account_key or bool(rec.testnet))
            if not trader.configured:
                res = {"ok": False, "error": "币安 API 凭据未配置，无法执行"}
            else:
                r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
                ident = uuid4().hex
                # 账号口径锁（docs/17 P1）：与该账号的结算巡检/开仓互斥
                lock_acc = rec.account_key or ("testnet-1" if rec.testnet else "live-1")
                if not _acquire_open_lock(r, ident, wait_s=20, ttl_s=_OPEN_LOCK_TTL_S,
                                          account=lock_acc):
                    res = {"ok": False, "error": "系统忙（巡检/开仓中），请稍后重试"}
                else:
                    try:
                        db.refresh(rec)  # 锁内重读：巡检可能刚好把单子平掉了
                        if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
                            res = {"ok": False, "error": f"该交易刚被结算平仓（{rec.status}）"}
                        else:
                            out = close_market_now(db, trader, rec)
                            res = {"ok": True, "symbol": rec.symbol,
                                   "pnl": float(out["pnl"]), "pnl_pct": out.get("pnl_pct"),
                                   "testnet": bool(rec.testnet)}
                    except Exception as e:
                        db.rollback()
                        logger.exception("卡片平仓失败 %s", trade_id)
                        res = {"ok": False, "error": f"平仓失败：{str(e)[:80]}"}
                    finally:
                        _release_open_lock(r, ident, account=lock_acc)
    finally:
        db.close()

    if res["ok"]:
        pnl, pct = res["pnl"], res.get("pnl_pct")
        color = "green" if pnl > 0 else ("red" if pnl < 0 else "default")
        env = "测试网" if res.get("testnet") else "正式网"

        def _col(title: str, value: str) -> dict:
            return {"tag": "column", "width": "weighted", "weight": 1,
                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**{title}**\n{value}"}}]}

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 市价平仓成功 · {res['symbol']}"},
                "template": "green" if pnl >= 0 else "red",
            },
            "elements": [
                {"tag": "column_set", "flex_mode": "stretch", "columns": [
                    _col("净盈亏", f"<font color='{color}'>{pnl:+.2f} USDT</font>"),
                    _col("收益率", f"{pct}%" if pct is not None else "—"),
                    _col("环境", env),
                ]},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": "手动平仓 · 费用已计入净额 · 详情见交易记录"}]},
            ],
        }
    else:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 市价平仓失败"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": str(res.get("error") or "未知错误")}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                 "content": "请到交易记录页手动处理或稍后重试"}]},
            ],
        }
    send_app_card(card)  # 失败已在 _post_message 记 WARNING 落 app_logs
