/**
 * 策略插件面板（docs/20 S1，2026-10-10）
 * 顶部：策略清单（内置 golden_k_ai 原生 + 插件策略卡片，展示开关/预算/风险权重）；
 * 下方：影子信号表（策略插件产出的未开单信号，只读）+ 手动刷新按钮。
 * 铁律：S1 影子期只记录不决策——本面板不提供开单/预算修改入口，
 * 注册表变更走交易配置（PUT /api/scans/config strategy_registry）。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Empty, Select, Space, Spin, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { strategiesApi, type StrategyBrief, type StrategyShadowSignalRow } from '../api/strategies'
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
  const touched = raw.touched
  if (typeof touched === 'string') parts.push(`触 ${touched}`)
  const vol = raw.vol_vs_ma20
  if (typeof vol === 'number') parts.push(`量 ${vol}×MA20`)
  return parts.join(' · ') || '—'
}

export default function StrategiesPanel() {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const { up, down } = SCHEME_UP_DOWN[colorScheme as ColorScheme]

  const [loading, setLoading] = useState(false)
  const [strategies, setStrategies] = useState<StrategyBrief[]>([])
  const [signals, setSignals] = useState<StrategyShadowSignalRow[]>([])
  const [filter, setFilter] = useState<string | undefined>(undefined)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async (sid?: string) => {
    setLoading(true)
    try {
      const [list, rows] = await Promise.all([
        strategiesApi.list(),
        strategiesApi.signals(sid),
      ])
      setStrategies(list)
      setSignals(rows)
    } catch {
      message.error('加载策略数据失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(filter)
  }, [load, filter])

  const manualRefresh = async () => {
    setRefreshing(true)
    try {
      await strategiesApi.refresh()
      message.success('已触发影子信号任务，稍后自动刷新')
      // 延迟重载（Celery 异步执行需要时间）
      setTimeout(() => load(filter), 5000)
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
      title: '时间', dataIndex: 'created_at', width: 150,
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
    </Space>
  )
}
