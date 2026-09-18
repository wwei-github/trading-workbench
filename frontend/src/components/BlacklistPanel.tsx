/**
 * 币种黑名单管理（复盘 Tab 子页，2026-09-18）
 * - 两个来源：手动添加（永久）/ 止损金额严重超出计划预算（≥1.2 倍或强平）自动加入（冷却 30 天）
 * - 生效位置：扫描宇宙过滤（主）+ 开仓前兜底；命中的币不进 AI 分析与开单
 * - 操作：手动添加 / 任意行移除（删行）；auto 到期自然失效
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Empty, Form, Input, Modal, Popconfirm, Space, Spin, Table, Tag, Typography, message,
} from 'antd'
import { PlusOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import type { BlacklistItem } from '../types'

const { Text } = Typography

const ORIGIN_META: Record<string, { color: string; label: string }> = {
  manual: { color: 'purple', label: '手动' },
  auto: { color: 'orange', label: '自动（止损超预算）' },
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return bj(s).format('MM-DD HH:mm')
}

export default function BlacklistPanel() {
  const [rows, setRows] = useState<BlacklistItem[]>([])
  const [loading, setLoading] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await scanApi.blacklist.list())
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取黑名单失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const handleAdd = async () => {
    try {
      const v = await form.validateFields()
      const symbol = v.symbol.trim().toUpperCase()
      await scanApi.blacklist.add(symbol.endsWith('USDT') ? symbol : `${symbol}USDT`, v.reason)
      message.success(`${symbol} 已加入黑名单`)
      setAddOpen(false)
      form.resetFields()
      fetchData()
    } catch (e: any) {
      if (e?.errorFields) return // 表单校验错误
      message.error(e?.response?.data?.detail || '添加失败')
    }
  }

  const handleRemove = async (symbol: string) => {
    try {
      await scanApi.blacklist.remove(symbol)
      message.success(`${symbol} 已移除`)
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '移除失败')
    }
  }

  const columns: ColumnsType<BlacklistItem> = [
    { title: '币种', dataIndex: 'symbol', key: 'symbol', width: 130,
      render: (v: string) => <Text strong>{v}</Text> },
    { title: '来源', dataIndex: 'origin', key: 'origin', width: 150,
      render: (v: string) => <Tag color={ORIGIN_META[v]?.color}>{ORIGIN_META[v]?.label || v}</Tag> },
    { title: '状态', dataIndex: 'active', key: 'active', width: 100,
      render: (_: boolean, r: BlacklistItem) =>
        r.active ? <Tag color="red">生效中</Tag> : r.expired ? <Tag>已过期</Tag> : <Tag>已失效</Tag> },
    { title: '原因/备注', dataIndex: 'reason', key: 'reason',
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v || '-'}</Text> },
    { title: '到期时间', dataIndex: 'expires_at', key: 'expires_at', width: 130,
      render: (v: string | null, r: BlacklistItem) => (r.origin === 'manual' ? '永久' : fmtTime(v)) },
    { title: '加入时间', dataIndex: 'created_at', key: 'created_at', width: 130, render: fmtTime },
    { title: '操作', key: 'op', width: 90,
      render: (_: unknown, r: BlacklistItem) => (
        <Popconfirm title={`移除 ${r.symbol}？`} description="移除后该币种恢复进入扫描与开单"
                    onConfirm={() => handleRemove(r.symbol)}>
          <Button size="small" danger icon={<StopOutlined />}>移除</Button>
        </Popconfirm>
      ) },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card size="small">
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            黑名单币种不进扫描候选（也就不会 AI 分析/开单）；实际亏损超出计划风险预算 20% 以上
            或被强平的币自动拉黑冷却 30 天，可随时手动移除
          </Text>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchData}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
              添加黑名单
            </Button>
          </Space>
        </Space>
      </Card>

      <Table<BlacklistItem>
        rowKey="id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        size="small"
        scroll={{ y: 400 }}
        pagination={false}
        locale={{ emptyText: <Empty description="黑名单为空" style={{ marginTop: 40 }} /> }}
      />

      <Modal
        title="添加黑名单"
        open={addOpen}
        onOk={handleAdd}
        onCancel={() => { setAddOpen(false); form.resetFields() }}
        okText="添加"
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="symbol" label="币种" rules={[{ required: true, message: '请输入币种' }]}
                     extra="如 4USDT 或 4（自动补 USDT）">
            <Input placeholder="4USDT" maxLength={20} />
          </Form.Item>
          <Form.Item name="reason" label="原因/备注（可选）">
            <Input.TextArea placeholder="如：盘口过薄/惯犯/庄控严重" rows={2} maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
