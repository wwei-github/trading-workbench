"""飞书群通知（自定义机器人 Webhook）

用途：CICD 部署通知（GitHub Actions 直发）+ 业务日报（notify_daily_report 任务）。
Webhook 走环境变量 FEISHU_NOTIFY_WEBHOOK（与 CICD 用的 GitHub Secret 同源配置，
后端独立读 env，避免和流水线耦合）。未配置时静默跳过，不抛错影响主流程。
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def send_feishu_text(text: str) -> bool:
    """发文本消息到飞书群。返回是否成功；未配置/失败只记日志不抛异常。"""
    webhook = settings.FEISHU_NOTIFY_WEBHOOK
    if not webhook:
        logger.info("FEISHU_NOTIFY_WEBHOOK 未配置，跳过飞书通知")
        return False
    try:
        resp = httpx.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            return True
        logger.warning("飞书通知返回异常: %s", str(data)[:200])
        return False
    except Exception as e:
        logger.warning("飞书通知发送失败: %s", e)
        return False


def send_feishu_card(card: dict) -> bool:
    """发消息卡片（interactive）到飞书群。card 为卡片体（header/elements），
    外层 msg_type 包装在本函数内。列对齐由卡片布局引擎负责，不受聊天比例字体影响。"""
    webhook = settings.FEISHU_NOTIFY_WEBHOOK
    if not webhook:
        logger.info("FEISHU_NOTIFY_WEBHOOK 未配置，跳过飞书通知")
        return False
    try:
        resp = httpx.post(
            webhook,
            json={"msg_type": "interactive", "card": card},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            return True
        logger.warning("飞书卡片返回异常: %s", str(data)[:300])
        return False
    except Exception as e:
        logger.warning("飞书卡片发送失败: %s", e)
        return False
