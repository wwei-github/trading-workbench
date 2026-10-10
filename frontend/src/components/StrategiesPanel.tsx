/**
 * 策略插件面板（docs/20 S1 + 影子分析，2026-10-10）
 * 顶部：策略清单（内置 golden_k_ai 原生 + 插件策略卡片，展示开关/预算/风险权重）；
 * 中部：影子信号表（含推演回填 outcome/R）+ 影子分析统计（win_rate/期望 R/重叠率）；
 * 底部：L1 观测快照（docs/19 §3.1，评分卡 8 项明细，纯观测）。
 * 铁律：S1 影子期只记录不决策——本面板不提供开单/预算修改入口，
 * 注册表变更走交易配置（PUT /api/scans/config strategy_registry）。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Empty, Select, Space, Spin, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { FundOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import {
  strategiesApi,
  type StrategyAnalysis,
  type StrategyBrief,
  type StrategyObservationRow,
  type StrategyShadowSignalRow,
} from '../api/strategies'
import { bj } from '../utils/dayjs'
import { SCHEME_UP_DOWN } from '../utils/scheme'
import { useScanStore, type ColorScheme } from '../stores/scanStore'

const { Text, Paragraph } = Typography

// 策略私有证据的紧凑摘要（raw 关键字段 → 一行文案）
function rawSummary(raw: Record<string, unknown> | null): string {
  if (!raw) return '—'
  const parts: string[] = []
  const gate = raw.gate_4h as Record<string, unknown> | undefined
  const st = raw.structure_1h as Record<string, unknown> | undefined
  if (gate) {
    if (typeof gate.adx === 'number') parts.push(`ADX ${gate.adx}`)
    if (typeof gate.slope_pct === 'number') parts.push(`斜率 ${(gate.slope_pct * 100).toFixed(2)}%`)
    if (typeof gate.wraps === 'number') parts.push(`缠绕 ${gate.wraps}`)
  }
  if (st) {
    if (typeof st.div_fast_mid === 'number') parts.push(`发散 ${st.div_fast_mid}`)
    if (typeof st.atr === 'number') parts.push(`ATR ${st.atr}`)
  }
  if (typeof raw.range_score === 'number') parts.push(`震荡分 ${raw.range_score}`)
  const mz = raw.macd_zero
  if (typeof mz === 'string') parts.push(`零轴 ${mz}`)
  const touched = raw.touched
  if (typeof touched === 'string') parts.push(`触 ${touched}`)
  const vol = raw.vol_vs_ma20
  if (typeof vol === 'number') parts.push(`量 ${vol}×MA20`)
  return parts.join(' · ') || '—'
}

const OUTCOME_META: Record<string, { label: string; color: string }> = {
  win_tp1: { label: '胜·TP1', color: 'green' },
  win_tp2: { label: '胜·TP2', color: 'green' },
  be_exit: { label: '保本', color: 'gold' },
  loss: { label: '亏', color: 'red' },
  open: { label: '未决', color: 'default' },
  invalid: { label: '无效', color: 'default' },
}

export default function StrategiesPanel() {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const { up, down } = SCHEME_UP_DOWN[colorScheme as ColorScheme]

  const [loading, setLoading] = useState(false)
  const [strategies, setStrategies] = useState<StrategyBrief[]>([])
  const [signals, setSignals] = useState<StrategyShadowSignalRow[]>([])
  const [filter, setFilter] = useState<string | undefined>(undefined)
  const [refreshing, setRefreshing] = useState(false)
  const [analysis, setAnalysis] = useState<StrategyAnalysis | null>(null)
  const [observations, setObservations] = useState<StrategyObservationRow[]>([])
  const [obsLoading, setObsLoading] = useState(false)

  const load = useCallback(async (sid?: string) => {
    setLoading(true)
    try {
      const [list, rows, ana] = await Promise.all([
        strategiesApi.list(),
        strategiesApi.signals(sid),
        strategiesApi.analysis(),
      ])
      setStrategies(list)
      setSignals(rows)
      setAnalysis(ana)
    } catch {
      message.error('加载策略数据失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadObservations = useCallback(async () => {
    setObsLoading(true)
    try {
      setObservations(await strategiesApi.observations(undefined, 50))
    } catch {
      message.error('加载观测数据失败')
    } finally {
      setObsLoading(false)
    }
  }, [])

  useEffect(() => {
    load(filter)
    loadObservations()
  }, [load, loadObservations, filter])

  const manualRefresh = async () => {
    setRefreshing(true)
    try {
      await strategiesApi.refresh()
      message.success('已触发影子信号任务，稍后自动刷新')
      // 延迟重载（Celery 异步执行需要时间）
      setTimeout(() => {
        load(filter)
        loadObservations()
      }, 5000)
    } catch {
      message.error('触发失败')
    } finally {
      setRefreshing(false)
    }
  }

  const manualEvaluate = async () => {
    setRefreshing(true)
    try {
      await strategiesApi.evaluate()
      message.success('已触发推演回填，稍后自动刷新')
      setTimeout(() => load(filter), 5000)
    } catch {
      message.error('触发失败')
    } finally {
      setRefreshing(false)
    }
  }

  const manualObservations = async () => {
    setRefreshing(true)
    try {
      await strategiesApi.refreshObservations()
      message.success('已触发 L1 观测任务，稍后自动刷新')
      setTimeout(() => loadObservations(), 5000)
    } catch {
      message.error('触发失败')
    } finally {
      setRefreshing(false)
    }
  }

  const stratCols: ColumnsType<StrategyBrief> = [
    {
      title: '策略', dataIndex: 'display_name', width: 170,
      render: (v: string, r) => (
        <Space size={6}>
          <Text strong>{v}</Text>
          {r.kind === 'native' ? <Tag>内置</Tag> : <Tag color="blue">插件</Tag>}
        </Space>
      ),
    },
    { title: 'ID', dataIndex: 'id', width: 120, render: (v: string) => <Text code>{v}</Text> },
    {
      title: '状态', dataIndex: 'enabled', width: 90,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: '仓位预算', dataIndex: 'max_positions', width: 100,
      render: (v: number) => <Tooltip title="策略级在跑仓上限（全局账户预算的子集划分）">{v} 仓</Tooltip>,
    },
    {
      title: '风险权重', dataIndex: 'risk_weight', width: 100,
      render: (v: number) => <Tooltip title="乘进单笔风险额；新策略 0.5 起步的风险驯化通道">×{v}</Tooltip>,
    },
    {
      title: '私有参数', dataIndex: 'config',
      render: (v: Record<string, unknown>) =>
        Object.keys(v || {}).length === 0 ? '—' : (
          <Paragraph style={{ margin: 0 }} code ellipsis={{ rows: 1 }}>
            {JSON.stringify(v)}
          </Paragraph>
        ),
    },
  ]

  const sigCols: ColumnsType<StrategyShadowSignalRow> = [
    {
      title: '币种', dataIndex: 'symbol', width: 120,
      render: (v: string) => <Text strong>{v.replace(/\/?USDT$/i, '')}</Text>,
    },
    {
      title: '方向', dataIndex: 'direction', width: 70,
      render: (v: 'long' | 'short') => (
        <Tag color={v === 'long' ? up : down}>{v === 'long' ? '多' : '空'}</Tag>
      ),
    },
    { title: '入场', dataIndex: 'entry', width: 90, render: (v: number) => v ?? '—' },
    { title: '止损', dataIndex: 'stop', width: 90, render: (v: number) => v ?? '—' },
    {
      title: 'TP1', dataIndex: 'tp1', width: 80,
      render: (v: number) => (v > 0 ? v : <Text type="secondary">纯跟踪</Text>),
    },
    {
      title: '类型', dataIndex: 'signal_type', width: 80,
      render: (v: string | null) =>
        v === 'pullback' ? <Tag color="cyan">回踩</Tag>
          : v === 'cross' ? <Tag color="geekblue">金叉</Tag>
            : (v || '—'),
    },
    { title: '置信', dataIndex: 'confidence', width: 70, render: (v: number) => (v * 100).toFixed(0) },
    {
      title: '信号说明', dataIndex: 'reason', ellipsis: true,
      render: (v: string | null, r) => (
        <Tooltip title={rawSummary(r.raw)}>
          <span>{v || '—'}</span>
        </Tooltip>
      ),
    },
    {
      title: '证据', dataIndex: 'raw', width: 260, ellipsis: true,
      render: (_: unknown, r) => <Text type="secondary">{rawSummary(r.raw)}</Text>,
    },
    {
      title: '推演', dataIndex: 'outcome', width: 110,
      render: (v: string | null, r) => {
        if (!v) return <Text type="secondary">未推演</Text>
        const meta = OUTCOME_META[v] || { label: v, color: 'default' }
        return (
          <Tooltip title={`假想出场 ${r.eval_bars ?? '—'} 根（replay_exit 按策略出场语义回放，open 不进 decided 分母）`}>
            <Space size={4}>
              <Tag color={meta.color} style={{ margin: 0 }}>{meta.label}</Tag>
              {r.r != null && (
                <Text type={r.r >= 0 ? 'success' : 'danger'} style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {r.r >= 0 ? '+' : ''}{r.r.toFixed(2)}R
                </Text>
              )}
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: '时间', dataIndex: 'created_at', width: 150,
      render: (v: string | null) => (v ? bj(v).format('MM-DD HH:mm') : '—'),
    },
  ]

  const analysisCols: ColumnsType<StrategyAnalysis['stats'][number]> = [
    { title: '策略', dataIndex: 'strategy_id', width: 130, render: (v: string) => <Text code>{v}</Text> },
    { title: '信号', dataIndex: 'signals', width: 70 },
    { title: '已推演', dataIndex: 'evaluated', width: 80 },
    {
      title: 'decided', dataIndex: 'decided', width: 80,
      render: (v: number, r) => <Tooltip title="未决 open 不进分母（P-04 口径诚实原则）">{v}{r.open > 0 && <Text type="secondary"> (+{r.open}open)</Text>}</Tooltip>,
    },
    { title: '胜', dataIndex: 'wins', width: 60 },
    { title: '胜率', dataIndex: 'win_rate', width: 80, render: (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`) },
    {
      title: '期望 R', dataIndex: 'avg_r', width: 90,
      render: (v: number | null) => (v == null ? '—' : (
        <Text type={v >= 0 ? 'success' : 'danger'} style={{ fontVariantNumeric: 'tabular-nums' }}>
          {v >= 0 ? '+' : ''}{v.toFixed(3)}
        </Text>
      )),
    },
    { title: '累计 R', dataIndex: 'total_r', width: 90, render: (v: number | null) => (v == null ? '—' : v.toFixed(2)) },
  ]

  const obsCols: ColumnsType<StrategyObservationRow> = [
    { title: '币种', dataIndex: 'symbol', width: 110, render: (v: string) => <Text strong>{v.replace(/\/?USDT$/i, '')}</Text> },
    {
      title: '4h 闸门', key: 'gate', width: 200, ellipsis: true,
      render: (_: unknown, r) => {
        const g = r.metrics.gate_4h
        if (!g) return <Text type="secondary">—</Text>
        const parts: string[] = []
        if (typeof g.direction === 'string' && g.direction !== 'none') parts.push(g.direction === 'up' ? '多头' : '空头')
        if (typeof g.slope_pct === 'number') parts.push(`斜率 ${(g.slope_pct * 100).toFixed(2)}%`)
        if (typeof g.wraps === 'number') parts.push(`缠绕 ${g.wraps}`)
        if (typeof g.adx === 'number') parts.push(`ADX ${g.adx}`)
        return parts.join(' · ') || '—'
      },
    },
    {
      title: '震荡分', key: 'score', width: 120,
      render: (_: unknown, r) => {
        const rs = r.metrics.range_score
        if (!rs) return '—'
        const hits = rs.items.filter((it) => it.hit === true).map((it) => `${it.no}.${it.name}`)
        return (
          <Tooltip title={hits.length ? hits.join('；') : '8 项全不命中'}>
            <Tag color={rs.score >= 3 ? 'orange' : rs.score >= 1 ? 'default' : 'green'} style={{ margin: 0 }}>
              {rs.score}/8
            </Tag>
          </Tooltip>
        )
      },
    },
    {
      title: 'KDJ', key: 'stoch', width: 140, ellipsis: true,
      render: (_: unknown, r) => {
        const st = r.metrics.stochastic
        if (!st) return '—'
        return `K${Number(st.k).toFixed(0)} D${Number(st.d).toFixed(0)}${st.cross === 1 ? ' 金叉' : st.cross === -1 ? ' 死叉' : ''}`
      },
    },
    { title: 'MACD零轴', key: 'mz', width: 110, render: (_: unknown, r) => r.metrics.macd_zero || '—' },
    {
      title: '时间', dataIndex: 'created_at', width: 140,
      render: (v: string | null) => (v ? bj(v).format('MM-DD HH:mm') : '—'),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small" title="策略清单">
        <Table<StrategyBrief>
          rowKey="id"
          size="small"
          columns={stratCols}
          dataSource={strategies}
          loading={loading}
          pagination={false}
        />
      </Card>
      <Card
        size="small"
        title={
          <Space>
            <span>影子信号</span>
            <Text type="secondary" style={{ fontWeight: 400 }}>
              策略插件产出、未开单（S1 影子期只记录不决策）
            </Text>
          </Space>
        }
        extra={
          <Space>
            <Select
              allowClear
              placeholder="全部策略"
              style={{ width: 160 }}
              value={filter}
              onChange={(v) => setFilter(v)}
              options={strategies
                .filter((s) => s.kind === 'plugin')
                .map((s) => ({ label: s.display_name, value: s.id }))}
            />
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={refreshing}
              onClick={manualRefresh}
            >
              手动跑信号
            </Button>
            <Button
              size="small"
              icon={<FundOutlined />}
              loading={refreshing}
              onClick={manualEvaluate}
            >
              推演回填
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => load(filter)}>
              刷新
            </Button>
          </Space>
        }
      >
        {loading && signals.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>
        ) : signals.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无影子信号——启用插件策略后随扫描自动产出，或点「手动跑信号」"
          />
        ) : (
          <Table<StrategyShadowSignalRow>
            rowKey="id"
            size="small"
            columns={sigCols}
            dataSource={signals}
            loading={loading}
            pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
          />
        )}
      </Card>
      {analysis && analysis.stats.length > 0 && (
        <Card
          size="small"
          title={
            <Space>
              <span>影子分析</span>
              <Text type="secondary" style={{ fontWeight: 400 }}>
                并发准入判定数据源（docs/20 §4.2）：重叠率 &gt;50% = 伪分散，只跑其一
              </Text>
            </Space>
          }
        >
          <Table
            rowKey="strategy_id"
            size="small"
            columns={analysisCols}
            dataSource={analysis.stats}
            loading={loading}
            pagination={false}
          />
          {analysis.overlaps.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">信号重叠率：</Text>
              {analysis.overlaps.map((o) => (
                <Tag
                  key={o.pair.join('+')}
                  color={o.overlap_rate > 0.5 ? 'red' : o.overlap_rate > 0.3 ? 'orange' : 'green'}
                  style={{ marginInlineEnd: 4 }}
                >
                  {o.pair.join(' × ')} {(o.overlap_rate * 100).toFixed(0)}%
                </Tag>
              ))}
            </div>
          )}
        </Card>
      )}
      <Card
        size="small"
        title={
          <Space>
            <span>L1 观测快照</span>
            <Text type="secondary" style={{ fontWeight: 400 }}>
              全市场周期证据（docs/19 §3.1，纯观测不拦截；P1 闸门阈值定标素材）
            </Text>
          </Space>
        }
        extra={
          <Space>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={refreshing}
              onClick={manualObservations}
            >
              手动观测
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={loadObservations}>
              刷新
            </Button>
          </Space>
        }
      >
        {obsLoading && observations.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>
        ) : observations.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无观测数据——随扫描自动积累，或点「手动观测」"
          />
        ) : (
          <Table<StrategyObservationRow>
            rowKey="id"
            size="small"
            columns={obsCols}
            dataSource={observations}
            loading={obsLoading}
            pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
          />
        )}
      </Card>
    </Space>
  )
}
