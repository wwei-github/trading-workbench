# 资金分析 Tab（docs/14）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 docs/14 实现资金分析 Tab：后端 1 个聚合接口（6 组数据）+ 前端 ECharts 单页仪表盘（筛选条/概览卡/6 图栅格）。

**Architecture:** 后端 `GET /api/trades/analytics` 一次返回 summary/equity_curve/daily_pnl/by_symbol/exit_reasons/pnl_distribution/transfers，SQL 端聚合无行数上限；前端 `FundAnalysisPanel` 单页仪表盘，`EChart` 薄封装按需注册 ECharts，配色全走现有 scheme 体系。

**Tech Stack:** FastAPI + SQLAlchemy 2.0（func.date 聚合）；React 18 + AntD 5 + echarts（按需）。

## Global Constraints

- 设计依据：`docs/14-资金分析Tab设计.md`；口径与交易记录页/日报一致（北京时间自然日，DB 存 UTC naive——SQL 过滤用「UTC 时间 ∈ 北京日边界换算」，展示层按北京时区 +8 落桶）
- 验证命令：后端 `python3 -m compileall backend/app -q`；前端 `cd frontend && npx tsc --noEmit`
- 配色：正负值一律 `schemeValueColor` / `SCHEME_UP_DOWN`（红涨绿跌可切换），禁止硬编码绿涨
- `AccountTransfer.amount` 带符号（正=转入/负=转出）；划转只有一套（服务器配置环境账户），测试网视图下划转区空态说明
- 全部在 **dev 分支**提交，commit 中文，`feat:` 前缀；不 push
- 依赖安装：`cd frontend && npm i echarts`（唯一新依赖）

---

### Task 1: 后端聚合接口

**Files:**
- Modify: `backend/app/api/trades.py`（文件头部注释"待实现"字样顺带删除）
- Modify: `backend/app/schemas/trade.py`（追加响应模型）

**Interfaces:**
- Produces: `GET /api/trades/analytics?scope=live|testnet&start=&end=`（YYYY-MM-DD 闭区间，按北京时间自然日）；响应结构见 docs/14 §5。Pydantic 模型 `FundAnalyticsOut`（含内嵌 summary 等 7 组）

- [ ] **Step 1: schemas/trade.py 追加响应模型**

按该文件现有风格（注意 Optional 用法与版本惯例）追加：

```python
class FundSummary(BaseModel):
    closed: int = 0
    wins: int = 0
    win_rate: Optional[float] = None      # 0-100，无闭单为 None
    net_pnl: float = 0.0
    fee_total: float = 0.0
    funding_total: float = 0.0
    avg_win_r: Optional[float] = None     # 仅 risk_amount>0 的盈利单
    transfer_in: float = 0.0
    transfer_out: float = 0.0
    transfer_net: float = 0.0


class DailyPoint(BaseModel):
    date: str          # YYYY-MM-DD（北京时间自然日）
    pnl: float = 0.0


class SymbolPnl(BaseModel):
    symbol: str
    pnl: float
    count: int
    wins: int


class ExitReasonStat(BaseModel):
    reason: str
    count: int
    pnl: float


class PnlBucket(BaseModel):
    bucket: str        # 如 "-100~-50"、"50~100"
    count: int


class TransferPoint(BaseModel):
    date: str
    inn: float = 0.0   # 转入合计（正数）
    out: float = 0.0   # 转出合计（正数）


class FundAnalyticsOut(BaseModel):
    summary: FundSummary
    equity_curve: list[DailyPoint]
    daily_pnl: list[DailyPoint]
    by_symbol: list[SymbolPnl]
    exit_reasons: list[ExitReasonStat]
    pnl_distribution: list[PnlBucket]
    transfers: list[TransferPoint]
```

- [ ] **Step 2: trades.py 加 analytics 端点**

路由注册在 `/market-risk` 附近（动态路由之前）。核心逻辑：

```python
@router.get("/analytics", response_model=FundAnalyticsOut)
def fund_analytics(
    scope: str = "live",
    start: Optional[str] = None,   # YYYY-MM-DD（北京时间）
    end: Optional[str] = None,     # 闭区间，含当日
    db: Session = Depends(get_db),
):
    """资金分析聚合（docs/14）：已闭单按 closed_at 落北京时间自然日桶，划转按 transfer_time"""
    from datetime import timedelta
    from sqlalchemy import func, case
    testnet = scope == "testnet"

    # 北京日边界 → UTC（DB 存 UTC naive）：北京 = UTC+8，闭单时间 +8h 后的 date 即北京日
    def _range_clause(col):
        # 直接用 UTC 列比较换算后的 UTC 边界
        lo = hi = None
        if start:
            lo = datetime.fromisoformat(start) - timedelta(hours=8)
        if end:
            hi = datetime.fromisoformat(end) + timedelta(days=1) - timedelta(hours=8)
        return lo, hi

    lo, hi = _range_clause(TradeRecord.closed_at)
    base = select(TradeRecord).where(
        TradeRecord.status == "CLOSED",
        TradeRecord.testnet == testnet,
        TradeRecord.closed_at.is_not(None),
    )
    if lo:
        base = base.where(TradeRecord.closed_at >= lo)
    if hi:
        base = base.where(TradeRecord.closed_at < hi)
    rows = db.execute(base).scalars().all()

    # Python 侧聚合（量级几百笔，清晰优先；北京日键 = closed_at + 8h 取 date）
    def _bj_day(dt: datetime) -> str:
        return (dt + timedelta(hours=8)).date().isoformat()

    closed = len(rows)
    wins = sum(1 for r in rows if (r.realized_pnl or 0) > 0)
    net_pnl = round(sum(float(r.realized_pnl or 0) for r in rows), 2)
    fee_total = round(sum(float(r.fee_total or 0) for r in rows), 2)
    funding_total = round(sum(float(r.funding_total or 0) for r in rows), 2)
    win_rs = [
        float(r.realized_pnl) / float(r.risk_amount)
        for r in rows
        if (r.realized_pnl or 0) > 0 and r.risk_amount and float(r.risk_amount) > 0
    ]
    avg_win_r = round(sum(win_rs) / len(win_rs), 2) if win_rs else None

    daily: dict[str, float] = {}
    for r in rows:
        d = _bj_day(r.closed_at)
        daily[d] = daily.get(d, 0.0) + float(r.realized_pnl or 0)
    days = sorted(daily)
    cum = 0.0
    equity = [DailyPoint(date=d, pnl=round(cum := cum + daily[d], 2)) for d in days]

    by_symbol_map: dict[str, dict] = {}
    for r in rows:
        m = by_symbol_map.setdefault(r.symbol, {"pnl": 0.0, "count": 0, "wins": 0})
        m["pnl"] += float(r.realized_pnl or 0)
        m["count"] += 1
        if (r.realized_pnl or 0) > 0:
            m["wins"] += 1

    exit_map: dict[str, dict] = {}
    for r in rows:
        k = r.exit_reason or "unknown"
        m = exit_map.setdefault(k, {"count": 0, "pnl": 0.0})
        m["count"] += 1
        m["pnl"] += float(r.realized_pnl or 0)

    # 盈亏分布固定档位（|pnl| 上不封顶，<0 分 8 档，>0 分 8 档）
    neg_edges = [-1000, -500, -200, -100, -50, -20, -10, -5]
    pos_edges = [5, 10, 20, 50, 100, 200, 500, 1000]
    buckets: dict[str, int] = {}
    for r in rows:
        v = float(r.realized_pnl or 0)
        if v <= 0:
            for e in neg_edges:
                if v <= e:
                    buckets[f"{e}~{'0' if e == -5 else neg_edges[neg_edges.index(e)+1]}"] = buckets.get(f"{e}~{'0' if e == -5 else neg_edges[neg_edges.index(e)+1]}", 0) + 1
                    break
            else:
                buckets["0~-5"] = buckets.get("0~-5", 0) + 1
        else:
            for i, e in enumerate(pos_edges):
                if v < e:
                    lo_b = pos_edges[i-1] if i > 0 else 0
                    buckets[f"{lo_b}~{e}"] = buckets.get(f"{lo_b}~{e}", 0) + 1
                    break
            else:
                buckets[">1000"] = buckets.get(">1000", 0) + 1

    # 划转：amount 带符号，正=转入；scope=testnet 时前端展示空态，后端仍返回空列表
    tlo, thi = _range_clause(AccountTransfer.transfer_time)
    tq = select(AccountTransfer).where(AccountTransfer.asset == "USDT")
    if testnet:
        rows_t: list = []
    else:
        if tlo:
            tq = tq.where(AccountTransfer.transfer_time >= tlo)
        if thi:
            tq = tq.where(AccountTransfer.transfer_time < thi)
        rows_t = db.execute(tq).scalars().all()
    t_daily: dict[str, dict] = {}
    for t in rows_t:
        d = _bj_day(t.transfer_time)
        m = t_daily.setdefault(d, {"inn": 0.0, "out": 0.0})
        amt = float(t.amount)
        if amt >= 0:
            m["inn"] += amt
        else:
            m["out"] += -amt
    t_in = round(sum(m["inn"] for m in t_daily.values()), 2)
    t_out = round(sum(m["out"] for m in t_daily.values()), 2)

    return FundAnalyticsOut(
        summary=FundSummary(
            closed=closed, wins=wins,
            win_rate=round(wins / closed * 100, 1) if closed else None,
            net_pnl=net_pnl, fee_total=fee_total, funding_total=funding_total,
            avg_win_r=avg_win_r,
            transfer_in=t_in, transfer_out=t_out, transfer_net=round(t_in - t_out, 2),
        ),
        equity_curve=equity,
        daily_pnl=[DailyPoint(date=d, pnl=round(daily[d], 2)) for d in days],
        by_symbol=sorted(
            (SymbolPnl(symbol=s, pnl=round(m["pnl"], 2), count=m["count"], wins=m["wins"])
             for s, m in by_symbol_map.items()),
            key=lambda x: abs(x.pnl), reverse=True,
        ),
        exit_reasons=sorted(
            (ExitReasonStat(reason=k, count=m["count"], pnl=round(m["pnl"], 2))
             for k, m in exit_map.items()),
            key=lambda x: x.count, reverse=True,
        ),
        pnl_distribution=[PnlBucket(bucket=b, count=c) for b, c in buckets.items()],
        transfers=[
            TransferPoint(date=d, inn=round(m["inn"], 2), out=round(m["out"], 2))
            for d, m in sorted(t_daily.items())
        ],
    )
```

实现者注意：上面是逻辑基准而非逐字要求——`walrus` 累计若嫌绕可普通循环；盈亏分布档位边界建议重写为更清晰的 helper 函数（输入值 → 桶名），保持档位语义：负值 8 档（-5/-10/-20/-50/-100/-200/-500/-1000 切分，最深 < -1000 归 ">|1000|"），正值同构。响应模型 import 与 FundAnalyticsOut 等从 schemas 导入。

- [ ] **Step 3: 验证**

```bash
python3 -m compileall backend/app -q && echo PY-OK
# 冒烟（可选）：本地无 DB 可跳过；有则起服务后
# curl "localhost:8000/api/trades/analytics?scope=live"
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/trades.py backend/app/schemas/trade.py
git commit -m "feat: 资金分析聚合接口 /api/trades/analytics（docs/14，闭单按北京日落桶）"
```

---

### Task 2: ECharts 封装 + 类型 + API

**Files:**
- Create: `frontend/src/components/EChart.tsx`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/scan.ts`
- 依赖: `cd frontend && npm i echarts`

**Interfaces:**
- Produces:
  - `<EChart option={option} height={number} />`（自动 resize）
  - 类型 `FundAnalytics/FundSummary/DailyPoint/SymbolPnl/ExitReasonStat/PnlBucket/TransferPoint`
  - `scanApi.trades.analytics(params)` 方法

- [ ] **Step 1: 安装 echarts**

```bash
cd frontend && npm i echarts
```

- [ ] **Step 2: EChart.tsx**

```tsx
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([
  LineChart, BarChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkLineComponent, CanvasRenderer,
])

/** ECharts 薄封装：按需注册 + ResizeObserver 自适应容器宽度 */
export default function EChart({ option, height = 300 }: {
  option: EChartsCoreOption
  height?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = echarts.init(ref.current)
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(ref.current)
    return () => { ro.disconnect(); chart.dispose() }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={ref} style={{ width: '100%', height }} />
}
```

- [ ] **Step 3: 类型 + API 方法**

`types/index.ts` 追加（对齐后端响应）：

```typescript
export interface FundSummary {
  closed: number; wins: number; win_rate: number | null
  net_pnl: number; fee_total: number; funding_total: number
  avg_win_r: number | null
  transfer_in: number; transfer_out: number; transfer_net: number
}
export interface DailyPoint { date: string; pnl: number }
export interface SymbolPnl { symbol: string; pnl: number; count: number; wins: number }
export interface ExitReasonStat { reason: string; count: number; pnl: number }
export interface PnlBucket { bucket: string; count: number }
export interface TransferPoint { date: string; inn: number; out: number }
export interface FundAnalytics {
  summary: FundSummary
  equity_curve: DailyPoint[]
  daily_pnl: DailyPoint[]
  by_symbol: SymbolPnl[]
  exit_reasons: ExitReasonStat[]
  pnl_distribution: PnlBucket[]
  transfers: TransferPoint[]
}
```

`api/scan.ts` 的 trades 对象（marketRisk 旁）加：

```typescript
    analytics: (params: { scope: 'live' | 'testnet'; start?: string; end?: string }) =>
      api.get<FundAnalytics>('/trades/analytics', { params }).then((r) => r.data),
```

- [ ] **Step 4: 验证 + Commit**

```bash
cd frontend && npx tsc --noEmit && echo TS-OK
git add frontend/package.json frontend/package-lock.json frontend/src/components/EChart.tsx frontend/src/types/index.ts frontend/src/api/scan.ts
git commit -m "feat: ECharts 按需封装与资金分析类型/接口（docs/14 Task2）"
```

---

### Task 3: FundAnalysisPanel 页面 + 注册 Tab

**Files:**
- Create: `frontend/src/components/FundAnalysisPanel.tsx`
- Modify: `frontend/src/pages/ScanResult.tsx`

**Interfaces:**
- Consumes: Task 2 的 `EChart`、类型、`analytics` 方法；`SCHEME_UP_DOWN`/`schemeValueColor`（`utils/scheme`）；EXIT_REASON_MAP 中文映射（从 TradesPanel 导出或在 Panel 内重建同款映射——优先在 TradesPanel `export` 该常量后复用）

- [ ] **Step 1: FundAnalysisPanel.tsx**

页面结构（docs/14 §3）：筛选条（Segmented 环境 + Segmented 范围 + 自定义 RangePicker + 刷新按钮）→ 概览 6 StatCard（复用 TradesPanel 同款布局/字体风格，数值着色 schemeValueColor）→ 2×3 图表栅格。

关键实现要求：
1. 状态：`scope: 'live'|'testnet'`、`range: 'all'|'week'|'month'|'custom'`、`dates: [Dayjs, Dayjs]|null`、`data: FundAnalytics|null`、`loading`
2. 请求参数：week = 本周周一（北京时间）到今天；month = 本月 1 号到今天；all 不传 start/end；custom 用 dates.format('YYYY-MM-DD')。dayjs 现有依赖
3. 六张图 option（配色函数取自 utils/scheme，`colorScheme` 用法照 TradesPanel 现有代码）：
   - 资金曲线：Line + areaStyle 渐变（up 色），dataZoom inside，y 轴 0 基线 markLine
   - 每日净收益：Bar，`itemStyle.color = (p) => schemeValueColor(p.value, colorScheme)`
   - 币种 Top10：横向 Bar（yAxis category inverse），正负双色，label 显示金额
   - 出场原因：Pie（reason → EXIT_REASON_MAP 中文，兜底原值），半径 ['40%','70%']
   - 盈亏分布：Bar，bucket 作 x 轴
   - 资金流水：Bar 两系列（转入 up 色/转出 down 色）+ 累计净流入 Line（双 y 轴或同轴）
4. 空态：`data.summary.closed === 0` 时图区显示 Empty「该范围内暂无已平仓交易」；测试网划转 `transfers.length===0` 时图表区提示「测试网账户未同步划转」
5. 图表卡片高度统一 300；切换筛选项 loading 过渡（Card loading 或 Spin）

- [ ] **Step 2: ScanResult.tsx 注册 Tab**

Tabs items 里「资金划转」条目旁加：

```tsx
{
  key: 'fund-analysis',
  label: '资金分析',
  children: <FundAnalysisPanel />,
},
```

- [ ] **Step 3: 验证 + Commit**

```bash
cd frontend && npx tsc --noEmit && echo TS-OK
git add frontend/src/components/FundAnalysisPanel.tsx frontend/src/pages/ScanResult.tsx frontend/src/components/TradesPanel.tsx
git commit -m "feat: 资金分析Tab页面（筛选/概览/六图栅格，docs/14 Task3）"
```

---

### Task 4: 全量回归 + 提交汇总

- [ ] **Step 1: 全量验证**

```bash
python3 -m compileall backend/app -q && echo PY-OK
cd frontend && npx tsc --noEmit && echo TS-OK
```

- [ ] **Step 2: 手工冒烟清单（交给主会话/用户）**

- 本地起服务：切「本周/本月/自定义」→ 网络面板确认 analytics 请求参数正确、图表渲染
- 切测试网：划转区空态文案出现
- 红涨绿跌切换开关对图表颜色生效

- [ ] **Step 3: 汇总提交检查**

```bash
git log --oneline main..dev
git status --short   # 必须干净
```
