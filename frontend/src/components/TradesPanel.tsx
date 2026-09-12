/**
 * 交易记录页（docs/06）：自动交易生命周期与收益展示
 * - 概览条：在跑单子 / 已平仓 / 胜率 / 累计净收益（正绿负红）
 * - 列表：币种/方向/环境（测试网·正式网）/状态/开仓信息/三价现值/收益，分页默认 10 条、固定高度滚动
 * - 展开行：左右双卡片——左 AI 分析结论快照（AiAnalysisCard），右 操作历史时间线
 * - 状态筛选：胶囊按钮组（进行中 = 运行中/TP1已止盈/已保本 三态聚合，带计数徽标）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Descriptions,
  Empty,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Typography,
  message,
} from 'antd'
import {
  AccountBookOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  HistoryOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  RocketOutlined,
  SwapOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import AiAnalysisCard from './AiAnalysisCard'
import type { TradeEvent, TradeRecord } from '../types'

// 状态标签：运行中三态 + 已平仓 + 失败
const STATUS_MAP: Record<string, { label: string; color: string }> = {
  OPENED: { label: '运行中', color: 'blue' },
  TP1_HIT: { label: 'TP1已止盈', color: 'cyan' },
  TP2_HIT: { label: '已保本', color: 'geekblue' },
  CLOSED: { label: '已平仓', color: 'default' },
  FAILED: { label: '失败', color: 'red' },
}

// 出场原因映射
const EXIT_REASON_MAP: Record<string, string> = {
  sl: '止损离场',
  tp1_then_sl: 'TP1后止损',
  tp1_trail: 'TP1后跟踪止损',
  trail_sl: 'TP2后跟踪止损',
  breakeven_sl: '保本止损',
  manual: '手动平仓',
  error: '异常平仓',
}

// 操作历史事件元数据：中文标签 / 主题色 / 时间线圆点图标
const EVENT_META: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
  OPEN: { label: '开仓', color: '#1677ff', icon: <RocketOutlined /> },
  TP1_FILL: { label: 'TP1 止盈成交', color: '#52c41a', icon: <CheckCircleOutlined /> },
  TP2_FILL: { label: 'TP2 止盈成交', color: '#52c41a', icon: <CheckCircleOutlined /> },
  SL_MOVE: { label: '止损移动', color: '#fa8c16', icon: <SwapOutlined /> },
  SL_FILL: { label: '止损成交', color: '#ff4d4f', icon: <CloseCircleOutlined /> },
  CANCEL: { label: '撤单', color: '#8c8c8c', icon: <MinusCircleOutlined /> },
  SETTLE: { label: '结算', color: '#1677ff', icon: <AccountBookOutlined /> },
  ERROR: { label: '异常', color: '#ff4d4f', icon: <WarningOutlined /> },
}

// 事件详情按键的中文名
const DETAIL_KEY_MAP: Record<string, string> = {
  price: '开仓价',
  qty: '数量',
  stop_loss: '止损',
  tp1: '止盈一',
  tp2: '止盈二',
  risk_amount: '止损金额',
  entry_order_id: '开仓单ID',
  sl_order_id: '止损单ID',
  real_entry_price: '实际入场价',
  from: '原止损',
  to: '新止损',
  realized_pnl: '净盈亏',
  pnl_pct: '收益率',
  exit_reason: '出场原因',
  qty_tp1: 'TP1数量',
  qty_tp2: 'TP2数量',
  message: '错误信息',
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  return String(n)
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return new Date(s)
    .toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
    .replace(/\//g, '-')
}

// 事件详情值格式化：盈亏带正负号着色、百分比补 %、出场原因转中文
function detailValue(k: string, v: unknown): React.ReactNode {
  if (k === 'exit_reason') return EXIT_REASON_MAP[String(v)] || String(v)
  if (k === 'pnl_pct') return `${v}%`
  if (k === 'realized_pnl') {
    const n = Number(v)
    if (!Number.isNaN(n)) {
      const color = n > 0 ? '#52c41a' : n < 0 ? '#ff4d4f' : '#595959'
      return (
        <strong style={{ color }}>
          {n > 0 ? '+' : ''}
          {n.toFixed(2)} USDT
        </strong>
      )
    }
  }
  return String(v)
}

// 事件详情 JSON → Descriptions 组件（一行两项）
function renderDetail(detail: Record<string, unknown> | null) {
  if (!detail || Object.keys(detail).length === 0) return null
  return (
    <Descriptions
      size="small"
      column={2}
      colon={false}
      style={{ marginTop: 8 }}
      labelStyle={{
        fontSize: 12,
        color: '#999',
        width: 72,
        whiteSpace: 'nowrap',
        paddingInlineEnd: 8,
      }}
      contentStyle={{ fontSize: 12, color: '#333', wordBreak: 'break-all' }}
      items={Object.entries(detail).map(([k, v]) => ({
        key: k,
        label: DETAIL_KEY_MAP[k] || k,
        children: detailValue(k, v),
      }))}
    />
  )
}

// 概览小卡片（与 AI 价格卡同风格：灰底 + 大数字）
function StatCard({ title, value, color }: { title: string; value: string; color?: string }) {
  return (
    <div style={{ flex: 1, minWidth: 120, padding: '8px 14px', borderRadius: 8, background: '#fafafa' }}>
      <div style={{ fontSize: 12, color: '#999', marginBottom: 2 }}>{title}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color || 'rgba(0,0,0,0.88)', lineHeight: 1.3 }}>
        {value}
      </div>
    </div>
  )
}

/**
 * 交易记录页（docs/06）
 */
export default function TradesPanel() {
  const [rows, setRows] = useState<TradeRecord[]>([])
  const [eventsMap, setEventsMap] = useState<Record<string, TradeEvent[]>>({})
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  // 一次拉全量（上限 500），状态筛选/概览统计都在前端算——切换即时且带数量
  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const data = await scanApi.trades.list(undefined, 500)
      setRows(data)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取交易记录失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 初始加载；交易按小时巡检，30s 轮询保持页面新鲜
  useEffect(() => {
    fetchData()
    const timer = setInterval(fetchData, 30000)
    return () => clearInterval(timer)
  }, [fetchData])

  const filtered = useMemo(
    () =>
      statusFilter === ''
        ? rows
        : statusFilter === 'running'
          ? rows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status))
          : rows.filter((r) => r.status === statusFilter),
    [rows, statusFilter],
  )

  // 概览统计：在跑（三态）/ 已平仓 / 胜率 / 累计净收益
  const stats = useMemo(() => {
    const running = rows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status)).length
    const closedRows = rows.filter((r) => r.status === 'CLOSED' && r.realized_pnl != null)
    const wins = closedRows.filter((r) => (r.realized_pnl ?? 0) > 0).length
    const totalPnl = closedRows.reduce((s, r) => s + (r.realized_pnl ?? 0), 0)
    const winRate = closedRows.length > 0 ? Math.round((wins / closedRows.length) * 100) : null
    return { running, closed: closedRows.length, winRate, totalPnl }
  }, [rows])

  // 状态筛选选项：进行中 = 三态聚合（运行中/TP1已止盈/已保本），带当前数量与状态色点
  const statusOptions = useMemo(() => {
    const running = rows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status)).length
    const count = (s: string) => rows.filter((r) => r.status === s).length
    return [
      { value: '', label: '全部', count: rows.length, dot: null as string | null },
      { value: 'running', label: '进行中', count: running, dot: '#1677ff' },
      { value: 'CLOSED', label: '已平仓', count: count('CLOSED'), dot: '#8c8c8c' },
      { value: 'FAILED', label: '失败', count: count('FAILED'), dot: '#ff4d4f' },
    ]
  }, [rows])

  // 展开行时加载该笔交易的操作历史
  const loadEvents = useCallback(async (tradeId: string) => {
    try {
      const evts = await scanApi.trades.events(tradeId)
      setEventsMap((prev) => ({ ...prev, [tradeId]: evts }))
    } catch {
      // 静默：展开行显示空时间线
    }
  }, [])

  // 收益列：正绿负红（净盈亏 + 相对止损金额的百分比）
  const renderPnl = (rec: TradeRecord) => {
    if (rec.status !== 'CLOSED' || rec.realized_pnl === null || rec.realized_pnl === undefined) {
      return <span style={{ color: '#999' }}>-</span>
    }
    const pnl = rec.realized_pnl
    const pct = rec.pnl_pct !== null && rec.pnl_pct !== undefined ? ` (${rec.pnl_pct > 0 ? '+' : ''}${rec.pnl_pct}%)` : ''
    const color = pnl > 0 ? '#52c41a' : pnl < 0 ? '#ff4d4f' : '#999'
    return (
      <span style={{ color, fontWeight: 600 }}>
        {pnl > 0 ? '+' : ''}{pnl.toFixed(2)}{pct}
      </span>
    )
  }

  // 操作历史时间线：彩色图标圆点 + 白色事件卡片 + 键值详情网格
  const renderTimeline = (rec: TradeRecord) => {
    const evts = eventsMap[rec.id]
    if (!evts) return <Spin size="small" />
    if (evts.length === 0)
      return (
        <div style={{ textAlign: 'center', padding: '18px 0', color: '#bfbfbf', fontSize: 12 }}>
          <HistoryOutlined style={{ fontSize: 20, display: 'block', marginBottom: 6 }} />
          暂无操作历史
          <div style={{ marginTop: 4, fontSize: 11 }}>开仓/止盈/止损/结算等事件将按时间记录在这里</div>
        </div>
      )
    return (
      <div>
        <div style={{ fontSize: 12, color: '#999', marginBottom: 10 }}>
          共 <span style={{ color: '#595959', fontWeight: 600 }}>{evts.length}</span> 条事件
        </div>
        <Timeline
          items={evts.map((ev) => {
            const meta =
              EVENT_META[ev.event_type] || {
                label: ev.event_type,
                color: '#8c8c8c',
                icon: <HistoryOutlined />,
              }
            const detail = renderDetail(ev.detail)
            return {
              dot: (
                <span
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 22,
                    height: 22,
                    borderRadius: '50%',
                    background: meta.color,
                    color: '#fff',
                    fontSize: 11,
                    boxShadow: '0 0 0 3px #fafafa',
                  }}
                >
                  {meta.icon}
                </span>
              ),
              children: (
                <div
                  style={{
                    background: '#fff',
                    border: '1px solid #f0f0f0',
                    borderRadius: 8,
                    padding: detail ? '8px 12px' : '10px 12px',
                    marginBottom: 2,
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <span style={{ fontWeight: 600, fontSize: 13, color: meta.color }}>
                      {meta.label}
                    </span>
                    <span
                      style={{
                        fontSize: 12,
                        color: '#999',
                        marginLeft: 'auto',
                        fontVariantNumeric: 'tabular-nums',
                      }}
                    >
                      {fmtTime(ev.created_at)}
                    </span>
                  </div>
                  {detail}
                </div>
              ),
            }
          })}
        />
      </div>
    )
  }

  const columns: ColumnsType<TradeRecord> = [
    {
      title: '币种',
      dataIndex: 'symbol',
      key: 'symbol',
      render: (v: string, rec) => (
        <Space size={4}>
          <Typography.Text strong>{v}</Typography.Text>
          <Tag color={rec.direction === 'long' ? 'green' : 'red'} style={{ marginInlineEnd: 0 }}>
            {rec.direction === 'long' ? '多' : '空'}
          </Tag>
        </Space>
      ),
    },
    {
      title: '环境',
      dataIndex: 'testnet',
      key: 'testnet',
      width: 80,
      render: (v: boolean) =>
        v === false ? <Tag color="green">正式网</Tag> : <Tag color="gold">测试网</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: string) => {
        const s = STATUS_MAP[v] || { label: v, color: 'default' }
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: '评分',
      dataIndex: 'recommendation',
      key: 'recommendation',
      width: 70,
      render: (v: number | null) =>
        v != null ? <strong style={{ color: v >= 80 ? '#ff4d4f' : '#fa8c16' }}>{v}</strong> : '-',
    },
    {
      title: '开仓价',
      dataIndex: 'entry_price',
      key: 'entry_price',
      width: 100,
      render: fmtNum,
    },
    {
      title: '止损（现值）',
      dataIndex: 'stop_loss',
      key: 'stop_loss',
      width: 110,
      render: fmtNum,
    },
    {
      title: 'TP1',
      dataIndex: 'tp1',
      key: 'tp1',
      width: 90,
      render: fmtNum,
    },
    {
      title: 'TP2',
      dataIndex: 'tp2',
      key: 'tp2',
      width: 90,
      render: (v: number | null) => (v ? fmtNum(v) : '-'),
    },
    {
      title: '数量',
      dataIndex: 'qty',
      key: 'qty',
      width: 90,
      render: fmtNum,
    },
    {
      title: '保证金',
      dataIndex: 'margin_used',
      key: 'margin_used',
      width: 90,
      render: (v: number | null) => (v !== null && v !== undefined ? v.toFixed(2) : '-'),
    },
    {
      title: '开仓时间',
      dataIndex: 'opened_at',
      key: 'opened_at',
      width: 150,
      render: fmtTime,
    },
    {
      title: '收益',
      key: 'pnl',
      width: 120,
      render: (_: unknown, rec) => renderPnl(rec),
    },
    {
      title: '出场原因',
      dataIndex: 'exit_reason',
      key: 'exit_reason',
      width: 120,
      render: (v: string | null) => (v ? EXIT_REASON_MAP[v] || v : '-'),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Row justify="space-between" align="middle">
        {/* 状态筛选：胶囊按钮组（选中=蓝底白字，带状态色点与计数徽标） */}
        <Space size={6} wrap>
          {statusOptions.map((opt) => {
            const active = statusFilter === opt.value
            return (
              <Tag.CheckableTag
                key={opt.value}
                checked={active}
                onChange={() => {
                  setStatusFilter(opt.value)
                  setPage(1)
                }}
                style={{
                  borderRadius: 999,
                  padding: '3px 12px',
                  fontSize: 13,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  background: active ? undefined : '#fff',
                  border: `1px solid ${active ? 'transparent' : '#e5e5e5'}`,
                  boxShadow: active ? '0 1px 4px rgba(22,119,255,0.30)' : 'none',
                }}
              >
                {opt.dot && (
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      flexShrink: 0,
                      background: active ? '#fff' : opt.dot,
                    }}
                  />
                )}
                {opt.label}
                <span
                  style={{
                    fontSize: 11,
                    lineHeight: '16px',
                    padding: '0 6px',
                    borderRadius: 999,
                    background: active ? 'rgba(255,255,255,0.25)' : '#f5f5f5',
                    color: active ? '#fff' : '#8c8c8c',
                  }}
                >
                  {opt.count}
                </span>
              </Tag.CheckableTag>
            )
          })}
        </Space>
        <Button icon={<ReloadOutlined />} size="small" onClick={fetchData}>
          刷新
        </Button>
      </Row>

      {/* 概览条 */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <StatCard title="在跑单子" value={String(stats.running)} color="#1677ff" />
        <StatCard title="已平仓" value={String(stats.closed)} />
        <StatCard
          title="胜率（净盈利笔数占比）"
          value={stats.winRate === null ? '-' : `${stats.winRate}%`}
          color={stats.winRate === null ? undefined : stats.winRate >= 50 ? '#52c41a' : '#ff4d4f'}
        />
        <StatCard
          title="累计净收益（含手续费）"
          value={`${stats.totalPnl > 0 ? '+' : ''}${stats.totalPnl.toFixed(2)} USDT`}
          color={stats.totalPnl > 0 ? '#52c41a' : stats.totalPnl < 0 ? '#ff4d4f' : undefined}
        />
      </div>

      <Spin spinning={loading}>
        {rows.length === 0 && !loading ? (
          <Empty description="暂无交易记录（TRADING_ENABLED 开启后自动开单）" style={{ marginTop: 60 }} />
        ) : (
          <Table<TradeRecord>
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            size="small"
            scroll={{ y: 480 }}
            pagination={{
              current: page,
              pageSize,
              total: filtered.length,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              showTotal: (t) => `共 ${t} 条`,
              onChange: (p, ps) => {
                setPage(ps !== pageSize ? 1 : p)
                setPageSize(ps)
              },
            }}
            expandable={{
              expandedRowRender: (rec) => (
                <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  {/* 左：AI 分析结论快照（较宽） */}
                  <div
                    style={{
                      flex: '1.5 1 420px',
                      minWidth: 0,
                      padding: '12px 14px',
                      borderRadius: 8,
                      background: '#fafafa',
                      border: '1px solid #f0f0f0',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        marginBottom: 10,
                        fontWeight: 600,
                        fontSize: 13,
                        color: '#595959',
                      }}
                    >
                      <RobotOutlined style={{ color: '#1677ff' }} />
                      AI 分析结论（开单时快照）
                    </div>
                    {rec.ai_snapshot ? (
                      <AiAnalysisCard ai={rec.ai_snapshot} />
                    ) : (
                      <Typography.Text type="secondary">
                        该记录早于快照功能，未存档 AI 分析结论
                      </Typography.Text>
                    )}
                  </div>
                  {/* 右：操作历史时间线（较窄） */}
                  <div
                    style={{
                      flex: '1 1 280px',
                      minWidth: 260,
                      maxWidth: 440,
                      padding: '12px 14px',
                      borderRadius: 8,
                      background: '#fafafa',
                      border: '1px solid #f0f0f0',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        marginBottom: 10,
                        fontWeight: 600,
                        fontSize: 13,
                        color: '#595959',
                      }}
                    >
                      <HistoryOutlined style={{ color: '#fa8c16' }} />
                      操作历史
                    </div>
                    {renderTimeline(rec)}
                  </div>
                </div>
              ),
              onExpand: (expanded, rec) => {
                if (expanded && !eventsMap[rec.id]) loadEvents(rec.id)
              },
            }}
          />
        )}
      </Spin>
    </div>
  )
}
