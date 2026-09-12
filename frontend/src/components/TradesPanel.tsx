/**
 * 交易记录页（docs/06）：自动交易生命周期与收益展示
 * - 列表：币种/方向/状态/开仓信息/三价现值/收益（正绿负红）
 * - 展开行：操作历史时间线（开仓/止盈成交/止损移动/结算）
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Col,
  Empty,
  Radio,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
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
  trail_sl: 'TP2后跟踪止损',
  breakeven_sl: '保本止损',
  manual: '手动平仓',
  error: '异常平仓',
}

// 操作历史事件标签
const EVENT_MAP: Record<string, { label: string; color: string }> = {
  OPEN: { label: '开仓', color: 'green' },
  TP1_FILL: { label: 'TP1止盈成交', color: 'green' },
  TP2_FILL: { label: 'TP2止盈成交', color: 'green' },
  SL_MOVE: { label: '止损移动', color: 'blue' },
  SL_FILL: { label: '止损成交', color: 'red' },
  CANCEL: { label: '撤单', color: 'default' },
  SETTLE: { label: '结算', color: 'blue' },
  ERROR: { label: '异常', color: 'red' },
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
  real_entry_price: '实际入场 AI 价',
  from: '原止损',
  to: '新止损',
  realized_pnl: '已实现盈亏',
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
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

// 事件详情 JSON → "中文名: 值" 列表
function renderDetail(detail: Record<string, unknown> | null) {
  if (!detail || Object.keys(detail).length === 0) return null
  return (
    <div style={{ fontSize: 12, color: '#666' }}>
      {Object.entries(detail).map(([k, v]) => (
        <div key={k}>
          {DETAIL_KEY_MAP[k] || k}: {String(v)}
        </div>
      ))}
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
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined)

  const fetchData = useCallback(async (status?: string) => {
    setLoading(true)
    try {
      const data = await scanApi.trades.list(status, 200)
      setRows(data)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取交易记录失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 初始加载；交易按小时巡检，30s 轮询保持页面新鲜
  useEffect(() => {
    fetchData(statusFilter)
    const timer = setInterval(() => fetchData(statusFilter), 30000)
    return () => clearInterval(timer)
  }, [statusFilter, fetchData])

  // 展开行时加载该笔交易的操作历史
  const loadEvents = useCallback(async (tradeId: string) => {
    try {
      const evts = await scanApi.trades.events(tradeId)
      setEventsMap((prev) => ({ ...prev, [tradeId]: evts }))
    } catch {
      // 静默：展开行显示空时间线
    }
  }, [])

  // 收益列：正绿负红（已实现盈亏 + 相对止损金额的百分比）
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

  const columns: ColumnsType<TradeRecord> = [
    {
      title: '币种',
      dataIndex: 'symbol',
      key: 'symbol',
      render: (v: string, rec) => (
        <Space size={4}>
          <Typography.Text strong>{v}</Typography.Text>
          <Tag color={rec.direction === 'long' ? 'green' : 'red'}>
            {rec.direction === 'long' ? '多' : '空'}
          </Tag>
        </Space>
      ),
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
      render: fmtNum,
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

  const statusOptions = [
    { value: '', label: '全部' },
    { value: 'OPENED', label: '运行中' },
    { value: 'TP1_HIT', label: 'TP1已止盈' },
    { value: 'TP2_HIT', label: '已保本' },
    { value: 'CLOSED', label: '已平仓' },
    { value: 'FAILED', label: '失败' },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Row justify="space-between" align="middle">
        <Col>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={statusFilter ?? ''}
            onChange={(e) => setStatusFilter(e.target.value || undefined)}
            options={statusOptions}
          />
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} size="small" onClick={() => fetchData(statusFilter)}>
            刷新
          </Button>
        </Col>
      </Row>

      <Spin spinning={loading}>
        {rows.length === 0 && !loading ? (
          <Empty description="暂无交易记录（TRADING_ENABLED 开启后自动开单）" style={{ marginTop: 60 }} />
        ) : (
          <Table<TradeRecord>
            rowKey="id"
            columns={columns}
            dataSource={rows}
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: false }}
            expandable={{
              expandedRowRender: (rec) => {
                const evts = eventsMap[rec.id]
                if (!evts) {
                  return <Spin size="small" />
                }
                if (evts.length === 0) {
                  return <Typography.Text type="secondary">暂无操作历史</Typography.Text>
                }
                return (
                  <Timeline
                    items={evts.map((ev) => {
                      const meta = EVENT_MAP[ev.event_type] || { label: ev.event_type, color: 'gray' }
                      return {
                        color: meta.color,
                        children: (
                          <div>
                            <Space size={8}>
                              <Tag color={meta.color}>{meta.label}</Tag>
                              <span style={{ fontSize: 12, color: '#999' }}>{fmtTime(ev.created_at)}</span>
                            </Space>
                            {renderDetail(ev.detail)}
                          </div>
                        ),
                      }
                    })}
                  />
                )
              },
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
