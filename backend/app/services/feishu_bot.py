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


def send_app_card(card: dict) -> bool:
    """以自建应用身份发 interactive 卡片（webhook 发的卡片收不到回调）。

    目标按前缀自动识别：oc_ → 群（chat_id），ou_ → 个人单聊（open_id）
    """
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
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            return True
        logger.warning("飞书应用卡片发送失败: %s", str(data)[:300])
        return False
    except Exception as e:
        logger.warning("飞书应用卡片发送异常: %s", e)
        return False


def start_card_listener() -> None:
    """启动长连接守护线程（backend lifespan 调用；未配置/缺 SDK 静默跳过）"""
    if not configured():
        logger.info("飞书自建应用未配置，卡片回调长连接不启动")
        return
    threading.Thread(target=_run_ws, name="feishu-card-ws", daemon=True).start()


def _run_ws() -> None:
    try:
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(_on_card_action)
            .build()
        )
        client = lark.ws.Client(
            settings.FEISHU_APP_ID, settings.FEISHU_APP_SECRET,
            event_handler=handler, log_level=lark.LogLevel.WARNING,
        )
        logger.info("飞书卡片回调长连接已启动（app_id=%s…）", settings.FEISHU_APP_ID[:8])
        client.start()  # 阻塞，内置自动重连
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
    """卡片按钮回调：校验令牌 → 后台线程执行 → toast 即时反馈"""
    try:
        value = dict((getattr(data.event, "action", None) and data.event.action.value) or {})
        if value.get("act") != "close_market":
            return _toast("未知操作", "error")
        if not settings.FEISHU_ACTION_TOKEN or value.get("token") != settings.FEISHU_ACTION_TOKEN:
            return _toast("无效操作令牌", "error")
        trade_id = str(value.get("trade_id") or "")
        if not trade_id:
            return _toast("缺少交易 ID", "error")
        threading.Thread(target=_exec_close, args=(trade_id,), daemon=True).start()
        return _toast("指令已接收，平仓执行中…")
    except Exception:
        logger.exception("卡片回调处理失败")
        return _toast("处理异常，请查看系统日志", "error")


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
            trader = BinanceTrader(bool(rec.testnet))
            if not trader.configured:
                res = {"ok": False, "error": "币安 API 凭据未配置，无法执行"}
            else:
                r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
                ident = uuid4().hex
                if not _acquire_open_lock(r, ident, wait_s=20, ttl_s=_OPEN_LOCK_TTL_S):
                    res = {"ok": False, "error": "系统忙（巡检/开仓中），请稍后重试"}
                else:
                    try:
                        db.refresh(rec)  # 锁内重读：巡检可能刚好把单子平掉了
                        if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
                            res = {"ok": False, "error": f"该交易刚被结算平仓（{rec.status}）"}
                        else:
                            out = close_market_now(db, trader, rec)
                            res = {"ok": True, "symbol": rec.symbol,
                                   "pnl": float(out["pnl"]), "pnl_pct": out.get("pnl_pct")}
                    except Exception as e:
                        db.rollback()
                        logger.exception("卡片平仓失败 %s", trade_id)
                        res = {"ok": False, "error": f"平仓失败：{str(e)[:80]}"}
                    finally:
                        _release_open_lock(r, ident)
    finally:
        db.close()

    if res["ok"]:
        pnl, pct = res["pnl"], res.get("pnl_pct")
        color = "green" if pnl > 0 else ("red" if pnl < 0 else "default")
        pct_txt = f"（{pct}%）" if pct is not None else ""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 市价平仓成功 · {res['symbol']}"},
                "template": "green" if pnl >= 0 else "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content":
                 f"**净盈亏**　<font color='{color}'>{pnl:+.2f} USDT</font>{pct_txt}"}},
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
    if not send_app_card(card):
        try:  # 应用发送失败回退 webhook 纯文本保底
            from app.services.notify import send_feishu_text
            send_feishu_text(
                f"{'✅ 平仓成功' if res['ok'] else '❌ 平仓失败'}\n"
                f"{res.get('symbol', '')} {res.get('pnl', res.get('error', ''))}"
            )
        except Exception:
            pass
