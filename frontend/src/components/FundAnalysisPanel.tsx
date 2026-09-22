/**
 * 资金分析页（docs/14）：筛选条（环境/时间范围）+ 概览 6 卡 + 2×3 图表栅格
 * - 数据源 GET /api/trades/analytics（已闭单按北京时间自然日落桶，划转按 transfer_time）
 * - 资金曲线 = 保证金余额估算（期初资金+累计净流入+累计已实现盈亏，后端重建）
 * - 每日净收益 = 日历热力图（calendar + heatmap，红绿深浅随当日盈亏）
 * - 配色全走 scheme 体系（红涨绿跌/绿涨红跌可切换），禁止硬编码涨跌色
 * - 划转按环境同步落库（testnet 标记），随页内 scope 过滤展示
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Row,
  Segmented,
  Space,
  Spin,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { Dayjs } from 'dayjs'
import type { EChartsCoreOption } from 'echarts/core'
import EChart from './EChart'
import StatCard from './StatCard'
import { EXIT_REASON_MAP } from './TradesPanel'
import { scanApi } from '../api/scan'
import { dayjs } from '../utils/dayjs'
import { SCHEME_UP_DOWN, schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import type { FundAnalytics } from '../types'

type Scope = 'live' | 'testnet'
type RangeKey = 'all' | 'week' | 'month' | 'custom'

/** 本周周一（北京时间）：dayjs 默认周日起始一周，手动归一到周一 */
function mondayOfThisWeek(): Dayjs {
  const dow = dayjs().day() // 0=周日 … 6=周六
  return dayjs().subtract((dow + 6) % 7, 'day')
}

/** hex → 带透明度 rgba（areaStyle 渐变用） */
function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

/** 带符号金额（+12.34 / -5.00 / 0.00） */
function fmtSigned(n: number): string {
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}`
}

// 盈亏分布档位：负值桶 = 深亏档（后端 ">|1000|"）、以 "-" 开头、或最小亏损档 "0~-"，其余为盈利档
function isNegBucket(bucket: string): boolean {
  return bucket === '>|1000|' || bucket.startsWith('-') || bucket.startsWith('0~-')
}

/** 资金分析页（docs/14） */
export default function FundAnalysisPanel() {
  // 默认 scope 跟随当前交易环境（系统配置弹窗可切，2026-09-20）；
  // 配置未加载前先按测试网（与后端 TRADING_TESTNET 初始值一致），拉到配置后校正
  const { aiConfig, fetchConfig } = useScanStore()
  const defaultScope: Scope = (aiConfig?.trading_testnet ?? true) ? 'testnet' : 'live'
  const [scope, setScope] = useState<Scope>('testnet')
  const [scopeInit, setScopeInit] = useState(false)
  const [range, setRange] = useState<RangeKey>('all')
  const [dates, setDates] = useState<[Dayjs, Dayjs] | null>(null)
  const [data, setData] = useState<FundAnalytics | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!aiConfig) {
      fetchConfig()
      return
    }
    if (!scopeInit) {
      setScope(defaultScope)
      setScopeInit(true)
    }
  }, [aiConfig, fetchConfig, scopeInit, defaultScope])

  const colorScheme = useScanStore((s) => s.colorScheme)
  const up = SCHEME_UP_DOWN[colorScheme].up
  const down = SCHEME_UP_DOWN[colorScheme].down

  const fetchData = useCallback(async () => {
    // 自定义范围但未选日期：清空旧数据，等选完再请求
    if (range === 'custom' && !dates) {
      setData(null)
      return
    }
    const params: { scope: Scope; start?: string; end?: string } = { scope }
    if (range === 'week') {
      params.start = mondayOfThisWeek().format('YYYY-MM-DD')
      params.end = dayjs().format('YYYY-MM-DD')
    } else if (range === 'month') {
      params.start = dayjs().startOf('month').format('YYYY-MM-DD')
      params.end = dayjs().format('YYYY-MM-DD')
    } else if (range === 'custom' && dates) {
      params.start = dates[0].format('YYYY-MM-DD')
      params.end = dates[1].format('YYYY-MM-DD')
    }
    setLoading(true)
    try {
      setData(await scanApi.trades.analytics(params))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取资金分析失败')
    } finally {
      setLoading(false)
    }
  }, [scope, range, dates])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const summary = data?.summary

  // ① 资金曲线：保证金余额（估算）Line + 蓝色 areaStyle 渐变 + dataZoom inside
  // 口径 = 期初资金 + 累计净流入 + 累计已实现盈亏（后端重建，不含历史浮动）
  const equityOption = useMemo<EChartsCoreOption>(() => {
    const pts = data?.balance_curve ?? []
    return {
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: unknown) => `${Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`,
      },
      grid: { left: 64, right: 20, top: 24, bottom: 40 },
      dataZoom: [{ type: 'inside' }],
      xAxis: { type: 'category', data: pts.map((p) => p.date) },
      yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [
        {
          name: '保证金余额',
          type: 'line',
          data: pts.map((p) => p.balance),
          smooth: true,
          showSymbol: false,
          lineStyle: { color: '#1677ff', width: 2 },
          itemStyle: { color: '#1677ff' },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: withAlpha('#1677ff', 0.22) },
                { offset: 1, color: withAlpha('#1677ff', 0.02) },
              ],
            },
          },
        },
      ],
    }
  }, [data])

  // ② 每日净收益：日历热力图（calendar + heatmap），颜色 = 当日盈亏涨跌色，
  // visualMap 对称刻度（±maxAbs）保证红绿深浅可比
  const dailyOption = useMemo<EChartsCoreOption>(() => {
    const pts = data?.daily_pnl ?? []
    const maxAbs = Math.max(1, ...pts.map((p) => Math.abs(p.pnl)))
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { value: [string, number] }) =>
          `${p.value[0]}<br/>当日净收益 <strong style="color:${schemeValueColor(Number(p.value[1]) || 0, colorScheme)}">${fmtSigned(Number(p.value[1]) || 0)} USDT</strong>`,
      },
      visualMap: {
        min: -maxAbs,
        max: maxAbs,
        calculable: false,
        show: false,
        inRange: { color: [down, '#f0f0f0', up] },
      },
      calendar: {
        range: [pts[0]?.date ?? dayjs().format('YYYY-MM-DD'), pts[pts.length - 1]?.date ?? dayjs().format('YYYY-MM-DD')],
        cellSize: 'auto',
        orient: 'horizontal',
        left: 30,
        right: 12,
        top: 30,
        itemStyle: { color: '#fafafa', borderColor: '#fff', borderWidth: 2 },
        splitLine: { show: false },
        dayLabel: { nameMap: 'ZH', firstDay: 1, fontSize: 10, color: '#8c8c8c' },
        monthLabel: { nameMap: 'ZH', fontSize: 10, color: '#595959' },
        yearLabel: { show: false },
      },
      series: [
        {
          name: '当日净收益',
          type: 'heatmap',
          coordinateSystem: 'calendar',
          data: pts.map((p) => [p.date, p.pnl]),
          itemStyle: { borderRadius: 3 },
        },
      ],
    }
  }, [data, colorScheme, up, down])

  // ③ 币种盈亏 Top10：横向 Bar（|pnl| 降序已由后端排好），正负双色 + 金额 label
  const symbolOption = useMemo<EChartsCoreOption>(() => {
    const rows = (data?.by_symbol ?? []).slice(0, 10)
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 8, right: 64, top: 8, bottom: 24, containLabel: true },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } } },
      yAxis: { type: 'category', inverse: true, data: rows.map((r) => r.symbol) },
      series: [
        {
          name: '净盈亏',
          type: 'bar',
          data: rows.map((r) => r.pnl),
          barMaxWidth: 16,
          itemStyle: {
            color: (p: { value: number | string }) =>
              schemeValueColor(Number(p.value) || 0, colorScheme),
          },
          label: {
            show: true,
            position: 'right',
            fontSize: 11,
            color: '#595959',
            formatter: (p: { value: number | string }) =>
              `${Number(p.value) > 0 ? '+' : ''}${Number(p.value).toFixed(2)}`,
          },
        },
      ],
    }
  }, [data, colorScheme])

  // ④ 出场原因分布：Pie（原因转中文，兜底原值）
  const exitOption = useMemo<EChartsCoreOption>(() => {
    const rows = (data?.exit_reasons ?? []).map((r) => ({
      name: EXIT_REASON_MAP[r.reason] || r.reason,
      value: r.count,
      pnl: r.pnl,
    }))
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: { name: string; value: number; percent: number; data?: { pnl?: number } }) =>
          `${p.name}<br/>笔数 ${p.value}（${p.percent}%）<br/>净盈亏 ${fmtSigned(Number(p.data?.pnl ?? 0))} USDT`,
      },
      legend: { bottom: 0, type: 'scroll', icon: 'circle', textStyle: { fontSize: 11 } },
      series: [
        {
          name: '出场原因',
          type: 'pie',
          radius: ['40%', '70%'],
          center: ['50%', '44%'],
          data: rows,
          label: { formatter: '{c}笔', fontSize: 11, color: '#595959' },
        },
      ],
    }
  }, [data])

  // ⑤ 盈亏分布：Bar（后端按深亏→深盈固定序），负档跌色/正档涨色
  const distOption = useMemo<EChartsCoreOption>(() => {
    const rows = data?.pnl_distribution ?? []
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 40, right: 16, top: 24, bottom: 56 },
      xAxis: {
        type: 'category',
        data: rows.map((r) => r.bucket),
        axisLabel: { rotate: 40, fontSize: 10 },
      },
      yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [
        {
          name: '笔数',
          type: 'bar',
          data: rows.map((r) => ({
            value: r.count,
            itemStyle: { color: isNegBucket(r.bucket) ? down : up },
          })),
          barMaxWidth: 24,
        },
      ],
    }
  }, [data, up, down])

  // ⑥ 资金流水：分组 Bar（转入涨色/转出跌色）+ 累计净流入 Line 叠加
  const transferOption = useMemo<EChartsCoreOption>(() => {
    const rows = data?.transfers ?? []
    let acc = 0
    const cum = rows.map((r) => Number((acc += r.transfer_in - r.transfer_out).toFixed(2)))
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 0, icon: 'circle', textStyle: { fontSize: 11 } },
      grid: { left: 56, right: 20, top: 32, bottom: 40 },
      xAxis: { type: 'category', data: rows.map((r) => r.date) },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [
        {
          name: '转入',
          type: 'bar',
          data: rows.map((r) => r.transfer_in),
          itemStyle: { color: up },
          barMaxWidth: 14,
        },
        {
          name: '转出',
          type: 'bar',
          data: rows.map((r) => r.transfer_out),
          itemStyle: { color: down },
          barMaxWidth: 14,
        },
        {
          name: '累计净流入',
          type: 'line',
          data: cum,
          smooth: true,
          showSymbol: false,
          lineStyle: { color: '#1677ff', width: 2 },
          itemStyle: { color: '#1677ff' },
        },
      ],
    }
  }, [data, up, down])

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* 筛选条：环境 + 时间范围（自定义出现 RangePicker）+ 刷新 */}
      <Row justify="space-between" align="middle" gutter={[0, 8]}>
        <Col flex="auto">
          <Space size={12} wrap>
            <Segmented
              value={scope}
              onChange={(v) => setScope(v as Scope)}
              options={[
                { label: "正式网", value: "live" },
                { label: "测试网", value: "testnet" },
              ]}
            />
            <Segmented
              value={range}
              onChange={(v) => setRange(v as RangeKey)}
              options={[
                { label: "全部", value: "all" },
                { label: "本周", value: "week" },
                { label: "本月", value: "month" },
                { label: "自定义", value: "custom" },
              ]}
            />
            {range === "custom" && (
              <DatePicker.RangePicker
                value={dates}
                onChange={(v) => setDates(v as [Dayjs, Dayjs] | null)}
                allowClear={false}
                placeholder={["开始日期", "结束日期"]}
              />
            )}
          </Space>
        </Col>
        <Button icon={<ReloadOutlined />} onClick={fetchData}>
          刷新
        </Button>
      </Row>

      {/* 概览 6 卡：净收益/盈亏比随涨跌色；胜率 ≥50 涨色；其余中性 */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <StatCard
          title="区间净收益"
          value={summary ? `${fmtSigned(summary.net_pnl)} USDT` : "-"}
          color={
            summary ? schemeValueColor(summary.net_pnl, colorScheme) : undefined
          }
        />
        <StatCard
          title="胜率（净盈利笔数占比）"
          value={summary?.win_rate == null ? "-" : `${summary.win_rate}%`}
          color={
            summary?.win_rate == null
              ? undefined
              : summary.win_rate >= 50
                ? up
                : down
          }
        />
        <StatCard
          title="手续费"
          value={summary ? `${summary.fee_total.toFixed(2)} USDT` : "-"}
        />
        <StatCard
          title="资金费"
          value={summary ? `${summary.funding_total.toFixed(2)} USDT` : "-"}
        />
        <StatCard
          title="净流入"
          value={summary ? `${fmtSigned(summary.transfer_net)} USDT` : "-"}
        />
        <StatCard
          title="盈亏比R（止盈单）"
          value={
            summary?.avg_win_r == null
              ? "-"
              : `${fmtSigned(summary.avg_win_r)}R`
          }
          color={
            summary?.avg_win_r == null
              ? undefined
              : schemeValueColor(summary.avg_win_r, colorScheme)
          }
        />
      </div>

      {/* 空态/图表栅格：data 为空（未选日期或加载中）不渲染图表，无已平仓交易时整体空态 */}
      {data === null ? (
        range === "custom" && !dates ? (
          <Empty description="请选择日期范围" style={{ marginTop: 60 }} />
        ) : (
          <Empty description="暂无数据" style={{ marginTop: 60 }} />
        )
      ) : data.summary.closed === 0 ? (
        <Empty description="该范围内暂无已平仓交易" style={{ marginTop: 60 }} />
      ) : (
        <Spin spinning={loading}>
          <Row gutter={[12, 12]}>
            <Col span={8}>
              <Card size="small" title="资金曲线（保证金余额·估算）">
                {(data?.balance_curve.length ?? 0) === 0 ? (
                  <Empty
                    description="交易所余额数据不可用"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    style={{ height: 300, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}
                  />
                ) : (
                  <EChart option={equityOption} height={300} />
                )}
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="每日净收益（日历）">
                <EChart option={dailyOption} height={300} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="币种盈亏 Top10">
                <EChart option={symbolOption} height={300} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="出场原因分布">
                <EChart option={exitOption} height={300} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="盈亏分布（笔数）">
                <EChart option={distOption} height={300} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="资金流水（USDT）">
                {(data?.transfers.length ?? 0) === 0 ? (
                  <Empty
                    description="该范围内暂无划转记录"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    style={{
                      height: 300,
                      display: "flex",
                      flexDirection: "column",
                      justifyContent: "center",
                    }}
                  />
                ) : (
                  <EChart option={transferOption} height={300} />
                )}
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
    </div>
  );
}
