"""大盘环境/市场情绪接口（Header 大盘条数据源，docs/specs 2026-10-01）。

聚合 market_data 现有数据源（恐贪指数 TTL 30min、BTC/ETH 大盘广度 TTL 5min），
零新增抓取逻辑；子项失败/抛异常均静默降级为 null，接口恒 200 不阻塞前端。
"""
import logging

from fastapi import APIRouter

from app.services import market_data

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market"])


@router.get("/api/market/overview")
def market_overview():
    """大盘环境摘要：恐贪指数 + BTC/ETH EMA 形态与 24h 涨跌幅。

    子项任一失败返回 null（前端降级展示「大盘 --」），整体恒 200。
    """
    fear_greed = None
    try:
        fear_greed = market_data.get_fear_greed()
    except Exception as e:
        logger.debug("market_overview-恐贪指数失败: %s", e)

    breadth = None
    try:
        # 模块级懒加载，避免导入期建连（与 position_advisor 同款做法）
        from app.services.exchange_pool import ExchangePool

        breadth = market_data.get_market_breadth(ExchangePool())
    except Exception as e:
        logger.debug("market_overview-大盘广度失败: %s", e)

    return {"fear_greed": fear_greed, "breadth": breadth}
