import { useCallback, useEffect, useState } from 'react'
import { Button, Modal, Space, Table, Tag, Tooltip, Typography, message } from 'antd'
import { HistoryOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import type { ConfigChangeItem } from '../types'
import { useScanStore } from '../stores/scanStore'
import { bj } from '../utils/dayjs'

/** 空值/空串占位：「—」表示此前未定制（NULL），与「空字符串」区分开 */
const showVal = (v: string | null) => (v === null || v === '' ? '—' : v)

/** 配置变更记录（docs/23 T14a，2026-10-29）：逐字段回看「哪个开关何时被改成什么」。
 *  与审计表同源（GET /scans/config/changes，时间倒序），另附配置行 updated_at 水位
 *  ——排查「改了配置没生效」时先看这里：时间对得上说明改动已落库，问题在消费侧。 */
export default function ConfigChangesPanel() {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState<ConfigChangeItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const { aiConfig, fetchConfig } = useScanStore()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      // 并行刷新配置（updated_at 水位）与变更列表
      const [res] = await Promise.all([scanApi.configChanges(200), fetchConfig()])
      setRows(res.items)
      setTotal(res.total)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '配置变更加载失败')
    } finally {
      setLoading(false)
    }
  }, [fetchConfig])

  useEffect(() => {
    if (open) load()
  }, [open, load])

  const columns: ColumnsType<ConfigChangeItem> = [
    {
      title: '时间（北京）',
      dataIndex: 'created_at',
      width: 165,
      render: (v: string) => bj(v).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '字段',
      dataIndex: 'field',
      width: 220,
      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
    },
    {
      title: '旧值 → 新值',
      key: 'change',
      render: (_: unknown, r) => (
        <Space size={6} wrap>
          <span style={{ color: '#999' }}>{showVal(r.old_value)}</span>
          <span style={{ color: '#999' }}>→</span>
          <span style={{ fontWeight: 600 }}>{showVal(r.new_value)}</span>
        </Space>
      ),
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 150,
      render: (v: string | null) =>
        v ? <Tag style={{ marginInlineEnd: 0 }}>{v}</Tag> : <span style={{ color: '#999' }}>未知</span>,
    },
  ]

  const updatedAt = aiConfig?.updated_at

  return (
    <>
      <Tooltip title="回看配置项被改动的时间与新旧值（审计表同源）">
        <Button icon={<HistoryOutlined />} onClick={() => setOpen(true)}>
          配置变更
        </Button>
      </Tooltip>
      <Modal
        title="配置变更记录"
        open={open}
        onCancel={() => setOpen(false)}
        footer={[
          <Button key="reload" icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>,
          <Button key="close" type="primary" onClick={() => setOpen(false)}>
            关闭
          </Button>,
        ]}
        width={900}
      >
        <div style={{ marginBottom: 12, fontSize: 12, color: '#999' }}>
          配置更新于{' '}
          {updatedAt ? `${bj(updatedAt).format('YYYY-MM-DD HH:mm:ss')}（北京时间）` : '未记录'}
          {total > rows.length && ` · 仅显示最近 ${rows.length}/${total} 条`}
        </div>
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, size: 'small' }}
          scroll={{ y: 420 }}
          locale={{ emptyText: '暂无配置变更记录' }}
        />
      </Modal>
    </>
  )
}
