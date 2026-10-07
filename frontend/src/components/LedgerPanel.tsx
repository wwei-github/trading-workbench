/**
 * 虚拟账本面板（docs/16 P-07）：全候选规则三价虚拟单统计
 * - 样本：每条扫描候选（含 AI 未分析/否决）一张规则三价虚拟单，周期任务推进到终局——
 *   判定样本从「已开单」放大 5-10 倍，N-01/N-02 的训练数据集
 * - KPI：decided/胜率/期望R/累计R/OPEN/EXPIRED（P-04 口径：未决与过期不进分母）
 * - 分桶：signal_type / direction / quality 分位 / rule_version（跨版本不混算）
 * - 明细：筛选 + 服务端分页（entry_bar_open DESC）
 * - 回填：手动触发源B 重放回填（唯一键去重可重入；409=已有任务在跑）
 * 无轮询：打开 tab / 刷新按钮 / 筛选变化时拉取
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Input,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { DatabaseOutlined, ReloadOutlined } from '@ant-design/icons'
import { ledgerApi } from '../api/ledger'
import type { LedgerBucket, LedgerEntry, LedgerStats } from '../types/ledger'
import { schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'

const OUTCOME_LABEL: Record<string, string> = {
  loss: '止损', win_tp1: 'TP1止盈', win_tp2: 'TP2止盈', be_exit: '保本/扫损',
}
const STATUS_LABEL: Record<string, string> = {
  OPEN: '在跟', DONE: '终局', EXPIRED: '过期',
}

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '-'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function rColor(v: number | null | undefined): string {
  const scheme = useScanStore.getState().colorScheme
  return schemeValueColor(v ?? 0, scheme)
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

// 分桶表（四维同列：分组/n/胜率/期望R/累计R）
function BucketTable({ title, rows }: { title: string; rows: LedgerBucket[] }) {
  return (
    <Card size="small" title={title} styles={{ body: { padding: 0 } }}>
      <Table
        size="small"
        rowKey="key"
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: '（无终局样本）' }}
        columns={[
          { title: '分组', dataIndex: 'key', width: 140, ellipsis: true },
          { title: 'n', dataIndex: 'n', width: 50 },
          { title: '胜率', dataIndex: 'win_rate', width: 70, render: (v: number) => `${(v * 100).toFixed(1)}%` },
          { title: '期望R', dataIndex: 'exp_r', width: 80, render: (v: number) => <b style={{ color: rColor(v) }}>{fmt(v)}</b> },
          { title: '累计R', dataIndex: 'sum_r', width: 80, render: (v: number) => fmt(v) },
        ]}
      />
    </Card>
  )
}

const PAGE_SIZE = 20

export default function LedgerPanel() {
  const [days, setDays] = useState(30)
  const [source, setSource] = useState<'' | 'live' | 'backfill'>('')
  const [stats, setStats] = useState<LedgerStats | null>(null)
  const [statsLoading, setStatsLoading] = useState(false)

  // 明细筛选（source/days 与总览共用）
  const [symbol, setSymbol] = useState('')
  const [direction, setDirection] = useState('')
  const [status, setStatus] = useState('')
  const [outcome, setOutcome] = useState('')
  const [signalType, setSignalType] = useState('')
  const [items, setItems] = useState<LedgerEntry[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [entriesLoading, setEntriesLoading] = useState(false)

  const [backfillDays, setBackfillDays] = useState(30)
  const [backfilling, setBackfilling] = useState(false)

  const loadStats = useCallback((d: number, src: '' | 'live' | 'backfill') => {
    setStatsLoading(true)
    ledgerApi.stats(d, src || undefined)
      .then(setStats)
      .catch(() => message.error('获取账本统计失败'))
      .finally(() => setStatsLoading(false))
  }, [])

  const loadEntries = useCallback((opts: {
    d: number; src: '' | 'live' | 'backfill'; p: number;
    sym: string; dir: string; st: string; oc: string; sig: string
  }) => {
    setEntriesLoading(true)
    ledgerApi.entries({
      days: opts.d, source: opts.src || undefined, page: opts.p, page_size: PAGE_SIZE,
      symbol: opts.sym || undefined, direction: opts.dir || undefined,
      status: opts.st || undefined, outcome: opts.oc || undefined,
      signal_type: opts.sig || undefined,
    })
      .then((r) => { setItems(r.items); setTotal(r.total) })
      .catch(() => message.error('获取账本明细失败'))
      .finally(() => setEntriesLoading(false))
  }, [])

  useEffect(() => {
    loadStats(days, source)
    loadEntries({ d: days, src: source, p: 1, sym: symbol, dir: direction, st: status, oc: outcome, sig: signalType })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days, source])

  const refresh = () => {
    loadStats(days, source)
    setPage(1)
    loadEntries({ d: days, src: source, p: 1, sym: symbol, dir: direction, st: status, oc: outcome, sig: signalType })
  }

  // 筛选变化 → 回第 1 页重查明细（统计不动）
  const refilter = (patch: { sym?: string; dir?: string; st?: string; oc?: string; sig?: string }) => {
    const next = {
      sym: patch.sym ?? symbol, dir: patch.dir ?? direction, st: patch.st ?? status,
      oc: patch.oc ?? outcome, sig: patch.sig ?? signalType,
    }
    setSymbol(next.sym); setDirection(next.dir); setStatus(next.st)
    setOutcome(next.oc); setSignalType(next.sig)
    setPage(1)
    loadEntries({ d: days, src: source, p: 1, ...next })
  }

  const turnPage = (p: number) => {
    setPage(p)
    loadEntries({ d: days, src: source, p, sym: symbol, dir: direction, st: status, oc: outcome, sig: signalType })
  }

  const submitBackfill = async () => {
    setBackfilling(true)
    try {
      await ledgerApi.backfill(backfillDays)
      message.success(`回填任务已提交（源B 重放 ${backfillDays} 天，唯一键去重可重入）`)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '提交回填失败')
    } finally {
      setBackfilling(false)
    }
  }

  const ov = stats?.overall ?? null
  const hasBuckets = (rows?: LedgerBucket[]) => !!rows && rows.length > 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 总览：KPI + 维度切换 */}
      <Card size="small" title={<><DatabaseOutlined /> 虚拟账本总览（全候选规则单 · P-04 口径：未决/过期不进分母）</>}
        extra={
          <Space>
            <Segmented
              value={source}
              onChange={(v) => setSource(v as '' | 'live' | 'backfill')}
              options={[
                { value: '', label: '全部' },
                { value: 'live', label: '实时' },
                { value: 'backfill', label: '回填' },
              ]}
            />
            <Select value={days} onChange={setDays} size="small" style={{ width: 92 }}
              options={[7, 14, 30, 60, 90].map((d) => ({ value: d, label: `近 ${d} 天` }))} />
            <Button size="small" icon={<ReloadOutlined />} loading={statsLoading || entriesLoading} onClick={refresh}>
              刷新
            </Button>
            <Popconfirm
              title={`触发源B 回填（近 ${backfillDays} 天）？`}
              description="对扫描池重放信号插 backfill 行，唯一键去重与实时行天然不冲突"
              onConfirm={submitBackfill}
            >
              <Space.Compact>
                <Select value={backfillDays} onChange={setBackfillDays} size="small" style={{ width: 72 }}
                  options={[7, 30, 60, 90].map((d) => ({ value: d, label: `${d}天` }))} />
                <Button size="small" loading={backfilling}>回填</Button>
              </Space.Compact>
            </Popconfirm>
          </Space>
        }>
        <Spin spinning={statsLoading}>
          <Row gutter={[8, 8]}>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="终局样本 decided" value={fmt(ov?.decided ?? 0)}
                sub={`胜 ${ov?.wins ?? 0} / 负 ${ov?.losses ?? 0} / 保本 ${ov?.be_exits ?? 0}`} />
            </Col>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="胜率" value={ov?.win_rate === null || ov === null ? '-' : `${(ov.win_rate * 100).toFixed(1)}%`}
                sub="win_tp1+win_tp2 / decided" />
            </Col>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="期望 R（含费）" value={fmt(ov?.exp_r)} color={rColor(ov?.exp_r)}
                sub="decided 净 R 均值" />
            </Col>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="累计 R" value={fmt(ov?.sum_r)} color={rColor(ov?.sum_r)} sub="decided 合计" />
            </Col>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="在跟 OPEN" value={fmt(ov?.open ?? 0)} color="#1677ff" sub="未决单列不进分母" />
            </Col>
            <Col xs={12} md={8} xl={4}>
              <Kpi title="过期 EXPIRED" value={fmt(ov?.expired ?? 0)} color="#999" sub=">30 天无数据佐证" />
            </Col>
          </Row>
        </Spin>
      </Card>

      {/* 四维分桶（只统计 decided 行） */}
      <Spin spinning={statsLoading}>
        <Row gutter={[12, 12]}>
          <Col xs={24} md={12}>
            <BucketTable title="按信号类型" rows={stats?.by_signal_type ?? []} />
          </Col>
          <Col xs={24} md={12}>
            <BucketTable title="按质量分位（CARD-09 同切法）" rows={stats?.by_quality ?? []} />
          </Col>
          <Col xs={24} md={12}>
            <BucketTable title="按方向" rows={stats?.by_direction ?? []} />
          </Col>
          <Col xs={24} md={12}>
            <BucketTable title="按规则版本（跨版本不混算）" rows={stats?.by_rule_version ?? []} />
          </Col>
        </Row>
        {!hasBuckets(stats?.by_signal_type) && !statsLoading && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            （暂无终局样本——等推进任务结算，或先触发回填补历史）
          </Typography.Text>
        )}
      </Spin>

      {/* 明细 */}
      <Card size="small" title={`账本明细（${total} 行 · 按信号K线倒序）`}>
        <Space wrap size="small" style={{ marginBottom: 8 }}>
          <Input
            allowClear placeholder="币种如 BTCUSDT" style={{ width: 150 }}
            onPressEnter={(e) => refilter({ sym: (e.target as HTMLInputElement).value.trim().toUpperCase() })}
            onBlur={(e) => { const v = e.target.value.trim().toUpperCase(); if (v !== symbol) refilter({ sym: v }) }}
          />
          <Select allowClear placeholder="方向" value={direction || undefined} style={{ width: 90 }}
            onChange={(v) => refilter({ dir: v ?? '' })}
            options={[{ value: 'long', label: '多' }, { value: 'short', label: '空' }]} />
          <Select allowClear placeholder="状态" value={status || undefined} style={{ width: 90 }}
            onChange={(v) => refilter({ st: v ?? '' })}
            options={Object.entries(STATUS_LABEL).map(([v, l]) => ({ value: v, label: l }))} />
          <Select allowClear placeholder="结局" value={outcome || undefined} style={{ width: 110 }}
            onChange={(v) => refilter({ oc: v ?? '' })}
            options={Object.entries(OUTCOME_LABEL).map(([v, l]) => ({ value: v, label: l }))} />
          <Input
            allowClear placeholder="信号类型" style={{ width: 190 }}
            onPressEnter={(e) => refilter({ sig: (e.target as HTMLInputElement).value.trim() })}
            onBlur={(e) => { const v = e.target.value.trim(); if (v !== signalType) refilter({ sig: v }) }}
          />
        </Space>
        <Table
          size="small"
          loading={entriesLoading}
          dataSource={items}
          rowKey="id"
          pagination={{
            current: page, pageSize: PAGE_SIZE, total, size: 'small',
            showSizeChanger: false, onChange: turnPage,
          }}
          columns={[
            { title: '信号K线', dataIndex: 'entry_bar_open', width: 130, render: (v: string | null) => (v || '').replace('T', ' ').slice(0, 16) },
            { title: '币', dataIndex: 'symbol', width: 95 },
            { title: '方向', dataIndex: 'direction', width: 60, render: (v: string) => v === 'long' ? <Tag color="green">多</Tag> : <Tag color="red">空</Tag> },
            { title: '来源', dataIndex: 'source', width: 65, render: (v: string) => v === 'live' ? '实时' : '回填' },
            { title: '信号', dataIndex: 'signal_type', width: 150, ellipsis: true },
            { title: '质量', dataIndex: 'quality_score', width: 55, render: (v: number | null) => v ?? '-' },
            { title: '入场', dataIndex: 'entry', width: 85, render: (v: number) => fmt(v, 4) },
            { title: '止损', dataIndex: 'stop', width: 85, render: (v: number) => fmt(v, 4) },
            { title: 'TP1', dataIndex: 'tp1', width: 85, render: (v: number) => fmt(v, 4) },
            { title: '状态', dataIndex: 'status', width: 65, render: (v: string) =>
                <Tag color={v === 'DONE' ? 'blue' : v === 'OPEN' ? 'processing' : 'default'}>{STATUS_LABEL[v] ?? v}</Tag> },
            { title: '结局', dataIndex: 'outcome', width: 95, render: (v: string | null) => {
                if (!v) return '-'
                const color = v === 'loss' ? 'red' : v.startsWith('win') ? 'green' : 'orange'
                return <Tag color={color}>{OUTCOME_LABEL[v] ?? v}</Tag>
              } },
            { title: '净R', dataIndex: 'r', width: 70, render: (v: number | null) => <b style={{ color: rColor(v) }}>{v === null ? '-' : fmt(v)}</b> },
            { title: '根数', dataIndex: 'bars', width: 55, render: (v: number | null) => v ?? '-' },
            { title: 'AI', dataIndex: 'ai_decision', width: 80, render: (v: string | null, r: LedgerEntry) => {
                if (!v) return '-'
                return <TooltipTitle v={v} rec={r.ai_recommendation} />
              } },
            { title: '版本', dataIndex: 'rule_version', width: 90, render: (v: string | null) => v ?? 'legacy' },
          ]}
        />
      </Card>
    </div>
  )
}

// AI 标注列：suggest/skip + 推荐分（N-02 校准数据可见性）
function TooltipTitle({ v, rec }: { v: string; rec: number | null }) {
  const label = v === 'suggest' ? '建议' : v === 'skip' ? '跳过' : v
  return (
    <span>
      {label}
      {rec !== null && <Typography.Text type="secondary" style={{ fontSize: 11 }}> {fmt(rec, 0)}</Typography.Text>}
    </span>
  )
}
