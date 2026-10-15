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

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_OPEN = "https://open.feishu.cn/open-apis"
_token_cache: dict = {"token": "", "expire_at": 0.0}


def configured() -> bool:
    """自建应用三件套是否齐全"""
    return bool(
        settings.FEISHU_APP_ID and settings.FEISHU_APP_SECRET and settings.FEISHU_TARGET_ID
    )


def testnet_notify_enabled() -> bool:
    """测试网飞书通知开关（system_config.feishu_testnet_notify_enabled，2026-10-15）。

    发送层在 testnet 标注的推送上调用；开关关闭时静默丢弃该推送。缺行/读库
    异常按「开」兜底——不因配置读取失败静默丢通知（每个推送现场读 DB，
    页面改保存即生效）。
    """
    from app.database import SessionLocal
    from app.models.system_config import SystemConfig

    db = None
    try:
        db = SessionLocal()
        cfg = db.get(SystemConfig, 1)
        if cfg is None:
            return True
        return bool(cfg.feishu_testnet_notify_enabled)
    except Exception:
        logger.warning("读取测试网飞书通知开关失败，按「开」处理", exc_info=True)
        return True
    finally:
        if db is not None:
            db.close()


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


def send_app_card(card: dict, *, testnet: bool = False) -> bool:
    """以自建应用身份发 interactive 卡片（webhook 发的卡片收不到回调）。

    目标按前缀自动识别：oc_ → 群（chat_id），ou_ → 个人单聊（open_id）

    testnet=True 表示这张卡来自「合约测试网单据」相关推送：系统配置的测试网
    飞书通知开关关闭时静默丢弃（返回 False，不发、不重试）。
    """
    if testnet and not testnet_notify_enabled():
        logger.info("测试网飞书通知已关闭，跳过卡片推送")
        return False
    return _post_message("interactive", json.dumps(card, ensure_ascii=False))


def send_app_text(text: str, *, testnet: bool = False) -> bool:
    """以自建应用身份发纯文本（降级保底用）。testnet 语义同 send_app_card。"""
    if testnet and not testnet_notify_enabled():
        logger.info("测试网飞书通知已关闭，跳过文本推送")
        return False
    return _post_message("text", json.dumps({"text": text}, ensure_ascii=False))


def upload_image(image: bytes) -> str:
    """上传图片到飞书 → image_key（卡片 img 元素引用；2026-10-15）。

    卡片内嵌图片必须两步走：先 im/v1/images 上传拿 image_key，再随卡片发。
    image_type=message 为「用于发送消息」的消息图片（≤10MB，png/jpg 等）。
    失败返回 ""（仅记日志），调用方降级为纯文字卡片。
    部署前置：飞书后台需给自建应用开「上传图片」权限（im:resource 或
    「上传文件 V2」任一），否则上传返回权限错误。
    """
    if not configured() or not image:
        return ""
    try:
        r = httpx.post(
            f"{_OPEN}/im/v1/images",
            headers={"Authorization": f"Bearer {_tenant_token()}"},
            files={"image": ("chart.png", image, "image/png")},
            data={"image_type": "message"},
            timeout=20,
        )
        data = r.json()
        if data.get("code") == 0:
            return str((data.get("data") or {}).get("image_key") or "")
        logger.warning("飞书图片上传失败: %s", str(data)[:300])
    except Exception as e:
        logger.warning("飞书图片上传异常: %s", e)
    return ""


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
    docs/18 P1 挂载点式扩展，合约逻辑零改动）/ open_live（正式网未开单提醒
    卡片「开单」按钮，2026-10-15）。
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
        if act == "open_live":
            if not settings.FEISHU_ACTION_TOKEN or value.get("token") != settings.FEISHU_ACTION_TOKEN:
                return _toast("无效操作令牌", "error")
            analysis_id = str(value.get("analysis_id") or "")
            account_key = str(value.get("account_key") or "")
            if not analysis_id or not account_key:
                return _toast("缺少开单参数", "error")
            threading.Thread(target=_exec_open_live, args=(analysis_id, account_key),
                             daemon=True).start()
            return _toast("指令已接收，正式网开单执行中…")
        return _toast("未知操作", "error")
    except Exception:
        logger.exception("卡片回调处理失败")
        return _toast("处理异常，请查看系统日志", "error")


def _exec_open_live(analysis_id: str, account_key: str) -> None:
    """后台执行正式网手动开单 → 会话内发结果卡片（新消息）。

    2026-10-15：正式网自动开单关闭提醒卡片「开单」按钮的回调。执行核心与网页
    扫描结果表「开单」按钮同源（trade_tasks.manual_open_from_analysis：账号口径
    开仓锁 + force_market 市价闸门），本函数只负责结果呈现。重复点击被
    try_open_for_analysis 的「同分析同账号已开仓」去重闸门拦住，不会二次下单；
    按钮只在正式网仅一个启用账号时渲染（send 侧控制），回调再按注册表复验账号。
    """
    from app.services.trade_engine import _fmt_price
    from app.tasks.trade_tasks import manual_open_from_analysis

    res = manual_open_from_analysis(analysis_id, account_key)

    def _col(title: str, value: str) -> dict:
        return {"tag": "column", "width": "weighted", "weight": 1,
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"**{title}**\n{value}"}}]}

    def _p(v) -> str:
        return _fmt_price(float(v)) if v else "—"

    symbol = str(res.get("symbol") or "")
    if res.get("ok") and res.get("opened"):
        is_long = res.get("direction") == "long"
        qty_line = f"数量 {float(res['qty']):g} · " if res.get("qty") else ""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 正式网手动开单 · {symbol}"},
                "template": "green",
            },
            "elements": [
                {"tag": "column_set", "flex_mode": "stretch", "columns": [
                    _col("方向", "做多 Long" if is_long else "做空 Short"),
                    _col("账号", str(res.get("account_key") or "—")),
                    _col("入场价", _p(res.get("entry_price"))),
                ]},
                {"tag": "column_set", "flex_mode": "stretch", "columns": [
                    _col("止损 SL", _p(res.get("stop_loss"))),
                    _col("止盈 TP1", _p(res.get("take_profit_1"))),
                    _col("止盈 TP2", _p(res.get("take_profit_2"))),
                ]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                    qty_line + "飞书卡片开单（市价即时入场）· SL/TP 条件单已挂 · "
                    "详情见交易记录")}]},
            ],
        }
    elif res.get("ok"):
        # 2026-10-15 措辞修正：opened=False 但反查到交易记录 ⇒ 命中的是「同分析同
        # 账号已开过」去重闸门（该闸门不落 block_reason）——此时币种其实已有单，
        # 「未开单」是误述，改为「未重复开单」并点明因由
        if res.get("trade_id"):
            title, tpl = f"ℹ️ 未重复开单 · {symbol}", "blue"
            reason = (f"该分析在账号 **{res.get('account_key') or '—'}** 已有成交"
                      "（同分析同账号不重复开单），本次点击未产生新单")
        else:
            title, tpl = f"⚠️ 未开单 · {symbol}", "orange"
            reason = str(res.get("block_reason")
                         or "开仓闸门未过（未落具体原因，见系统日志）")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": tpl,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": reason}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                    "已按现价走完整开仓闸门（市价入场口径），本次未成交；"
                    "可在工作台「扫描结果」手动重试")}]},
            ],
        }
    else:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 开单未执行"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": str(res.get("error") or "未知错误")}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                    "校验/锁/下单执行失败，请查看系统日志；可在工作台手动重试")}]},
            ],
        }
    try:
        send_app_card(card)
    except Exception:
        logger.exception("手动开单结果卡片发送失败")


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
    """后台执行平仓（共享核心 manual_close_market：校验+账号锁互斥+锁内重读）
    → 会话内发结果卡片（新消息）。执行核心与网页交易记录「市价平仓」按钮同源
    （2026-09-25 抽取），本函数只负责结果卡片呈现。"""
    from app.tasks.trade_tasks import manual_close_market

    res = manual_close_market(trade_id)

    if res["ok"]:
        pnl, pct = res["pnl"], res.get("pnl_pct")
        color = "green" if pnl > 0 else ("red" if pnl < 0 else "default")

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
