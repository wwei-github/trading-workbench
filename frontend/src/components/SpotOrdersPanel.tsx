/**
 * 现货·订单记录（docs/18 P0 只读列表）
 * P0 通常为空（无下单通道）；P1 信号/定投执行后展示：
 * 币种/方向/数量/金额/价格/触发方式/状态/环境/时间，触发详情可展开
 */
import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Empty, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { spotApi } from '../api/spot'
import type { SpotOrder } from '../types/spot'
import { bj } from '../utils/dayjs'

const STATUS_META: Record<string, { label: string; color: string }> = {
  PENDING_CONFIRM: { label: '待确认', color: 'gold' },
  FILLED: { label: '已成交', color: 'green' },
  CANCELLED: { label: '已取消', color: 'default' },
  FAILED: { label: '失败', color: 'red' },
}

const TRIGGER_META: Record<string, string> = {
  signal: '折价信号',
  dca: '周度定投',
  manual: '手动',
}

function fmtPrice(v: number | null): string {
  if (v === null || v === undefined) return '-'
  return v >= 100 ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : v.toFixed(6)
}

export default function SpotOrdersPanel() {
  const [rows, setRows] = useState<SpotOrder[]>([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await spotApi.orders())
    } catch {
      // 错误信息由 axios 拦截器统一提示
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const columns: ColumnsType<SpotOrder> = [
    {
      title: '币种',
      dataIndex: 'asset',
      width: 80,
      render: (_: unknown, r: SpotOrder) => <Typography.Text strong>{r.symbol.replace('USDT', '')}</Typography.Text>,
    },
    {
      title: '方向',
      dataIndex: 'side',
      width: 70,
      render: (s: string) => (
        <Tag color={s === 'BUY' ? 'green' : 'red'}>{s === 'BUY' ? '买入' : '卖出'}</Tag>
      ),
    },
    { title: '数量', dataIndex: 'qty', width: 130, render: fmtPrice },
    {
      title: '金额 (U)',
      dataIndex: 'quote_amount',
      width: 100,
      render: (v: number | null) => (v === null ? '-' : v.toLocaleString('en-US')),
    },
    { title: '价格', dataIndex: 'price', width: 120, render: fmtPrice },
    {
      title: '触发',
      dataIndex: 'trigger_type',
      width: 90,
      render: (t: string) => TRIGGER_META[t] || t,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (s: string, r: SpotOrder) => {
        const meta = STATUS_META[s] || { label: s, color: 'default' }
        return (
          <Tooltip title={r.fail_reason || undefined}>
            <Tag color={meta.color}>{meta.label}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '环境',
      dataIndex: 'testnet',
      width: 80,
      render: (t: boolean) => <Tag color={t ? 'blue' : 'gold'}>{t ? '测试网' : '正式网'}</Tag>,
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v: string | null) => (v ? bj(v).format('MM-DD HH:mm:ss') : '-'),
    },
  ]

  return (
    <Card
      size="small"
      title="现货订单"
      extra={
        <Button icon={<ReloadOutlined />} size="small" onClick={load} loading={loading}>
          刷新
        </Button>
      }
    >
      {loading && rows.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin />
        </div>
      ) : rows.length === 0 ? (
        <Empty description="暂无现货订单（P0 仅评分展示，P1 开放信号确认下单）" />
      ) : (
        <Table<SpotOrder>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ y: 'calc(100vh - 320px)' }}
        />
      )}
    </Card>
  )
}
