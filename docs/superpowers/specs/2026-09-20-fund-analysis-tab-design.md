# 资金分析 Tab 设计方案

日期：2026-09-20
状态：已确认（用户选定：环境可切换 / ECharts / 后端聚合 API）

## 1. 目标

在主页面新增顶层 Tab「资金分析」，通过图表展示合约账户资金状况与交易盈亏结构，支持时间范围筛选。

## 2. 决策记录

| 决策点 | 结论 | 理由 |
|---|---|---|
| 数据口径 | 正式网/测试网 Segmented 切换，默认正式网 | 测试网单子会扭曲真实资金曲线 |
| 图表库 | ECharts（按需引入） | 现有 lightweight-charts 只适合K线；ECharts 面积/柱/饼全覆盖，中文文档 |
| 数据获取 | 新增后端聚合 API | 现有列表接口 500 条上限，「全部」范围会截断失真 |
| 页面形态 | 单页仪表盘（方案 A） | 数据量小（几百笔），一次拉全 + 图表栅格，无二级 Tab 必要 |

## 3. 页面结构

新增顶层 Tab「资金分析」（ScanResult.tsx，置于「资金划转」旁），组件 `FundAnalysisPanel`：

```
┌──────────────────────────────────────────────┐
│ [正式网|测试网]  [全部|本周|本月|自定义____] [刷新] │  ← 筛选条
├──────────────────────────────────────────────┤
│ 区间净收益 | 胜率 | 手续费 | 资金费 | 净流入 | 盈亏比R │  ← StatCard 概览（复用）
├──────────────────────┬───────────────────────┤
│ ① 资金曲线（面积图）    │ ② 每日净收益（正绿负红柱）│
├──────────────────────┼───────────────────────┤
│ ③ 币种盈亏 Top10（横条）│ ④ 出场原因分布（饼图）    │
├──────────────────────┼───────────────────────┤
│ ⑤ 盈亏分布（直方图）    │ ⑥ 资金流水（转入/转出柱）  │
└──────────────────────┴───────────────────────┘
```

## 4. 筛选器交互

- **环境**：Segmented「正式网（默认）/ 测试网」，过滤 `trade_records.testnet`
- **时间范围**：全部（默认）/ 本周（周一起）/ 本月（自然月）/ 自定义（RangePicker）
- **口径**：北京时间自然日（与日报一致）；交易按 `closed_at` 落桶，无平仓时间不计入
- 切换任一筛选项 → 重新请求聚合接口，图表 loading 过渡

## 5. 后端 API（1 个）

`GET /api/trades/analytics?scope=live|testnet&start=YYYY-MM-DD&end=YYYY-MM-DD`

不传 start/end 即全部。响应：

```json
{
  "summary": { "closed": 27, "wins": 17, "win_rate": 63, "net_pnl": 1537.21,
               "fee_total": -12.3, "funding_total": 1.2, "avg_win_r": 1.25,
               "transfer_in": 1000, "transfer_out": 200, "transfer_net": 800 },
  "equity_curve":  [{ "date": "2026-09-01", "cum_pnl": 123.4 }],
  "daily_pnl":     [{ "date": "2026-09-01", "pnl": 45.6 }],
  "by_symbol":     [{ "symbol": "BTCUSDT", "pnl": 123, "count": 5, "wins": 3 }],
  "exit_reasons":  [{ "reason": "sl", "count": 10, "pnl": -300 }],
  "pnl_distribution": [{ "bucket": "-100~-50", "count": 4 }],
  "transfers":     [{ "date": "2026-09-01", "in": 100, "out": 50, "cum_net": 500 }]
}
```

实现要点：

- SQL 端聚合（`func.date` + `group by`），无行数上限
- 已闭单过滤：`status='CLOSED' AND testnet=? AND closed_at ∈ range`
- 划转来自 `account_transfers`（服务器当前配置环境账户）；测试网视图下划转区显示空态说明
- 时间过滤在 SQL 用北京时间边界换算 UTC（DB 存 UTC naive）

## 6. 前端实现

- 依赖：`npm i echarts`，按需注册（Line/Bar/Pie/Grid/Tooltip/DataZoom）+ 薄封装 `<EChart option={...} />`（ResizeObserver 自适应）
- 配色：全部走现有 `SCHEME_UP_DOWN` / `schemeValueColor`（红涨绿跌可切换），与全站一致
- 变更文件：
  - `backend/app/api/trades.py` — analytics 端点
  - `backend/app/schemas/trade.py` — 响应模型
  - `frontend/src/components/FundAnalysisPanel.tsx` — 页面
  - `frontend/src/components/EChart.tsx` — 图表封装
  - `frontend/src/api/scan.ts` — 接口
  - `frontend/src/types/index.ts` — 类型
  - `frontend/src/pages/ScanResult.tsx` — 注册 Tab

## 7. 图表明细

| # | 图表 | 类型 | 数据 | 要点 |
|---|---|---|---|---|
| ① | 资金曲线 | 面积图 | equity_curve（累计净收益） | x=日期，面积渐变随配色方案；DataZoom 缩放 |
| ② | 每日净收益 | 柱状图 | daily_pnl | 正绿负红（跟随配色方案），0 轴基线 |
| ③ | 币种盈亏 Top10 | 横向条形 | by_symbol 按 pnl 绝对值排序取 10 | 正负双色，右侧标金额 |
| ④ | 出场原因分布 | 饼图 | exit_reasons | reason → 中文映射（复用 EXIT_REASON_MAP） |
| ⑤ | 盈亏分布 | 直方图 | pnl_distribution | 固定档位桶，观察盈亏形态 |
| ⑥ | 资金流水 | 分组柱状图 | transfers | 转入/转出两列 + 累计净流入折线叠加 |

## 8. 概览指标口径

与交易记录页/日报一致：

- 区间净收益 = 区间内已闭单 realized_pnl 之和（含手续费/资金费）
- 胜率 = 盈利笔数 ÷ 已闭单笔数（realized_pnl > 0 记盈利）
- 手续费 = fee_total 合计（负值）；资金费 = funding_total 合计（正负皆可）
- 盈亏比 R = 盈利单 realized_pnl ÷ risk_amount 均值（仅 risk_amount > 0 的已闭单）
- 净流入 = 划转转入 − 转出（区间内）

## 9. 边界与空态

- 区间内无已闭单 → 图表区显示「该范围内暂无已平仓交易」空态
- 自定义范围跨年正常聚合（SQL 无上限）
- FAILED/未开仓记录不参与任何图表
- 环境切换后划转区空态：测试网账户未同步划转时提示说明

## 10. 非目标（YAGNI）

- 不做导出 CSV/图片
- 不做对比基准（BTC 同期涨跌）
- 不做 Sharpe/回撤等高级指标（后续可加在 summary）
- 不做自动刷新轮询（手动刷新按钮即可，数据源本身按小时变动）
