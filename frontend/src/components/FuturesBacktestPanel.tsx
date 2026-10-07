/**
 * 合约回测面板（docs/16 P-06）：假设出场规则的分钟级验证
 * - 样本源：scan=扫描候选全链路重放（假想入场）/ history=历史建议单+否决单+真实单
 * - 变体预设：CARD-05 提前保本 / CARD-06 TP0 微止盈 / ATR k 扫描——与基线（生产语义）
 *   配对对比，输出 delta_exp_r / delta_win_rate / 结局迁移
 * - 组合模拟：固定风险额 + 在跑上限 + 同向上限，R/USDT 双口径资金曲线
 * - 提交后轮询（celery 异步）；引擎版本随结果落库可追溯
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  InputNumber,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ExperimentOutlined, ReloadOutlined } from '@ant-design/icons'
import EChart from './EChart'
import type { EChartsCoreOption } from 'echarts/core'
import { backtestApi } from '../api/backtest'
import { SCHEME_UP_DOWN, schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import type {
  BacktestOrder,
  FuturesBacktestListItem,
  FuturesBacktestRecord,
  PortfolioPoint,
} from '../types/backtest'

const OUTCOME_LABEL: Record<string, string> = {
  loss: '止损', win_tp1: 'TP1止盈', win_tp2: 'TP2止盈', be_exit: '保本/扫损',
  open: '未决', invalid: '无效',
}
const SRC_LABEL: Record<string, string> = {
  scan: '扫描假想', suggest: 'AI建议', veto: '否决单', actual: '真实单',
}

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '-'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function rColor(v: number | null | undefined): string {
  const scheme = useScanStore.getState().colorScheme
  return schemeValueColor(v ?? 0, scheme)
}

/** hex → 带透明度 rgba（areaStyle 渐变用），与 FundAnalysisPanel 同款 */
function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

// 组合资金曲线（EChart 实现）：R/USDT 口径单线 + 0 基线 + dataZoom 区间缩放，
// 悬浮显示当日累计与平仓笔数；涨色随全局红绿方案
function EquityChart({ series, mode }: { series: PortfolioPoint[]; mode: 'r' | 'usdt' }) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const up = SCHEME_UP_DOWN[colorScheme].up
  const option = useMemo<EChartsCoreOption>(() => {
    const key = mode === 'r' ? 'equity_r' : 'equity_usdt'
    const pts = series
    const last = pts.length ? pts[pts.length - 1][key] : 0
    const digits = mode === 'r' ? 2 : 0
    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const arr = params as { name: string; value: number }[]
          const p = pts.find((x) => x.date === arr?.[0]?.name)
          const v = arr?.[0]?.value
          const sign = v > 0 ? '+' : ''
          return [
            `<b>${p?.date ?? arr?.[0]?.name}</b>`,
            `累计 ${sign}${fmt(v, digits)} ${mode === 'r' ? 'R' : 'U'}`,
            p ? `当日平仓 ${p.closed} 笔` : '',
          ].filter(Boolean).join('<br/>')
        },
      },
      grid: { left: 56, right: 20, top: 24, bottom: 40 },
      dataZoom: [{ type: 'inside' }],
      xAxis: { type: 'category', data: pts.map((p) => p.date), boundaryGap: false },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: '#f0f0f0' } },
        axisLabel: { formatter: (v: number) => fmt(v, digits) },
      },
      series: [
        {
          name: mode === 'r' ? '累计 R' : '累计 USDT',
          type: 'line',
          data: pts.map((p) => p[key]),
          smooth: true,
          showSymbol: false,
          lineStyle: { color: up, width: 2 },
          itemStyle: { color: up },
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: withAlpha(up, 0.24) },
                { offset: 1, color: withAlpha(up, 0.02) },
              ],
            },
          },
          markLine: {
            silent: true, symbol: 'none', label: { show: false },
            lineStyle: { color: '#999', type: 'dashed' },
            data: [{ yAxis: 0 }],
          },
          markPoint: {
            silent: true, symbol: 'circle', symbolSize: 6,
            label: { show: true, position: 'right', fontSize: 11, color: up,
                     formatter: () => `${last > 0 ? '+' : ''}${fmt(last, digits)}` },
            data: pts.length > 1 ? [{ coord: [pts.length - 1, last] }] : [],
          },
        },
      ],
    }
  }, [series, mode, up])
  return <EChart option={option} height={260} />
}

function Kpi({ title, value, color, sub }: {
  title: string; value: React.ReactNode; color?: string; sub?: React.ReactNode
}) {
  return (
    <Card size="small" style={{ background: '#fafafa' }} styles={{ body: { padding: '8px 12px' } }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>{title}</Typography.Text>
      <div style={{ fontSize: 20, fontWeight: 600, color, lineHeight: '28px' }}>{value}</div>
      {sub != null && <Typography.Text type="secondary" style={{ fontSize: 11 }}>{sub}</Typography.Text>}
    </Card>
  )
}

export default function FuturesBacktestPanel() {
  const [source, setSource] = useState<'scan' | 'history'>('scan')
  const [symbols, setSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT'])
  const [days, setDays] = useState(30)
  const [riskUsdt, setRiskUsdt] = useState(100)
  const [maxOpen, setMaxOpen] = useState(5)
  const [sameCap, setSameCap] = useState(0)
  // 变体预设（变体名与 overrides 与后端引擎键一一对应）
  const [card05, setCard05] = useState(false)
  const [card06, setCard06] = useState(false)
  const [atrScan, setAtrScan] = useState(false)

  const [running, setRunning] = useState<FuturesBacktestRecord | null>(null)
  const [result, setResult] = useState<FuturesBacktestRecord | null>(null)
  const [history, setHistory] = useState<FuturesBacktestListItem[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [recLoading, setRecLoading] = useState(false)
  const [curveVariant, setCurveVariant] = useState('baseline')
  const [curveMode, setCurveMode] = useState<'r' | 'usdt'>('r')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadHistory = useCallback(() => {
    backtestApi.list(20).then(setHistory).catch(() => {})
  }, [])

  const openHistory = useCallback(async (id: string) => {
    setRecLoading(true)
    try {
      setResult(await backtestApi.get(id))
    } catch {
      message.error('获取回测详情失败')
    } finally {
      setRecLoading(false)
    }
  }, [])

  useEffect(() => {
    loadHistory()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [loadHistory])

  const buildVariants = () => {
    const variants: { name: string; overrides: Record<string, number | boolean | null> }[] = []
    if (card05) variants.push({ name: 'CARD-05提前保本', overrides: { be_trigger_r: 0.8 } })
    if (card06) variants.push({ name: 'CARD-06微止盈', overrides: { tp0_r: 0.5, tp0_qty_pct: 0.25, tp0_gate_r: 1.2 } })
    if (atrScan) {
      variants.push({ name: 'ATR k=2.0', overrides: { trail_atr_k: 2.0 } })
      variants.push({ name: 'ATR k=2.5', overrides: { trail_atr_k: 2.5 } })
    }
    return variants
  }

  const submit = async () => {
    const variants = buildVariants()
    if (variants.length === 0) {
      message.warning('至少勾选一个变体预设（基线自动参与对比）')
      return
    }
    if (source === 'scan' && symbols.length === 0) {
      message.warning('扫描重放源必须指定币种')
      return
    }
    setSubmitting(true)
    try {
      const { id } = await backtestApi.create({
        source, symbols, days, variants,
        risk_usdt: riskUsdt, max_open_trades: maxOpen, same_direction_cap: sameCap,
      })
      const rec = await backtestApi.get(id)
      setRunning(rec)
      setResult(null)
      pollRef.current = setInterval(async () => {
        try {
          const r = await backtestApi.get(id)
          if (r.status !== 'RUNNING') {
            if (pollRef.current) clearInterval(pollRef.current)
            setRunning(null)
            setResult(r)
            if (r.status === 'DONE') message.success('回测完成')
            else message.error(`回测失败：${r.error || '未知错误'}`)
            loadHistory()
          }
        } catch {
          /* 轮询错误静默 */
        }
      }, 2000)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '提交回测失败')
    } finally {
      setSubmitting(false)
    }
  }

  const summary = result?.status === 'DONE' ? result.summary ?? null : null
  const base = summary?.variants?.[0] ?? null
  const basePf = summary?.portfolio?.find((p) => p.name === 'baseline') ?? null
  const pf = summary?.portfolio?.find((p) => p.name === curveVariant) ?? basePf
  const orders: BacktestOrder[] = result?.orders || []
  const deltaByName = new Map((summary?.deltas ?? []).map((d) => [d.name, d]))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>
      {/* 表单 */}
      <Card size="small" title={<><ExperimentOutlined /> 合约回测参数</>} extra={
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={loadHistory}>刷新历史</Button>
          <Button type="primary" size="small" loading={submitting} disabled={!!running} onClick={submit}>
            {running ? '回测中…' : '开始回测'}
          </Button>
        </Space>
      }>
        <Space wrap size="middle">
          <span>
            样本源{' '}
            <Segmented
              value={source}
              onChange={(v) => setSource(v as 'scan' | 'history')}
              options={[
                { value: 'scan', label: '扫描重放' },
                { value: 'history', label: '历史建议单' },
              ]}
            />
          </span>
          <span>
            <Tooltip title={source === 'history' ? '可空=全部币种' : '扫描重放源必填（≤50 个）'}>
              币种{' '}
              <Select
                mode="tags"
                value={symbols}
                onChange={setSymbols}
                placeholder={source === 'history' ? '全部币种' : '输入币种如 BTCUSDT'}
                tokenSeparators={[',', ' ']}
                style={{ width: 260 }}
                maxTagCount={4}
                options={['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT'].map((s) => ({ value: s, label: s }))}
              />
            </Tooltip>
          </span>
          <span>
            回看天数{' '}
            <InputNumber value={days} min={1} max={90} onChange={(v) => setDays(v || 30)} style={{ width: 80 }} />
          </span>
        </Space>
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12, marginRight: 12 }}>
            变体预设（与基线=生产语义配对对比）
          </Typography.Text>
          <Checkbox checked={card05} onChange={(e) => setCard05(e.target.checked)}>
            CARD-05 提前保本（MFE≥0.8R 武装）
          </Checkbox>
          <Checkbox checked={card06} onChange={(e) => setCard06(e.target.checked)} style={{ marginLeft: 12 }}>
            CARD-06 TP0 微止盈（0.5R 出 25%）
          </Checkbox>
          <Checkbox checked={atrScan} onChange={(e) => setAtrScan(e.target.checked)} style={{ marginLeft: 12 }}>
            ATR 跟进 k 扫描（2.0 / 2.5）
          </Checkbox>
        </div>
        <Space wrap size="middle" style={{ marginTop: 8 }}>
          <span>单笔风险额 U <InputNumber value={riskUsdt} min={1} max={10000} onChange={(v) => setRiskUsdt(v || 100)} style={{ width: 90 }} /></span>
          <span>在跑上限 <InputNumber value={maxOpen} min={1} max={50} onChange={(v) => setMaxOpen(v || 5)} style={{ width: 70 }} /></span>
          <span>同向上限 <InputNumber value={sameCap} min={0} max={maxOpen} onChange={(v) => setSameCap(v ?? 0)} style={{ width: 70 }} /></span>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {source === 'scan'
              ? '扫描重放：对选定币滚动跑生产扫描管线（6+1票→触发确认），假想入场三价由结构推导'
              : '历史建议单：采样 AI 建议单 + veto 否决单 + 真实单的三价快照回放'}
          </Typography.Text>
        </Space>
      </Card>

      {running && (
        <Card size="small"><Space><Spin size="small" /> 采样与变体重放中（{running.params.source === 'scan' ? '扫描重放' : '历史建议单'} · {running.params.days} 天）…</Space></Card>
      )}

      {result && (result.status === 'DONE' || result.status === 'FAILED') && (
        <Spin spinning={recLoading}>
          {result.status === 'FAILED' ? (
            <Alert type="error" showIcon message="回测失败" description={result.error} />
          ) : summary && base && (
            <>
              <Card size="small" title={`回测结果（${summary.engine_version} · ${summary.signals.n} 个信号 · 回看 ${summary.data_range.days} 天）`}>
                <Row gutter={[8, 8]}>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="基线期望（R/笔）" value={fmt(base.exp_r)} color={rColor(base.exp_r)}
                      sub={`decided ${base.decided} · 未决 ${base.excluded_open}`} />
                  </Col>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="基线胜率" value={`${((base.win_rate ?? 0) * 100).toFixed(1)}%`}
                      sub={`全损率 ${((base.full_loss_rate ?? 0) * 100).toFixed(1)}% · 均持 ${fmt(base.avg_bars, 1)} 根`} />
                  </Col>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="基线累计 R" value={fmt(base.sum_r)} color={rColor(base.sum_r)}
                      sub={Object.entries(base.outcome_dist).map(([k, v]) => `${OUTCOME_LABEL[k] ?? k}:${v}`).join(' · ') || '-'} />
                  </Col>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="组合期末（R）" value={fmt(basePf?.final_equity_r)} color={rColor(basePf?.final_equity_r)}
                      sub={`≈ ${fmt(basePf?.final_equity_usdt, 0)} U（风险额 ${fmt(result.params.portfolio.risk_usdt, 0)} U/笔）`} />
                  </Col>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="组合最大回撤" value={`${fmt(basePf?.max_dd_r)} R`}
                      sub={`模拟 ${basePf?.trades ?? 0} 笔 · 拒单 ${basePf ? basePf.rejected.slot_full + basePf.rejected.same_dir_cap : 0}`} />
                  </Col>
                  <Col xs={12} md={8} xl={4}>
                    <Kpi title="最优变体（Δ期望）" value={(() => {
                      const ds = (summary.deltas ?? []).filter((d) => d.delta_exp_r !== null)
                      if (ds.length === 0) return '-'
                      const best = ds.reduce((a, b) => ((b.delta_exp_r ?? 0) > (a.delta_exp_r ?? 0) ? b : a))
                      return `+${fmt(best.delta_exp_r)} R`
                    })()}
                      sub={`${(summary.deltas ?? []).length} 个变体配对对比`} />
                  </Col>
                </Row>
              </Card>

              {/* 变体对比表 */}
              <Card size="small" title="变体对比（基线 = 生产语义；delta 只计双双终局样本）" style={{ marginTop: 12 }}>
                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  dataSource={summary.variants}
                  columns={[
                    { title: '变体', dataIndex: 'name', width: 150, render: (v: string) => v === 'baseline' ? <Tag color="blue">基线</Tag> : v },
                    { title: '终局', width: 70, render: (_: unknown, r) => `${r.decided}/${r.n}` },
                    { title: '未决', width: 60, render: (_: unknown, r) => r.excluded_open || '-' },
                    { title: '期望 R', width: 90, render: (_: unknown, r) => <b style={{ color: rColor(r.exp_r) }}>{fmt(r.exp_r)}</b> },
                    { title: '胜率', width: 80, render: (_: unknown, r) => r.win_rate === null ? '-' : `${(r.win_rate * 100).toFixed(1)}%` },
                    { title: '全损率', width: 80, render: (_: unknown, r) => r.full_loss_rate === null ? '-' : `${(r.full_loss_rate * 100).toFixed(1)}%` },
                    { title: '累计 R', width: 90, render: (_: unknown, r) => fmt(r.sum_r) },
                    { title: 'Δ期望 R', width: 90, render: (_: unknown, r) => {
                        const d = deltaByName.get(r.name)
                        if (!d || d.delta_exp_r === null) return '-'
                        return <span style={{ color: rColor(d.delta_exp_r), fontWeight: 600 }}>{d.delta_exp_r >= 0 ? '+' : ''}{fmt(d.delta_exp_r)}</span>
                      } },
                    { title: 'Δ胜率', width: 80, render: (_: unknown, r) => {
                        const d = deltaByName.get(r.name)
                        if (!d || d.delta_win_rate === null) return '-'
                        return <span style={{ color: rColor(d.delta_win_rate) }}>{d.delta_win_rate >= 0 ? '+' : ''}{(d.delta_win_rate * 100).toFixed(1)}%</span>
                      } },
                    { title: '配对数', width: 70, render: (_: unknown, r) => deltaByName.get(r.name)?.n_pairs ?? '-' },
                    { title: '结局迁移（top）', render: (_: unknown, r) => {
                        const d = deltaByName.get(r.name)
                        if (!d || d.migration_top.length === 0) return '-'
                        return d.migration_top.slice(0, 3).map((m, i) => (
                          <Tag key={i} style={{ marginBottom: 2 }}>
                            {OUTCOME_LABEL[m.from] ?? m.from}→{OUTCOME_LABEL[m.to] ?? m.to} ×{m.n}
                          </Tag>
                        ))
                      } },
                  ]}
                />
              </Card>

              {/* 组合资金曲线：变体切换 + R/USDT 双口径 */}
              <Card size="small" title="组合资金曲线（固定风险额 · 在跑/同向上限过闸）" style={{ marginTop: 12 }}
                extra={
                  <Space>
                    <Select value={curveVariant} onChange={setCurveVariant} size="small" style={{ width: 160 }}
                      options={(summary.portfolio ?? []).map((p) => ({ value: p.name, label: p.name }))} />
                    <Segmented size="small" value={curveMode} onChange={(v) => setCurveMode(v as 'r' | 'usdt')}
                      options={[{ value: 'r', label: 'R' }, { value: 'usdt', label: 'USDT' }]} />
                  </Space>
                }>
                {pf && pf.daily_series.length > 1 ? (
                  <EquityChart series={pf.daily_series} mode={curveMode} />
                ) : (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>（无逐日数据）</Typography.Text>
                )}
                {pf && (
                  <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
                    <Descriptions.Item label="期末">{fmt(pf.final_equity_r)} R（{fmt(pf.final_equity_usdt, 0)} U）</Descriptions.Item>
                    <Descriptions.Item label="最大回撤">{fmt(pf.max_dd_r)} R</Descriptions.Item>
                    <Descriptions.Item label="满仓拒单">{pf.rejected.slot_full}</Descriptions.Item>
                    <Descriptions.Item label="同向拒单">{pf.rejected.same_dir_cap}</Descriptions.Item>
                  </Descriptions>
                )}
              </Card>

              {/* 基线订单明细 */}
              {orders.length > 0 && (
                <Card size="small" title={`基线订单明细（${orders.length} 笔${result.truncated ? '，超长已截断' : ''}）`} style={{ marginTop: 12 }}>
                  <Table
                    size="small"
                    dataSource={orders}
                    rowKey={(o, i) => `${o.symbol}-${o.entry_time_ms}-${i}`}
                    pagination={{ pageSize: 20, size: 'small' }}
                    columns={[
                      { title: '币', dataIndex: 'symbol', width: 90 },
                      { title: '方向', dataIndex: 'direction', width: 70, render: (v) => v === 'long' ? <Tag color="green">多</Tag> : <Tag color="red">空</Tag> },
                      { title: '来源', dataIndex: 'src', width: 90, render: (v) => SRC_LABEL[v] ?? v ?? '-' },
                      { title: '入场', dataIndex: 'entry', width: 90, render: (v) => fmt(v, 4) },
                      { title: '止损', dataIndex: 'stop', width: 90, render: (v) => fmt(v, 4) },
                      { title: '结局', dataIndex: 'outcome', width: 100, render: (v) => {
                          const color = v === 'loss' ? 'red' : v.startsWith('win') ? 'green' : v === 'be_exit' ? 'orange' : 'default'
                          return <Tag color={color}>{OUTCOME_LABEL[v] ?? v}</Tag>
                        } },
                      { title: '净 R', dataIndex: 'r', width: 80, render: (v) => <b style={{ color: rColor(v) }}>{v === null ? '-' : fmt(v)}</b> },
                      { title: '根数', dataIndex: 'bars', width: 60 },
                      { title: '时间', dataIndex: 'entry_time_ms', width: 140, render: (v) => new Date(v).toLocaleString('zh-CN', { hour12: false }).slice(0, 16) },
                    ]}
                  />
                </Card>
              )}
            </>
          )}
        </Spin>
      )}

      {/* 历史 */}
      {history.length > 0 && (
        <Card size="small" title="历史回测" style={{ marginTop: 12 }}>
          <Table
            size="small"
            dataSource={history}
            rowKey="id"
            pagination={false}
            onRow={(rec) => ({
              onClick: () => openHistory(rec.id),
              style: { cursor: 'pointer', background: result?.id === rec.id ? '#e6f4ff' : undefined },
            })}
            columns={[
              { title: '样本源', width: 90, render: (_: unknown, r: FuturesBacktestListItem) => (r.params?.source === 'history' ? '历史单' : '扫描重放') },
              { title: '币种', width: 160, render: (_: unknown, r: FuturesBacktestListItem) => (r.params?.symbols || []).length ? (r.params.symbols.map((s) => s.replace('USDT', '')).join('/') ) : '全部' },
              { title: '天数', width: 60, render: (_: unknown, r: FuturesBacktestListItem) => r.params?.days },
              { title: '变体', width: 60, render: (_: unknown, r: FuturesBacktestListItem) => r.variants_n },
              { title: '信号', width: 60, render: (_: unknown, r: FuturesBacktestListItem) => r.signals ?? '-' },
              { title: '状态', width: 80, render: (_: unknown, r: FuturesBacktestListItem) =>
                r.status === 'DONE' ? <Tag color="green">完成</Tag> : r.status === 'FAILED' ? <Tag color="red">失败</Tag> : <Tag color="blue">运行中</Tag> },
              { title: '时间', width: 150, render: (_: unknown, r: FuturesBacktestListItem) => (r.created_at || '').replace('T', ' ').slice(0, 16) },
            ]}
          />
        </Card>
      )}

      {!running && !result && history.length === 0 && (
        <Card size="small" style={{ marginTop: 12 }}>
          <Empty description="暂无回测记录——选好币种与变体预设点「开始回测」（假设验证从等影子数周 → 分钟级）" />
        </Card>
      )}
    </div>
  )
}
