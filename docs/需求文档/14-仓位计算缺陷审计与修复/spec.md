# 仓位计算缺陷审计报告（2026-10-27）

> 范围：`backend/app/services/trade_engine.py` 仓位链路（`try_open_for_analysis` → `_attempt_open` → `materialize_pending_entry` / `adopt_position`），并联校 `risk_budget.py`、`risk_guard.py`、`binance_trader.py`、`config.py`。
> 性质：只读审计结论；**F1/F2/F3（P1）已于 2026-10-27 获授权修复**，实施记录见 [tasks.md](tasks.md)。F4-F7 仍为候选，未授权不改。
> 模型基线：固定分数风险 `qty × |入场价 − 止损价| = 风险预算`。

## 一、结论

确认 7 项缺陷：P1×3、P2×2、P3×2。P1 三项都会让“单笔亏损 = 风险预算”的设计前提失效。

| ID | 级别 | 位置 | 缺陷 | 影响 |
| --- | --- | --- | --- | --- |
| F1 | P1 | `trade_engine.py` L679 / L840 / L1013 | 预算只覆盖价格风险，未计双边 taker 费与止损滑点；成交后超限仅告警 | 0.5% 止损实际亏损 ≈1.20× 预算；叠加 0.3% 滑点 ≈1.80× |
| F2 | P1 | `trade_engine.py` L75-L84 | 风险基数下限恒为 100，钱包 <100 时基数大于本金 | 50 U 账户单笔 6% 权益、日熔断 18% 权益 |
| F3 | P1 | `trade_engine.py` L1813-L1897；L1194-L1359 | 挂单只占单数/保证金，不计入组合热度与同向集中度；成交落地不复核闸门 | 多条挂单可穿透 heat cap / 同向上限 / 日熔断 |
| F4 | P2 | `risk_budget.py` L36；`risk_guard.py` L549；`trade_engine.py` L495 / L1855 | 风险比例回落来源分叉（`RISK_BUDGET_PCT` vs `TRADING_RISK_PCT`） | 两 env 分别配置时，AI 校验仓位与实际下单不一致 |
| F5 | P2 | `binance_trader.py` L270-L271；`trade_engine.py` L742 | MARKET_LOT_SIZE 口径混用 | 限价单被市价上限错误缩仓；市价单可能被 `-1013` 拒 |
| F6 | P3 | `trade_engine.py` L3622 / L3681-L3682 | 接管单 TP 数量未取整就落库 | `remaining_qty` / `remaining_risk` 尾差 |
| F7 | P3 | `trade_engine.py` L1056-L1059；`actual_review.py` L253-L278 | R 分母不含费、分子含费 | 窄止损策略 R 系统性偏低（口径问题） |

## 二、P1 证据与量化

### F1 风险预算不含交易成本

```python
# trade_engine.py L679
notional = risk_budget / stop_pct
# L840
risk_amount = round(qty * abs(ref_price - sl), 4)
# L1013-L1019：超限只 warning，不缩仓
actual_risk = fill_qty * abs(fill_price - sl)
if risk_amount > 0 and actual_risk > risk_amount * 1.15:
    logger.warning(...)
```

预算 30 U、ref=100、sl=99.5（0.5%）：

- `qty=60`，名义 6000 U；
- 触损价格亏损 30 U + 双边 taker 0.05% ≈ 5.99 U = **35.99 U（1.20×）**；
- 止损市价滑点 0.3% ≈ 18 U → **53.99 U（1.80×）**；
- `actual_risk` 不含平仓费与止损滑点，识别不到费用部分；超限也不缩仓。

与 [05-风控体系](../../风控/05-风控体系.md) “打止损时亏损恒等于风险预算”不符。另：`_adaptive_leverage`（L87）用纯价格 `stop_pct` 过强平闸门，未留给止损滑点余量。

**实施结果**（2026-10-27）：计价分母改为 `stop_pct_eff = (|ref−sl| + fee_rate×(ref+sl)) / ref`（`_cost_adjusted_stop_pct`）；强平闸门输入为 `stop_pct_eff + max_slippage_pct`（粗筛与精确档复验两处同源）；`risk_amount` 保持价格风险语义。`risk_amount` 记录值不变意味着组合热度与复盘 R 口径不因本次修复而历史不可比。

### F2 小账户基数放大风险

```python
# trade_engine.py L75-L84
return float(min(max(digit * magnitude, 100), 5000))
```

- `wallet=50` → 基数 100 → 单笔 3 U = 权益 6%；
- 同一基数用于日熔断（`_daily_loss_cap` L1452-L1458）→ 9 U = 权益 18%；
- L1780 `if _capital_base(wallet) <= 0` 对任意 wallet>0 都不成立，文档“钱包 <10 不开仓”未生效。

**实施结果**（2026-10-27）：基数取 `min(base, wallet)`——50→50、99.99→99.99、100→100、999.99→900；`_daily_loss_cap(50, 9)=4.5`。文档「钱包 <10 不开仓」表述修正为「钱包 ≤ 0 不开仓」；风控/执行/总览/交易系统四份文档已同步。

### F3 挂单绕过组合闸门

- `_pending_reserved`（L289-L302）只汇总条数与 `margin_used`；
- heat / 同向只查 `TradeRecord.status in (OPENED, TP1_HIT, TP2_HIT)`，不含 `PendingEntryOrder`；
- `materialize_pending_entry` 成交后直接挂保护单建记录，不复核 heat / 同向 / 日熔断 / 余额。

**实施结果**（2026-10-27）：开仓侧 heat/同向统计追加同账号活跃挂单 `risk_amount`（β 加权，排除本分析自身挂单）；成交落地新增 `_pending_gate_breaches` 复核热度/同向/当日熔断，越线记 `GATE_BREACH` 事件 + warning，**保护单照挂**（复核异常降级跳过，绝不留裸仓）。

## 三、P2 / P3 摘要

- **F4**：`risk_budget` 与 `risk_guard` 回落 `RISK_BUDGET_PCT`，`trade_engine` 回落 `TRADING_RISK_PCT`。默认同为 3.0，属潜伏分叉。建议统一为 `TRADING_RISK_PCT`。
- **F5**：限价单套用 `min(max_qty, market_max_qty)`；市价/止损市价单只校验 `min_qty`。建议按单型取值。
- **F6**：`adopt_position` 挂单用 `round_qty`，raw 落库用原始值。建议先取整再共用。
- **F7**：`initial_risk` 为价格风险，`realized_pnl` 含费。本轮建议只补文档口径说明，不改算法（避免历史 R 不可比）。

## 四、非缺陷但需在案

- `wallet_balance()["wallet"]` 为 wallet balance（不含未实现盈亏），持仓浮亏时风险基数偏高。
- `_capital_base` 分档在 100/1000/10000 处跳变（如 999.99→900、1000→1000），属既定设计。
- `actual_risk` 的 1.15× 阈值是监控而非硬约束。

## 五、影响面

F1-F3 已实施（2026-10-27）：新仓名义缩小约 5%–17%（随费率/止损宽度）；小账户熔断阈值随权益等比下降；挂单提前占用热度额度，成交落地留越线痕迹。F4-F7 未改动。

## 六、范围确认

本轮授权范围：**仅 P1（F1/F2/F3）**，已实施并全量测试通过。F4/F5/F6/F7 未授权，保持不动；如需继续，按 [tasks.md](tasks.md) 的 T4-T7 逐项确认。
