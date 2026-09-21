"""飞书通知统一入口（自建应用机器人）

用途：业务日报、持仓建议/平仓结果的文本保底等。2026-09-22 起全部走
自建应用（feishu_bot，长连接回调通道），webhook 自定义机器人已弃用。
未配置自建应用三件套时静默跳过（返回 False），不抛错影响主流程。
"""
import logging

logger = logging.getLogger(__name__)


def send_feishu_card(card: dict) -> bool:
    """发消息卡片（interactive）。card 为卡片体（header/elements），
    外层包装与发送通道由 feishu_bot 负责；未配置返回 False。"""
    from app.services.feishu_bot import send_app_card
    return send_app_card(card)


def send_feishu_text(text: str) -> bool:
    """发纯文本消息；未配置返回 False。"""
    from app.services.feishu_bot import send_app_text
    return send_app_text(text)
