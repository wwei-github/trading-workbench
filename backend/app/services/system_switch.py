"""系统总开关（system_config.system_enabled）：关闭后所有定时任务入口直接跳过。

与 trading_enabled（只拦开新单）不同：本开关是整站调度闸门——扫描、结算巡检、
复盘/归因/补丁、日报全部不执行（注意：结算巡检也停，在跑单的止损补挂随之暂停，
仅用于维护窗口）。任务级读 DB，页面改完下一轮 beat 即生效，无需重启。
手动触发（手动扫描/手动 AI 分析/手动开仓）不受此开关限制。
"""
import logging

from app.database import SessionLocal
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)


def system_enabled() -> bool:
    """读总开关；查询异常时放行（DB 不可用时任务本身也跑不起来，不误伤手动场景）"""
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, 1)
        return bool(cfg.system_enabled) if cfg else True
    except Exception:
        logger.exception("读取系统总开关失败，按开启处理")
        return True
    finally:
        db.close()
