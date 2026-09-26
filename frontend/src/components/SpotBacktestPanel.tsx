/**
 * 现货回测面板（docs/18 §10，P1）：任选历史区间快速回测
 * - 表单：币种多选（缺省三币）+ 日期区间 + 预算 + 可选参数覆盖
 * - 结果：汇总卡（净增币/超额/回撤/笔数）+ 权益曲线（含定投基准换算线）+ 订单明细表
 * - 提交后轮询（纯计算秒级~十秒级）；历史回测列表可点击回看
 * - 引擎与实盘同一 evaluate_day（§10.2 同构铁律），唯一差异 = 跳过 AI（§10.4）
 */
import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Empty,
  InputNumber,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ExperimentOutlined, ReloadOutlined, DatabaseOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { spotApi, type KlineCacheStatus } from '../api/spot'
import { schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import type { BacktestOrder, BacktestRecord, BacktestSeriesPoint } from '../types/spot'

const SYMBOL_OPTS = [
  { value: 'BTCUSDT', label: 'BTC' },
  { value: 'ETHUSDT', label: 'ETH' },
  { value: 'BNBUSDT', label: 'BNB' },
]
const ASSET = (s: string) => s.replace('USDT', '')

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '-'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

// 权益曲线（2026-09-26 重设计）：面积图 + 网格刻度 + 悬浮十字线/tooltip + 末端数值 + 图例。
// 蓝实线=系统权益（现金+持仓市值），灰虚线=已投入本金（含费累计买入支出）
function EquityChart({ series }: { series: BacktestSeriesPoint[] }) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const [hover, setHover] = useState<number | null>(null)
  const W = 900
  const H = 280
  const PAD = { l: 56, r: 60, t: 16, b: 26 }
  if (series.length < 2) {
    return <Typography.Text type="secondary" style={{ fontSize: 12 }}>数据点不足，无法绘制曲线</Typography.Text>
  }
  const eq = series.map((p) => p.equity)
  const inv = series.map((p) => p.invested)
  const all = [...eq, ...inv, 0]
  const min = Math.min(...all)
  const max = Math.max(...all)
  const pad = (max - min) * 0.06 || Math.max(1, max * 0.02)
  const lo = Math.max(0, min - pad)
  const hi = max + pad
  const x = (i: number) => PAD.l + (i / (series.length - 1)) * (W - PAD.l - PAD.r)
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo || 1)) * (H - PAD.t - PAD.b)
  const line = (vals: number[]) =>
    vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area = `${line(eq)} L${x(eq.length - 1).toFixed(1)},${y(lo).toFixed(1)} L${x(0).toFixed(1)},${y(lo).toFixed(1)} Z`
  const gridVals = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3)

  const onMove = (e: ReactMouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * W
    const i = Math.round(((px - PAD.l) / (W - PAD.l - PAD.r)) * (series.length - 1))
    if (i >= 0 && i < series.length) setHover(i)
  }
  const hp = hover !== null ? series[hover] : null
  const hoverPnl = hp ? hp.equity - hp.invested : 0

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ position: 'absolute', right: 4, top: -4, fontSize: 12, color: '#8c8c8c' }}>
        <span style={{ color: '#1677ff' }}>—</span> 系统权益
        <span style={{ marginLeft: 12, color: '#8c8c8c' }}>┈</span> 已投入本金
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: '100%', height: H, display: 'block' }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#1677ff" stopOpacity={0.18} />
            <stop offset="100%" stopColor="#1677ff" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        {gridVals.map((v, k) => (
          <g key={k}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="#f0f0f0" />
            <text x={PAD.l - 6} y={y(v) + 3} fontSize={10} fill="#8c8c8c" textAnchor="end">{fmt(v, 0)}</text>
          </g>
        ))}
        <path d={area} fill="url(#eqFill)" />
        <path d={line(inv)} fill="none" stroke="#8c8c8c" strokeWidth={1} strokeDasharray="4 3" />
        <path d={line(eq)} fill="none" stroke="#1677ff" strokeWidth={1.6} />
        {/* 末端数值标注 */}
        <circle cx={x(eq.length - 1)} cy={y(eq[eq.length - 1])} r={3} fill="#1677ff" />
        <text x={W - PAD.r + 6} y={y(eq[eq.length - 1]) + 3} fontSize={10} fill="#1677ff">{fmt(eq[eq.length - 1], 0)}</text>
        <text x={W - PAD.r + 6} y={y(inv[inv.length - 1]) + 3} fontSize={10} fill="#8c8c8c">{fmt(inv[inv.length - 1], 0)}</text>
        {/* x 轴刻度：首/中/末 */}
        {[0, Math.floor((series.length - 1) / 2), series.length - 1].map((i, k) => (
          <text
            key={k}
            x={x(i)}
            y={H - 8}
            fontSize={10}
            fill="#8c8c8c"
            textAnchor={k === 0 ? 'start' : k === 2 ? 'end' : 'middle'}
          >
            {series[i].date.slice(5)}
          </text>
        ))}
        {/* 悬浮十字线 */}
        {hp && hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={H - PAD.b} stroke="#bfbfbf" strokeDasharray="3 3" />
            <circle cx={x(hover)} cy={y(eq[hover])} r={3.5} fill="#1677ff" stroke="#fff" />
          </g>
        )}
      </svg>
      {/* 悬浮明细 tooltip（后半段自动左翻防溢出） */}
      {hp && hover !== null && (
        <div
          style={{
            position: 'absolute',
            left: `${(x(hover) / W) * 100}%`,
            top: 8,
            transform: hover > series.length * 0.6 ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)',
            background: '#fff',
            border: '1px solid #eee',
            borderRadius: 6,
            boxShadow: '0 2px 8px rgba(0,0,0,0.12)',
            padding: '6px 10px',
            fontSize: 12,
            lineHeight: '20px',
            pointerEvents: 'none',
            minWidth: 160,
            zIndex: 2,
          }}
        >
          <div style={{ fontWeight: 600 }}>{hp.date}</div>
          <div>系统权益 <b style={{ color: '#1677ff' }}>{fmt(hp.equity)}</b> U</div>
          <div>已投入 {fmt(hp.invested)} U</div>
          <div>
            浮动盈亏{' '}
            <b style={{ color: schemeValueColor(hoverPnl, colorScheme) }}>
              {hoverPnl >= 0 ? '+' : ''}{fmt(hoverPnl)} U
            </b>
          </div>
          {Object.entries(hp.coin_qty).length > 0 && (
            <div style={{ color: '#8c8c8c' }}>
              持仓 {Object.entries(hp.coin_qty).map(([s, q]) => `${ASSET(s)} ${fmt(q as number, 4)}`).join(' · ')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// KPI 紧凑卡：标题 + 大数字（可含彩色 span）+ 副注
function Kpi({ title, value, color, sub }: {
  title: string
  value: React.ReactNode
  color?: string
  sub?: React.ReactNode
}) {
  return (
    <Card size="small" style={{ background: '#fafafa' }} styles={{ body: { padding: '8px 12px' } }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>{title}</Typography.Text>
      <div style={{ fontSize: 20, fontWeight: 600, color, lineHeight: '28px' }}>{value}</div>
      {sub != null && <Typography.Text type="secondary" style={{ fontSize: 11 }}>{sub}</Typography.Text>}
    </Card>
  )
}

export default function SpotBacktestPanel() {
  const [symbols, setSymbols] = useState<string[]>(SYMBOL_OPTS.map((o) => o.value))
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(2, 'year'),
    dayjs(),
  ])
  const [budget, setBudget] = useState(3000)
  const [overrideDca, setOverrideDca] = useState(false)
  const [dcaEnabled, setDcaEnabled] = useState<boolean | null>(null)
  const [perOrder, setPerOrder] = useState<number | null>(null)

  const [running, setRunning] = useState<BacktestRecord | null>(null)
  const [result, setResult] = useState<BacktestRecord | null>(null)
  const [history, setHistory] = useState<BacktestRecord[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [cache, setCache] = useState<KlineCacheStatus | null>(null)
  const [backfilling, setBackfilling] = useState(false)
  const [recLoading, setRecLoading] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const cachePollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadHistory = useCallback(() => {
    spotApi.listBacktests(20).then(setHistory).catch(() => {})
  }, [])

  // 点击历史回测行：列表接口不含大 JSON（daily_series/orders），须拉全量详情渲染曲线
  const openHistory = useCallback(async (rec: BacktestRecord) => {
    if (rec.status === 'RUNNING') return
    setRecLoading(true)
    try {
      setResult(await spotApi.getBacktest(rec.id))
    } catch {
      message.error('获取回测详情失败')
    } finally {
      setRecLoading(false)
    }
  }, [])

  const loadCache = useCallback(() => {
    spotApi.klineCacheStatus().then(setCache).catch(() => {})
  }, [])

  // 手动触发 K线全量回填（免费公开行情；提交后轮询缓存状态直到日K就绪）
  const triggerBackfill = async () => {
    setBackfilling(true)
    try {
      await spotApi.klineBackfill()
      message.info('回填任务已提交（正式网公开行情，免费无需 key，约数秒~数十秒）')
      let ticks = 0
      cachePollRef.current = setInterval(async () => {
        ticks += 1
        try {
          const s = await spotApi.klineCacheStatus()
          setCache(s)
          const ready = Object.values(s).every((iv) => (iv['1d']?.bars ?? 0) >= 400)
          if (ready || ticks > 30) {
            if (cachePollRef.current) clearInterval(cachePollRef.current)
            setBackfilling(false)
            if (ready) message.success('K线数据已就绪，可开始回测')
            else message.warning('回填仍在进行或未达标，可稍后刷新查看')
          }
        } catch {
          /* 轮询错误静默，下轮重试 */
        }
      }, 3000)
    } catch {
      setBackfilling(false)
      message.error('触发回填失败')
    }
  }

  useEffect(() => {
    loadHistory()
    loadCache()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      if (cachePollRef.current) clearInterval(cachePollRef.current)
    }
  }, [loadHistory, loadCache])

  const submit = async () => {
    if (!range) return
    setSubmitting(true)
    try {
      const body = {
        symbols,
        start: range[0].format('YYYY-MM-DD'),
        end: range[1].format('YYYY-MM-DD'),
        budget,
        ...(overrideDca ? { dca_enabled: dcaEnabled ?? undefined } : {}),
        ...(perOrder ? { per_order_usdt: perOrder } : {}),
      }
      const { id } = await spotApi.createBacktest(body)
      const rec = await spotApi.getBacktest(id)
      setRunning(rec)
      setResult(null)
      pollRef.current = setInterval(async () => {
        try {
          const r = await spotApi.getBacktest(id)
          if (r.status !== 'RUNNING') {
            if (pollRef.current) clearInterval(pollRef.current)
            setRunning(null)
            setResult(r)
            if (r.status === 'DONE') message.success('回测完成')
            else message.error(`回测失败：${r.error || '未知错误'}`)
            loadHistory()
          }
        } catch {
          /* 轮询错误静默，下轮重试 */
        }
      }, 1500)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '提交回测失败')
    } finally {
      setSubmitting(false)
    }
  }

  // 结果展示派生值：净盈亏 = 期末权益 − 预算（现金守恒下恰为累计已实现+未实现盈亏净额）
  const colorScheme = useScanStore((st) => st.colorScheme)
  const summary = result?.status === 'DONE' ? result.summary ?? null : null
  const equityEnd = summary?.equity_curve_end ?? 0
  const pnl = summary ? equityEnd - summary.budget : 0
  const lastSeriesHoldings = result?.daily_series?.length
    ? Object.entries(result.daily_series[result.daily_series.length - 1].coin_qty)
        .map(([k, v]) => `${ASSET(k)} ${fmt(v as number, 6)}`).join(' · ') || '-'
    : '-'
  const orders: BacktestOrder[] = result?.orders || []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>
      {/* 表单 */}
      <Card size="small" title={<><ExperimentOutlined /> 回测参数</>} extra={
        <Space>
          <Tooltip title="免费公开行情全量回填（与交易环境无关）；缓存不足时回测也会自动回补">
            <Button size="small" icon={<DatabaseOutlined />} loading={backfilling} onClick={triggerBackfill}>
              补全行情
            </Button>
          </Tooltip>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => { loadHistory(); loadCache() }}>刷新历史</Button>
          <Button type="primary" size="small" loading={submitting} disabled={!!running} onClick={submit}>
            {running ? '回测中…' : '开始回测'}
          </Button>
        </Space>
      }>
        <Space wrap size="middle">
          <span>
            币种{' '}
            <Select
              mode="multiple"
              value={symbols}
              onChange={setSymbols}
              options={SYMBOL_OPTS}
              style={{ width: 220 }}
              maxTagCount={3}
            />
          </span>
          <span>
            区间{' '}
            <DatePicker.RangePicker
              value={range}
              onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
              allowClear={false}
            />
          </span>
          <span>
            预算 U{' '}
            <InputNumber value={budget} min={100} step={500} onChange={(v) => setBudget(v || 3000)} style={{ width: 100 }} />
          </span>
          <span>
            <Tooltip title="可选：覆盖单份金额；缺省用策略配置">
              单份 U <InputNumber
                value={perOrder}
                min={10}
                step={10}
                placeholder="缺省"
                onChange={(v) => setPerOrder(v)}
                style={{ width: 90 }}
              />
            </Tooltip>
          </span>
          <span>
            <Tooltip title="可选：覆盖周度定投开关（回测基准=定投，关闭后基准按信号单金额）">
              定投 <Switch size="small" checked={overrideDca} onChange={setOverrideDca} />
            </Tooltip>
            {overrideDca && (
              <Select
                size="small"
                value={dcaEnabled === null ? undefined : dcaEnabled}
                placeholder="开/关"
                style={{ width: 80, marginLeft: 4 }}
                options={[{ value: true, label: '开' }, { value: false, label: '关' }]}
                onChange={(v) => setDcaEnabled(v)}
              />
            )}
          </span>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          {cache
            ? '行情缓存：' + SYMBOL_OPTS.map((o) => {
                const d = cache[o.value]?.['1d']
                if (!d || !d.bars) return `${o.label} 无缓存`
                return `${o.label} ${d.bars}根（${d.start}~${d.end}）${d.fresh ? '✓' : '数据偏旧'}`
              }).join(' · ')
            : '行情缓存：加载中…'}
        </Typography.Text>
      </Card>

      {running && (
        <Card size="small"><Space><Spin size="small" /> 日历重放中（{running.params.start} ~ {running.params.end}）…</Space></Card>
      )}

      {/* 结果：KPI 6 卡 + 权益曲线 + 持仓/回款条（历史回看与实时结果共用，Spin 覆盖加载态） */}
      {result && (result.status === 'DONE' || result.status === 'FAILED') && (
        <Spin spinning={recLoading}>
          {result.status === 'FAILED' ? (
            <Alert type="error" showIcon message="回测失败" description={result.error} />
          ) : summary && (
            <Card size="small" title={`回测结果 ${summary.start} ~ ${summary.end}（${summary.bars} 个交易日 · 预算 ${fmt(summary.budget)} U）`}>
              <Row gutter={[8, 8]}>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="净盈亏（期末−预算）"
                    value={`${pnl >= 0 ? '+' : ''}${fmt(pnl)}`}
                    color={schemeValueColor(pnl, colorScheme)}
                    sub={`总收益率 ${(pnl * 100 / (summary.budget || 1)).toFixed(2)}% · 含已实现`}
                  />
                </Col>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="期末权益"
                    value={`${fmt(equityEnd)} U`}
                    sub={`现金 ${fmt(summary.cash_left)} U · 市值 ${fmt(summary.total_market_value)} U`}
                  />
                </Col>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="净增币"
                    value={`${fmt(summary.total_qty, 6)} ${summary.coin_unit}`}
                    sub={`期末市值 ${fmt(summary.total_market_value)} U`}
                  />
                </Col>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="vs 定投基准超额"
                    value={Object.entries(summary.excess_qty).map(([sym, q]) => (
                      <span key={sym} style={{ marginRight: 8, fontSize: 14, color: q >= 0 ? '#389e0d' : '#cf1322' }}>
                        {q >= 0 ? '+' : ''}{fmt(q, 4)} {ASSET(sym)}
                      </span>
                    ))}
                    sub={`基准净增 ${fmt(summary.benchmark.total_qty, 6)}（投入 ${fmt(summary.benchmark.invested)} U）`}
                  />
                </Col>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="最大回撤（权益）"
                    value={`${(summary.max_drawdown * 100).toFixed(2)}%`}
                    color={summary.max_drawdown < -0.2 ? '#cf1322' : '#fa8c16'}
                    sub={`累计投入 ${fmt(summary.invested)} U（含费）`}
                  />
                </Col>
                <Col xs={12} md={8} xl={4}>
                  <Kpi
                    title="模拟成交"
                    value={`${summary.orders_count} 笔`}
                    sub={Object.entries(summary.action_dist).map(([k, v]) => `${k}:${v}`).join(' · ')}
                  />
                </Col>
              </Row>
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  权益曲线（悬浮查看每日权益/投入/持仓明细；AI 复核在回测中跳过=满额执行）
                </Typography.Text>
                {result.daily_series && result.daily_series.length > 1 ? (
                  <EquityChart series={result.daily_series} />
                ) : (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>（无逐日数据）</Typography.Text>
                )}
              </div>
              <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
                <Descriptions.Item label="系统持仓">{lastSeriesHoldings}</Descriptions.Item>
                <Descriptions.Item label="基准持仓">{Object.entries(summary.benchmark.qty_per_symbol).map(([k, v]) => `${ASSET(k)} ${fmt(v, 6)}`).join(' · ')}</Descriptions.Item>
                <Descriptions.Item label="已实现回款">{fmt(summary.total_realized)} U</Descriptions.Item>
                <Descriptions.Item label="期末权益">{fmt(equityEnd)} U</Descriptions.Item>
              </Descriptions>
            </Card>
          )}
        </Spin>
      )}

      {/* 订单明细 */}
      {orders.length > 0 && (
        <Card size="small" title={`模拟订单明细（${orders.length} 笔，最多展示 200）`}>
          <Table
            size="small"
            dataSource={orders.slice(0, 200)}
            rowKey={(o) => `${o.date}-${o.symbol}-${o.side}-${o.qty}`}
            pagination={{ pageSize: 20, size: 'small' }}
            columns={[
              { title: '日期', dataIndex: 'date', width: 100 },
              { title: '币', dataIndex: 'symbol', width: 60, render: (v) => ASSET(v) },
              { title: '方向', dataIndex: 'side', width: 60, render: (v) => (v === 'BUY' ? <Tag color="green">买</Tag> : <Tag color="red">卖</Tag>) },
              { title: '触发', dataIndex: 'trigger', width: 70, render: (v) => (v === 'dca' ? <Tag color="blue">定投</Tag> : <Tag color="purple">信号</Tag>) },
              { title: '价格', dataIndex: 'price', width: 90, render: (v) => fmt(v, 2) },
              { title: '数量', dataIndex: 'qty', width: 110, render: (v) => fmt(v, 6) },
              { title: '金额 U', dataIndex: 'quote', width: 80, render: (v) => fmt(v) },
              { title: '评分', dataIndex: 'score', width: 60 },
            ]}
          />
        </Card>
      )}

      {/* 历史回测（点击行回看全量结果：曲线/订单由详情接口拉取） */}
      {history.length > 0 && (
        <Card size="small" title="历史回测">
          <Table
            size="small"
            dataSource={history}
            rowKey="id"
            pagination={false}
            onRow={(rec) => ({
              onClick: () => openHistory(rec),
              style: { cursor: 'pointer', background: result?.id === rec.id ? '#e6f4ff' : undefined },
            })}
            columns={[
              { title: '区间', width: 180, render: (_: unknown, r: BacktestRecord) => `${r.params?.start} ~ ${r.params?.end}` },
              { title: '币种', width: 140, render: (_: unknown, r: BacktestRecord) => (r.params?.symbols || []).map(ASSET).join('/') },
              { title: '状态', width: 90, render: (_: unknown, r: BacktestRecord) =>
                r.status === 'DONE' ? <Tag color="green">完成</Tag> : r.status === 'FAILED' ? <Tag color="red">失败</Tag> : <Tag color="blue">运行中</Tag> },
              { title: '笔数', width: 70, render: (_: unknown, r: BacktestRecord) => r.summary?.orders_count ?? '-' },
              { title: '超额（首币）', render: (_: unknown, r: BacktestRecord) => {
                  if (!r.summary) return '-'
                  const e = Object.values(r.summary.excess_qty)[0]
                  return e === undefined ? '-' : `${e >= 0 ? '+' : ''}${fmt(e, 6)}`
                } },
              { title: '最大回撤', width: 90, render: (_: unknown, r: BacktestRecord) => (r.summary ? `${(r.summary.max_drawdown * 100).toFixed(1)}%` : '-') },
              { title: '时间', width: 150, render: (_: unknown, r: BacktestRecord) => (r.created_at || '').replace('T', ' ').slice(0, 16) },
            ]}
          />
        </Card>
      )}

      {!running && !result && history.length === 0 && (
        <Card size="small"><Empty description="暂无回测记录——选好区间点「开始回测」（引擎与实盘同一套规则，仅跳过 AI）" /></Card>
      )}
    </div>
  )
}
