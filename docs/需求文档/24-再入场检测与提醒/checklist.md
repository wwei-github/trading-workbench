# 再入场检测与提醒 — 验收清单

- [x] 仅正式网建监控：测试网止损单不产生 `reentry_watches` 行
- [x] 只提醒不下单：不创建 `TradeRecord`、不改仓位、不挂 SL/TP、不 bump `RULE_VERSION`
- [x] 平仓时结构闸与 `shadow.compute_reentry_precond` 同口径（EMA 排列 / ADX≥20 / R²≥0.5）
- [x] 触发价按 `entry ± θR` 计算，θ=0 时等于原开仓价；窗口默认 24h
- [x] 生命周期六态齐全且优先级正确：触发 → 到期 → 同币在跑仓 → 结构破坏 → 人工忽略
- [x] 飞书卡每次监控只发一张；发送失败不写 `notified_at`，下轮补发
- [x] 飞书卡与前端页面均出现免责声明「仅提醒，不会自动开单；回测该再入场腿期望为负」
- [x] API 列表透出 `seconds_left` 与结构展平字段；dismiss 对终态返回 400
- [x] 前端合约模式「交易记录」后出现「再入场提醒」页签，含监控中/已触发/已结束分组
- [x] 面板列齐全（币种/方向/状态/原入场·止损/触发价/当前价/距触发/结构/剩余时间/忽略），60s 自动刷新，忽略需二次确认
- [x] 单测与回归通过：`test_reentry_watch.py` + `test_reentry_shadow.py` + `test_hold_warnings.py` 全绿；`tsc --noEmit` 无错

> 验证记录（2026-10-31）：`32 passed`（pytest，3.21s）；`npx tsc --noEmit` exit 0。
> 唯一口径差异：K 线弹窗 hitTime 用 `created_at` 而非 `closed_at`（后端未透出 closed_at，平仓后分钟级建立监控，误差可忽略）。
