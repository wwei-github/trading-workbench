"""共享静态评分门槛与账号级连损提升档计算。

连损动态档只可在执行层按账号使用。批量 AI 分析中的跨账号最大连损
不能作为所有账号的统一门槛。
"""

from app.config import settings


def derate_threshold(consecutive_losses: int, enabled: bool | None = None) -> int:
    """当日连损对应的提升门槛（≥3 笔→85，2 笔→75）；0 表示不降速。

    enabled=None 使用执行层开关；提示词说明可传 True 仅标注潜在账号约束。
    """
    if enabled is False or (enabled is None and not settings.CONSECUTIVE_LOSS_DERATE):
        return 0
    if consecutive_losses >= 3:
        return 85
    if consecutive_losses == 2:
        return 75
    return 0


def static_min_recommendation(cfg=None) -> float:
    """静态门槛：system_config.min_recommendation 优先，回落 env 默认。"""
    if cfg is not None and getattr(cfg, "min_recommendation", None) is not None:
        return float(cfg.min_recommendation)
    return float(settings.TRADING_MIN_RECOMMENDATION)


def effective_min_recommendation(cfg=None, consecutive_losses: int = 0) -> float:
    """执行单账号的最终门槛 = max(静态门槛, 该账号连损提升档)。"""
    return max(
        static_min_recommendation(cfg),
        float(derate_threshold(consecutive_losses)),
    )
