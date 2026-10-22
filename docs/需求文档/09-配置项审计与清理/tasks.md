# 任务清单：配置项审计与清理

> 本次为纯前端删除/收敛 + 文档同步；后端与 DB 零改动。

## T1 删除 `breakout_vol_ratio` 弹窗入口

- 文件：`frontend/src/components/ScanConfigPanel.tsx`
- 从 `FILTER_FIELDS` 删除「突破量比门槛」条目。
- 验收：弹窗无该字段；保存不提交该键。

## T2 `kline_interval` 仅保留 1h

- 文件：`frontend/src/components/ScanConfigPanel.tsx`
- `Select` 选项收敛为单一 `1h`，tooltip 说明小时级调度。
- 验收：下拉无非 1h 选项。

## T3 `kline_window` 最小值 300

- 文件：`frontend/src/components/ScanConfigPanel.tsx`
- `min: 30 → 300`；tooltip 说明后端 `max(kline_window, 300)` 取数下限。
- 验收：输入 300 可保存，最小步进不再出现 30–299。

## T4 重写 `pullback_touch_refs` tooltip

- 文件：`frontend/src/components/ScanConfigPanel.tsx`
- 按 spec §S4 文案替换。
- 验收：文案与 `trigger.py` 当前行为一致。

## T5 文档同步

- 文件：`docs/前端与查询/08-前端界面.md` §4.1
- 更新扫描基础（周期固定 1h、数量 300-1500），并从筛选参数组删除 `breakout_vol_ratio`。

## T6 验证与提交

- `cd frontend && npm run build`；
- 手工验收 spec §5 五项；
- 本地 conventional commit（不推送、不部署）。
