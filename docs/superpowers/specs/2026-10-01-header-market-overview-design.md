# Header 改版：Logo + 大盘环境/市场情绪 + 合约现货切换右上角

日期：2026-10-01
状态：已确认（用户批准）

## 背景

当前 Header（`frontend/src/App.tsx`）只有标题文字；合约/现货切换位于页面内工具栏
（`frontend/src/pages/ScanResult.tsx` 顶部的 Segmented）。用户要求：

1. Header 左侧增加 Logo（内联 SVG，K 线蜡烛主题）
2. Header 中部展示大盘环境/市场情绪（精简版：BTC/ETH EMA 趋势 + 24h 涨跌幅 + 恐贪指数）
3. 合约/现货切换移到 Header 右上角

## 方案要点

### 1. 大盘环境/市场情绪（Header 中部）

**后端** — 新增 `GET /api/market/overview`（新建 `app/api/market.py`，`main.py` 注册）：

- 聚合现有数据源，零新增抓取逻辑：
  - `market_data.get_fear_greed()`：恐贪指数（alternative.me，进程内 TTL 30min）
  - `market_data.get_market_breadth(ExchangePool())`：BTC/ETH EMA 形态 + 24h 涨跌幅（TTL 5min）
- 子项失败静默返回 `null`，接口整体恒 200（前端降级展示，不阻塞页面）
- 返回结构：

```json
{
  "fear_greed": { "value": 72, "label": "Greed" } | null,
  "breadth": {
    "BTCUSDT": { "ema_state": "bullish_align", "ema_label": "多头排列", "change_24h_pct": 2.31 },
    "ETHUSDT": { ... }
  } | null
}
```

**前端** — 新增 `components/MarketOverviewBar.tsx`：

- BTC / ETH：EMA 状态标签（`ema_label`）+ 24h 涨跌幅；涨跌色跟随全局配色方案
  （`useScanStore.colorScheme`，红涨绿跌/绿涨红跌自动翻转）
- 恐贪指数：数值 + 中文分档标签（极度恐惧/恐惧/中性/贪婪/极度贪婪）+ 分档着色
  （恐惧红 → 中性灰 → 贪婪绿；语义色固定，不随配色方案翻转）
- 挂载时拉取一次，60s 轮询刷新；Tooltip 展示明细
- 全部数据不可用 → 显示灰色「大盘 --」，不打断布局

### 2. Logo（Header 左侧）

- 内联 SVG 组件（`components/Logo.tsx`）：三根 K 线蜡烛造型（红-青-红），
  深色 Header 底（#001529）上醒目；零外部依赖，无图片资源
- 保留现有标题「虚拟币筛选工作台」，logo 与标题水平排列

### 3. 合约/现货切换（Header 右上角）

- `mode` 状态从 `ScanResult` 提升到 `App`，继续用 `useTabMemory('tabs.mode', ['futures','spot'])`
  —— **localStorage key 不变，刷新后模式记忆不丢**
- `ScanResult` 改为受控组件，接收 `mode` / `onModeChange` props；删除工具栏里的 Segmented；
  工具栏其余控件（ScanStatus、配置弹窗、EnvironmentTag 等）原样不动

## 涉及文件

| 层 | 新增 | 修改 |
|---|---|---|
| 后端 | `app/api/market.py`、`tests/test_market_api.py` | `app/api/__init__.py`、`app/main.py` |
| 前端 | `components/MarketOverviewBar.tsx`、`components/Logo.tsx`、`api/market.ts` | `App.tsx`、`pages/ScanResult.tsx`、`types/index.ts` |

## 错误处理与降级

- 后端：恐贪指数依赖 alternative.me 外网，被墙/超时 → 该子项 `null`；
  大盘广度依赖交易所 K 线，失败 → `null`；均不抛异常
- 前端：请求失败静默（console.debug），展示降级态；轮询 60s 一次，命中后端缓存，无交易所压力

## 测试

- 后端 `tests/test_market_api.py`：monkeypatch `get_fear_greed` / `get_market_breadth`，
  覆盖全量数据 / 子项缺失 / 全失败三种降级路径（不触网）
- 前端：`tsc` + `vite build` 通过；浏览器手工验证 Header 布局与配色翻转
