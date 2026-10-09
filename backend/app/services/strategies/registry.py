"""策略注册表（docs/20 §七）：system_config.strategy_registry JSON 的解析与注入。

- NULL / 解析失败 → 仅内置 golden_k_ai（现状，零行为变化）；
- 未知 id 忽略并告警（防前端误配撑爆注册表）；
- 数值越界 clamp：max_positions 1~10、risk_weight 0.1~1.0；
- golden_k_ai 恒存在且不可禁用（原生管线无开关语义——禁用它等于停摆系统）。
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

GOLDEN_K_ID = "golden_k_ai"
KNOWN_STRATEGIES = {GOLDEN_K_ID, "ema_trend"}

# 未知键拒绝的合法键集（docs/20 rule of two）
_ENTRY_KEYS = {"id", "enabled", "max_positions", "risk_weight", "config"}


@dataclass
class PluginEntry:
    """注册表条目：策略开关 + 预算（全局账户预算的子集划分）+ 私有参数。"""

    id: str
    enabled: bool = True
    max_positions: int = 3
    risk_weight: float = 1.0
    config: dict = field(default_factory=dict)


def load_registry(raw: Optional[list]) -> list[PluginEntry]:
    """解析 strategy_registry JSON → 条目列表。恒含 golden_k_ai（首位）。"""
    entries: dict[str, PluginEntry] = {
        GOLDEN_K_ID: PluginEntry(id=GOLDEN_K_ID, enabled=True, max_positions=10),
    }
    for item in (raw or []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        sid = str(item["id"])
        if sid not in KNOWN_STRATEGIES or sid == GOLDEN_K_ID:
            logger.warning("strategy_registry 忽略非法/内置条目: %r", sid)
            continue
        unknown = set(item) - _ENTRY_KEYS
        if unknown:
            logger.warning("strategy_registry 条目 %s 未知键被拒: %s", sid, unknown)
        try:
            mp = int(item.get("max_positions", 1))
        except (TypeError, ValueError):
            mp = 1
        try:
            rw = float(item.get("risk_weight", 1.0))
        except (TypeError, ValueError):
            rw = 1.0
        cfg = item.get("config")
        entries[sid] = PluginEntry(
            id=sid,
            enabled=bool(item.get("enabled", False)),
            max_positions=max(1, min(10, mp)),
            risk_weight=max(0.1, min(1.0, rw)) if rw > 0 else 0.1,
            config=cfg if isinstance(cfg, dict) else {},
        )
    return list(entries.values())


def enabled_plugin_entries(raw: Optional[list]) -> list[PluginEntry]:
    """enabled 策略条目（golden_k_ai 恒在，供调度方与原生管线并列遍历）。"""
    return [e for e in load_registry(raw) if e.enabled]


def get_strategy(sid: str, raw: Optional[list] = None):
    """按 id 实例化策略插件（golden_k_ai 返回占位对象，其协议方法不可调用）。"""
    if sid == GOLDEN_K_ID:
        from app.services.strategies.golden_k_ai import GoldenKAIStrategy
        return GoldenKAIStrategy()
    if sid == "ema_trend":
        entry = next((e for e in load_registry(raw) if e.id == sid), None)
        from app.services.strategies.ema_trend import EmaTrendStrategy
        return EmaTrendStrategy(entry.config if entry else None)
    raise KeyError(f"未知策略: {sid}")
