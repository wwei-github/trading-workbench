"""策略插件协议（docs/20 §三）：StrategySignal + StrategyPlugin。

Candidate 第一版 = symbol 字符串（rule of two：两个策略的候选都以币为粒度；
出现第三形态再泛化）。StrategySignal 与 AIAnalysis 三价对齐——
trade_engine 未来消费的唯一信号形态；S1 影子期仅落库不进执行。
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class StrategySignal:
    """策略信号（与现有 AIAnalysis 三价语义对齐）。"""

    strategy_id: str
    symbol: str
    direction: str              # long / short
    entry: float
    stop: float
    tp1: float                  # 0 = 无
    tp2: float                  # 0 = 无
    reason: str
    confidence: float           # 策略自评 0~1
    signal_type: Optional[str] = None   # 策略内信号分类（pullback / cross…）
    raw: dict = field(default_factory=dict)  # 策略私有证据快照（落库归因用）


@runtime_checkable
class StrategyPlugin(Protocol):
    """策略插件协议。实现方为纯计算体：不碰 DB、不下单、不读全局配置——
    参数由 registry 注入，行情由调用方传入（便于测试与影子/实盘同代码）。"""

    id: str
    display_name: str

    def scan_candidates(self, universe: list[str]) -> list[str]:
        """筛币 + 预筛（策略私有过滤），返回值得逐个 build_signal 的 symbol。"""
        ...

    def build_signal(self, symbol: str) -> Optional[StrategySignal]:
        """对单个候选产出信号（None = 不触发）。"""
        ...

    def exit_spec(self) -> dict:
        """出场语义声明（docs/20 §六）：骨架不可变，参数按策略解析。
        S3 前仅作声明与回测用，不影响生产出场。"""
        ...
