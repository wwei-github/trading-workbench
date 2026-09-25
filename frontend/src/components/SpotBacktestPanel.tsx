/**
 * 现货回测面板（docs/18 §10，P1）：任选历史区间快速回测
 * - 表单：币种多选（缺省三币）+ 日期区间 + 预算 + 可选参数覆盖
 * - 结果：汇总卡（净增币/超额/回撤/笔数）+ 权益曲线（含定投基准换算线）+ 订单明细表
 * - 提交后轮询（纯计算秒级~十秒级）；历史回测列表可点击回看
 * - 引擎与实盘同一 evaluate_day（§10.2 同构铁律），唯一差异 = 跳过 AI（§10.4）
 */
import { useCallback, useEffect, useRef, useState } from 'react'
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

// 轻量 SVG 折线：权益曲线（蓝） vs 定投基准（灰虚线，同预算等额定投到末日的换算市值）
function EquityChart({ series }: { series: BacktestSeriesPoint[] }) {
  const W = 860
  const H = 220
  const PAD = { l: 46, r: 8, t: 10, b: 22 }
  if (series.length < 2) return null
  const eq = series.map((p) => p.equity)
  const min = Math.min(...eq)
  const max = Math.max(...eq)
  const lo = Math.max(0, min - (max - min) * 0.05)
  const hi = max + (max - min) * 0.05 || max + 1
  const x = (i: number) => PAD.l + (i / (series.length - 1)) * (W - PAD.l - PAD.r)
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo || 1)) * (H - PAD.t - PAD.b)
  const path = (vals: number[]) => vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  // 定投基准市值线：invested 累计（保守近似——不计币价波动，仅作参考底稿）
  const invested = series.map((p) => p.invested)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 220 }}>
      <path d={path(invested)} fill="none" stroke="#8c8c8c" strokeWidth={1} strokeDasharray="4 3" />
      <path d={path(eq)} fill="none" stroke="#1677ff" strokeWidth={1.6} />
      <text x={PAD.l} y={H - 6} fontSize={10} fill="#8c8c8c">{series[0].date}</text>
      <text x={W - PAD.r} y={H - 6} fontSize={10} fill="#8c8c8c" textAnchor="end">{series[series.length - 1].date}</text>
      <text x={4} y={PAD.t + 8} fontSize={10} fill="#8c8c8c">{fmt(hi, 0)}</text>
      <text x={4} y={H - PAD.b} fontSize={10} fill="#8c8c8c">{fmt(lo, 0)}</text>
    </svg>
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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const cachePollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadHistory = useCallback(() => {
    spotApi.listBacktests(20).then(setHistory).catch(() => {})
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

  const s = result?.status === 'DONE' ? result.summary : null
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

      {result?.status === 'FAILED' && (
        <Alert type="error" showIcon message="回测失败" description={result.error} />
      )}

      {/* 汇总 */}
      {s && (
        <Card size="small" title={`回测结果 ${s.start} ~ ${s.end}（${s.bars} 个交易日）`}>
          <Row gutter={[12, 12]}>
            <Col span={6}>
              <Card size="small" style={{ background: '#fafafa' }}>
                <Typography.Text type="secondary">系统净增币</Typography.Text>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{fmt(s.total_qty, 6)} <span style={{ fontSize: 12 }}>{s.coin_unit}</span></div>
                <Typography.Text type="secondary">期末市值 {fmt(s.total_market_value)} U · 剩余现金 {fmt(s.cash_left)} U</Typography.Text>
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small" style={{ background: '#fafafa' }}>
                <Typography.Text type="secondary">vs 定投基准超额</Typography.Text>
                <div style={{ fontSize: 22, fontWeight: 600 }}>
                  {Object.entries(s.excess_qty).map(([sym, q]) => (
                    <span key={sym} style={{ marginRight: 8, color: q >= 0 ? '#389e0d' : '#cf1322' }}>
                      {q >= 0 ? '+' : ''}{fmt(q, 6)} {ASSET(sym)}
                    </span>
                  ))}
                </div>
                <Typography.Text type="secondary">基准净增 {fmt(s.benchmark.total_qty, 6)}（投入 {fmt(s.benchmark.invested)} U）</Typography.Text>
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small" style={{ background: '#fafafa' }}>
                <Typography.Text type="secondary">最大回撤（权益曲线）</Typography.Text>
                <div style={{ fontSize: 22, fontWeight: 600, color: s.max_drawdown < -0.2 ? '#cf1322' : '#fa8c16' }}>
                  {(s.max_drawdown * 100).toFixed(2)}%
                </div>
                <Typography.Text type="secondary">投入 {fmt(s.invested)} U</Typography.Text>
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small" style={{ background: '#fafafa' }}>
                <Typography.Text type="secondary">模拟成交</Typography.Text>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{s.orders_count} 笔</div>
                <Typography.Text type="secondary">
                  {Object.entries(s.action_dist).map(([k, v]) => `${k}:${v}`).join(' · ')}
                </Typography.Text>
              </Card>
            </Col>
          </Row>
          {result?.daily_series && result.daily_series.length > 1 && (
            <div style={{ marginTop: 12 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                权益曲线（蓝=系统，灰虚线=已投入本金参考线；AI 复核在回测中跳过=满额执行）
              </Typography.Text>
              <EquityChart series={result.daily_series} />
            </div>
          )}
          <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
            <Descriptions.Item label="系统持仓">{Object.entries(result?.daily_series?.[result.daily_series.length - 1]?.coin_qty || {}).map(([k, v]) => `${ASSET(k)} ${fmt(v as number, 6)}`).join(' · ')}</Descriptions.Item>
            <Descriptions.Item label="基准持仓">{Object.entries(s.benchmark.qty_per_symbol).map(([k, v]) => `${ASSET(k)} ${fmt(v, 6)}`).join(' · ')}</Descriptions.Item>
            <Descriptions.Item label="已实现回款">{fmt(s.total_realized)} U</Descriptions.Item>
            <Descriptions.Item label="期末权益">{fmt(result?.summary?.equity_curve_end)} U</Descriptions.Item>
          </Descriptions>
        </Card>
      )}

      {/* 订单明细 */}
      {orders.length > 0 && (
        <Card size="small" title={`模拟订单明细（${orders.length} 笔，最多展示 200）`}>
          <Table
            size="small"
            dataSource={orders.slice(0, 200)}
            rowKey={(o) => `${o.date}-${o.symbol}-${o.side}-${o.qty}`}
            pagination={{ pageSize: 10, size: 'small' }}
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

      {/* 历史回测 */}
      {history.length > 0 && (
        <Card size="small" title="历史回测">
          <Table
            size="small"
            dataSource={history}
            rowKey="id"
            pagination={false}
            onRow={(rec) => ({
              onClick: () => setResult(rec),
              style: { cursor: 'pointer' },
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
