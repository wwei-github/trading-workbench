"""内置策略 golden_k_ai（docs/20 S0/S1）：现有「金K候选 → AI 三价」管线占位。

零行为变化铁律：本插件不包裹、不改写任何现有扫描/AI/开单代码——
现有管线继续原样运行（scanner.py/ai_tasks/trade_engine），仅在注册表
占位归因（TradeRecord.strategy_id 默认值即本 id）。exit_spec 为现行
生产出场语义的声明式快照，S3 才被引擎消费。
"""


class GoldenKAIStrategy:
    """内置策略：信号生产走现有原生管线，不走协议方法。"""

    id = "golden_k_ai"
    display_name = "金K + AI（内置）"

    def scan_candidates(self, universe: list[str]) -> list[str]:  # pragma: no cover
        raise NotImplementedError("golden_k_ai 信号生产走原生管线（scanner），不走插件协议")

    def build_signal(self, symbol: str):  # pragma: no cover
        raise NotImplementedError("golden_k_ai 信号生产走原生管线（scanner），不走插件协议")

    def exit_spec(self) -> dict:
        """现行生产出场（声明式快照，与 trade_engine/backtest_engine 基线一致）：
        TP 分批比例页面可配（默认 60/20/80）+ TP1 成交武装保本 + 摆动3/ATR_k 跟进。"""
        return {
            "tp_ladder": "configurable",   # tp1_qty_pct_with_tp2 / tp2_qty_pct / tp1_qty_pct_no_tp2
            "be_arm": "after_tp1",
            "trail": "swing3+atr_k",
            "sig_exit": None,
        }
