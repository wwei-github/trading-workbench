"""多策略可插拔架构（docs/20）——策略插件包。

分层：base.py 协议（StrategySignal + StrategyPlugin）→ 各策略子模块
（golden_k_ai 内置管线占位 / ema_trend 规则插件）→ registry 注册表。

铁律（docs/20 §1.2）：
- 策略 = 信号生产者，系统 = 信号消费者；策略之间完全不可见；
- 策略只产 StrategySignal，风控/执行/出场骨架/复盘由底座统一承载；
- rule of two：协议字段必须两个策略都用得上才准加。
"""
from app.services.strategies.base import StrategyPlugin, StrategySignal
from app.services.strategies.registry import (
    KNOWN_STRATEGIES,
    PluginEntry,
    enabled_plugin_entries,
    get_strategy,
    load_registry,
)

__all__ = [
    "StrategyPlugin",
    "StrategySignal",
    "KNOWN_STRATEGIES",
    "PluginEntry",
    "load_registry",
    "enabled_plugin_entries",
    "get_strategy",
]
