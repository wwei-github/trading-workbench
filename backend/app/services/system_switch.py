"""系统总开关（system_config.system_enabled）：关闭后停止「币种筛选后的 AI 分析」。

语义（2026-09-20 用户拍板，v2）：关闭 = 定时扫描照常跑（K线/指标筛选照常），
但筛选后的批量 AI 分析跳过（省 AI 配额），开单随之停止（分析不产出，分发不发生）；
结算巡检、复盘、归因、补丁、日报等其他定时任务照旧执行。
手动触发（手动扫描/手动单币分析/即时开仓）不受限。
任务级读 DB，页面改完下一轮 beat 即生效，无需重启。
拦截点：ai_tasks.run_ai_analysis_task（scan_type=scheduled 的批量链路）。
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
