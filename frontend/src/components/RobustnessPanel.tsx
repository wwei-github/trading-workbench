/**
 * 参数稳健性扫描面板（docs/16 P-12）：多重比较折扣尺
 * - 网格变体 × 同一信号集重放（默认出厂网格 12 变体：保本武装 × 微止盈 × ATR 松紧）
 * - PBO（CSCV）：IS 最优变体 OOS 掉中位数以下的组合占比——「山峰 vs 高原」读数
 * - DSR：把「已试 N 个变体」折进显著性的 Sharpe 检验
 * - 纯离线报告：不自动改任何生产参数，拍板走 docs/16 §0 流程
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  InputNumber,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ExperimentOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { robustnessApi } from '../api/robustness'
import type { RobustnessListItem, RobustnessRecord, RobustnessVariant } from '../types/robustness'
import { useScanStore } from '../stores/scanStore'
import { schemeValueColor } from '../utils/scheme'

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '-'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function rColor(v: number | null | undefined): string {
  const scheme = useScanStore.getState().colorScheme
  return schemeValueColor(v ?? 0, scheme)
}

const VERDICT_META: Record<string, { color: string; label: string }> = {
  plateau: { color: 'green', label: '高原（稳健）' },
  peak: { color: 'red', label: '山峰（过拟合风险）' },
  mixed: { color: 'orange', label: '证据不足' },
  insufficient: { color: 'default', label: '样本不足' },
}

/** overrides 压缩文案：be=0.8·tp0=0.5·k=2.5 */
function overridesText(o: Record<string, number | boolean | null>): string {
  const KEY: Record<string, string> = {
    be_trigger_r: 'be', tp0_r: 'tp0', trail_atr_k: 'k',
    trail_swing_bars: 'sw', atr_period: 'ap', tp0_qty_pct: 'q',
    tp0_gate_r: 'g', be_fee_buffer: 'buf',
  }
  return Object.entries(o)
    .map(([k, v]) => `${KEY[k] ?? k}=${v === null ? '—' : v}`)
    .join(' · ')
}

export default function RobustnessPanel() {
  const [source, setSource] = useState<'scan' | 'history'>('scan')
  const [symbols, setSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT'])
  const [days, setDays] = useState(30)
  const [submitting, setSubmitting] = useState(false)
  const [record, setRecord] = useState<RobustnessRecord | null>(null)
  const [history, setHistory] = useState<RobustnessListItem[]>([])
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await robustnessApi.list())
    } catch {
      /* 列表失败静默 */
    }
  }, [])

  const stopPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const startPoll = useCallback(
    (id: string) => {
      stopPoll()
      pollRef.current = setInterval(async () => {
        try {
          const rec = await robustnessApi.get(id)
          if (rec.status !== 'RUNNING') {
            stopPoll()
            setRecord(rec)
            loadHistory()
          }
        } catch {
          stopPoll()
        }
      }, 3000)
    },
    [loadHistory, stopPoll],
  )

  useEffect(() => {
    loadHistory()
    return stopPoll
  }, [loadHistory, stopPoll])

  const submit = async () => {
    if (source === 'scan' && symbols.length === 0) {
      message.warning('扫描重放源必须选择币种')
      return
    }
    setSubmitting(true)
    try {
      const { id } = await robustnessApi.create({ source, symbols, days })
      message.success('稳健性扫描已提交（默认出厂网格 12 变体）')
      setRecord({ id, status: 'RUNNING', params: { source, symbols, days, n_blocks: 8, grid: null }, summary: null, error: null, created_at: null, finished_at: null })
      startPoll(id)
      loadHistory()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      message.error(err.response?.data?.detail ?? '提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  const openRecord = async (id: string) => {
    try {
      const rec = await robustnessApi.get(id)
      setRecord(rec)
      if (rec.status === 'RUNNING') startPoll(id)
    } catch {
      message.error('读取扫描结果失败')
    }
  }

  const s = record?.summary
  const verdict = s?.conclusion ? VERDICT_META[s.conclusion.verdict] : null

  const columns: ColumnsType<RobustnessVariant> = [
    {
      title: '变体', dataIndex: 'name', width: 70,
      render: (v: string, row) => (
        <Space size={4}>
          <span>{v}</span>
          {s?.best_is === v && <Tag color="blue">IS最优</Tag>}
          {s?.best_oos === v && <Tag color="purple">OOS最优</Tag>}
        </Space>
      ),
    },
    {
      title: '参数', dataIndex: 'overrides', ellipsis: true,
      render: (o: Record<string, number | boolean | null>) => (
        <Typography.Text code style={{ fontSize: 12 }}>{overridesText(o)}</Typography.Text>
      ),
    },
    { title: 'decided', dataIndex: 'decided', width: 80, align: 'right', render: (v: number) => fmt(v, 0) },
    {
      title: '胜率', dataIndex: 'win_rate', width: 80, align: 'right',
      render: (v: number | null) => (v === null ? '-' : `${fmt(v * 100, 1)}%`),
    },
    {
      title: '期望R', dataIndex: 'exp_r', width: 90, align: 'right',
      render: (v: number | null) => (
        <span style={{ color: rColor(v), fontWeight: 600 }}>{v === null ? '-' : `${v > 0 ? '+' : ''}${fmt(v, 3)}`}</span>
      ),
      sorter: (a, b) => (a.exp_r ?? -9) - (b.exp_r ?? -9),
      defaultSortOrder: 'descend',
    },
    { title: '累计R', dataIndex: 'sum_r', width: 90, align: 'right', render: (v: number) => <span style={{ color: rColor(v) }}>{v > 0 ? '+' : ''}{fmt(v, 1)}</span> },
    { title: '每笔SR', dataIndex: 'sr', width: 85, align: 'right', render: (v: number) => fmt(v, 3) },
    { title: '偏度', dataIndex: 'skew', width: 75, align: 'right', render: (v: number) => fmt(v, 2) },
    {
      title: 'DSR', dataIndex: 'dsr', width: 85, align: 'right',
      render: (v: number) => (
        <Tag color={v >= 0.9 ? 'green' : v >= 0.5 ? 'orange' : 'red'} style={{ marginInlineEnd: 0 }}>{fmt(v, 3)}</Tag>
      ),
      sorter: (a, b) => a.dsr - b.dsr,
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small" title={<span><ExperimentOutlined /> 参数稳健性扫描（P-12）</span>}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={loadHistory}>刷新</Button>
            <Button type="primary" loading={submitting} onClick={submit}
              disabled={record?.status === 'RUNNING'}>开始扫描</Button>
          </Space>
        }
      >
        <Space wrap size={12}>
          <Segmented
            value={source}
            onChange={(v) => setSource(v as 'scan' | 'history')}
            options={[
              { value: 'scan', label: '扫描重放' },
              { value: 'history', label: '历史建议单' },
            ]}
          />
          <Select
            mode="multiple" allowClear placeholder="币种（scan 源必选）"
            style={{ minWidth: 280 }} value={symbols} onChange={setSymbols}
            options={['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT'].map((sym) => ({ value: sym, label: sym }))}
          />
          <InputNumber min={7} max={90} value={days} onChange={(v) => setDays(v ?? 30)} addonAfter="天" style={{ width: 120 }} />
        </Space>
        <Typography.Paragraph type="secondary" style={{ marginTop: 10, marginBottom: 0, fontSize: 12 }}>
          出厂网格：保本武装（基线/0.8R 提前）× 微止盈（无/0.5R）× ATR 跟进 k（2.5/3.0/4.0）= 12 变体；
          PBO ≤ 0.2 且 DSR ≥ 0.9 → 高原可信；PBO ≥ 0.5 → 样本内最优多半是过拟合山峰，禁止当规律。
        </Typography.Paragraph>
      </Card>

      {record?.status === 'RUNNING' && (
        <Card size="small"><Space><Spin size="small" />扫描进行中（12 变体 × {days} 天信号重放）…</Space></Card>
      )}
      {record?.status === 'FAILED' && (
        <Alert type="error" showIcon message="扫描失败" description={record.error} />
      )}

      {s && record?.status === 'DONE' && (
        <>
          <Alert
            type={s.conclusion.verdict === 'plateau' ? 'success' : s.conclusion.verdict === 'peak' ? 'error' : 'warning'}
            showIcon
            message={verdict ? `判读：${verdict.label}` : '判读'}
            description={s.conclusion.text}
          />
          <Card size="small" title="折扣判据总览">
            <Descriptions size="small" column={4}>
              <Descriptions.Item label="PBO">
                <Typography.Text strong style={{ color: (s.pbo?.pbo ?? 1) >= 0.5 ? 'red' : (s.pbo?.pbo ?? 1) <= 0.2 ? 'green' : 'orange' }}>
                  {s.pbo ? `${fmt(s.pbo.pbo * 100, 1)}%` : '—'}
                </Typography.Text>
              </Descriptions.Item>
              <Descriptions.Item label="CSCV 组合">{s.pbo ? `${s.pbo.overfit_combos}/${s.pbo.n_combinations}（${s.pbo.n_blocks} 块）` : '—'}</Descriptions.Item>
              <Descriptions.Item label="IS最优">{s.best_is}{s.conclusion.best_is_is_best_oos ? '（=OOS最优）' : ''}</Descriptions.Item>
              <Descriptions.Item label="OOS最优">{s.best_oos}</Descriptions.Item>
              <Descriptions.Item label="信号数">{fmt(s.n_signals, 0)}</Descriptions.Item>
              <Descriptions.Item label="矩阵行（全变体decided）">{fmt(s.matrix_rows, 0)}</Descriptions.Item>
              <Descriptions.Item label="剔除（未决）">{fmt(s.excluded_n, 0)}</Descriptions.Item>
              <Descriptions.Item label="引擎">{s.engine_version}</Descriptions.Item>
            </Descriptions>
          </Card>
          <Card size="small" title={`逐变体统计（${s.n_trials} 变体 · 按 exp_r 排序）`}>
            <Table<RobustnessVariant>
              size="small" rowKey="name" columns={columns}
              dataSource={[...s.variants].sort((a, b) => (b.exp_r ?? -9) - (a.exp_r ?? -9))}
              pagination={false} scroll={{ y: 420 }}
            />
          </Card>
        </>
      )}

      <Card size="small" title="历史扫描">
        {history.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无扫描记录" />
        ) : (
          <Table<RobustnessListItem>
            size="small" rowKey="id"
            pagination={{ pageSize: 5, hideOnSinglePage: true }}
            dataSource={history}
            onRow={(r) => ({ onClick: () => openRecord(r.id), style: { cursor: 'pointer' } })}
            columns={[
              {
                title: '时间', dataIndex: 'created_at', width: 150,
                render: (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '-'),
              },
              { title: '状态', dataIndex: 'status', width: 90, render: (v: string) => <Tag color={v === 'DONE' ? 'green' : v === 'FAILED' ? 'red' : 'blue'}>{v}</Tag> },
              { title: '判读', dataIndex: 'verdict', width: 110, render: (v: string | null) => (v ? <Tag color={VERDICT_META[v]?.color ?? 'default'}>{VERDICT_META[v]?.label ?? v}</Tag> : '-') },
              { title: 'PBO', dataIndex: 'pbo', width: 80, align: 'right', render: (v: number | null) => (v === null ? '-' : `${fmt(v * 100, 1)}%`) },
              { title: '信号', dataIndex: 'n_signals', width: 70, align: 'right', render: (v: number | null) => (v === null ? '-' : fmt(v, 0)) },
              {
                title: '参数', key: 'p', ellipsis: true,
                render: (_: unknown, r: RobustnessListItem) =>
                  `${r.params?.source === 'history' ? '历史' : '扫描'} · ${(r.params?.symbols ?? []).join(',') || '全部'} · ${r.params?.days}天`,
              },
            ]}
          />
        )}
      </Card>
    </Space>
  )
}
