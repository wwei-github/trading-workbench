/**
 * 战法管理面板（2026-10-02）
 * - 用户自建"战术剧本"（名称+剧本描述），AI 分析时对照判定信号是否符合并落库标注
 * - 战法仅作借鉴参考（不做硬闸不拦截开单），复盘按战法分组对比实盘胜率/EV
 * - 适用方向（多/空/全部）与适用环境（测试网/正式网/双环境）过滤注入
 * - 启停开关：停用后不再注入 AI 判定，历史命中标注保留；被引用的战法不可删除
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Empty, Form, Input, Modal, Popconfirm, Radio, Space, Spin, Switch,
  Table, Tag, Typography, message,
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { tacticsApi, type TacticPayload } from '../api/tactics'
import { bj } from '../utils/dayjs'
import type { Tactic } from '../types'

const { Text } = Typography

const DIRECTION_META: Record<Tactic['direction'], { color: string; label: string }> = {
  all: { color: 'default', label: '多空通用' },
  long: { color: 'red', label: '仅做多' },
  short: { color: 'green', label: '仅做空' },
}
const SCOPE_META: Record<Tactic['scope'], { color: string; label: string }> = {
  testnet: { color: 'gold', label: '测试网' },
  live: { color: 'red', label: '正式网' },
  both: { color: 'blue', label: '双环境' },
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return bj(s).format('MM-DD HH:mm')
}

export default function TacticsPanel() {
  const [rows, setRows] = useState<Tactic[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Tactic | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await tacticsApi.list())
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取战法列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (t: Tactic) => {
    setEditing(t)
    form.setFieldsValue({
      name: t.name,
      description: t.description,
      direction: t.direction,
      scope: t.scope,
    })
    setModalOpen(true)
  }

  const handleSave = async () => {
    try {
      const v: TacticPayload = await form.validateFields()
      setSaving(true)
      if (editing) {
        await tacticsApi.update(editing.id, v)
        message.success('战法已更新')
      } else {
        await tacticsApi.create(v)
        message.success('战法已创建（默认停用，请启用后参与 AI 判定）')
      }
      setModalOpen(false)
      fetchData()
    } catch (e: any) {
      if (e?.errorFields) return
      message.error(e?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleToggle = async (t: Tactic, enabled: boolean) => {
    try {
      await tacticsApi.toggle(t.id, enabled)
      setRows((prev) => prev.map((r) => (r.id === t.id ? { ...r, enabled } : r)))
      message.success(`${t.name} 已${enabled ? '启用' : '停用'}`)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '操作失败')
    }
  }

  const handleRemove = async (t: Tactic) => {
    try {
      await tacticsApi.remove(t.id)
      message.success(`${t.name} 已删除`)
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '删除失败')
    }
  }

  const columns: ColumnsType<Tactic> = [
    { title: '战法名', dataIndex: 'name', key: 'name', width: 160,
      render: (v: string, r: Tactic) => (
        <Space size={4}>
          <Text strong>{v}</Text>
          {!r.enabled && <Tag color="default" style={{ marginInlineEnd: 0 }}>停用</Tag>}
        </Space>
      ) },
    { title: '战法剧本', dataIndex: 'description', key: 'description',
      ellipsis: { showTitle: false },
      render: (v: string) => (
        <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: v }}>
          {v}
        </Text>
      ) },
    { title: '方向', dataIndex: 'direction', key: 'direction', width: 100,
      render: (v: Tactic['direction']) => <Tag color={DIRECTION_META[v]?.color}>{DIRECTION_META[v]?.label || v}</Tag> },
    { title: '环境', dataIndex: 'scope', key: 'scope', width: 100,
      render: (v: Tactic['scope']) => <Tag color={SCOPE_META[v]?.color}>{SCOPE_META[v]?.label || v}</Tag> },
    { title: '命中数', dataIndex: 'trade_count', key: 'trade_count', width: 80,
      align: 'right',
      render: (v: number, r: Tactic) => (
        <Text type={r.enabled ? undefined : 'secondary'} strong={r.enabled}>{v}</Text>
      ) },
    { title: '启用', dataIndex: 'enabled', key: 'enabled', width: 70,
      render: (_: boolean, r: Tactic) => (
        <Switch size="small" checked={r.enabled} onChange={(on) => handleToggle(r, on)} />
      ) },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 120, render: fmtTime },
    { title: '操作', key: 'op', width: 130,
      render: (_: unknown, r: Tactic) => (
        <Space size={4}>
          <Button size="small" type="link" style={{ paddingInline: 4 }} onClick={() => openEdit(r)}>编辑</Button>
          <Popconfirm
            title={`删除 ${r.name}？`}
            description={r.trade_count > 0 ? '已有分析引用，删除将被拒绝（请改为停用）' : '删除后不可恢复'}
            onConfirm={() => handleRemove(r)}>
            <Button size="small" type="link" danger style={{ paddingInline: 4 }}>删除</Button>
          </Popconfirm>
        </Space>
      ) },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card size="small">
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            战法是自建的"战术剧本"：AI 分析时逐条对照判定信号是否符合（命中仅在分析卡与复盘打标，
            不拦截开单）；复盘按战法分组对比实盘胜率/期望——哪个战法真能打，数据说话。
            仅启用的战法参与判定；按适用环境（测试网/正式网/双环境）随当前交易账号所在网络注入
          </Text>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchData}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建战法</Button>
          </Space>
        </Space>
      </Card>

      <Table<Tactic>
        rowKey="id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        size="small"
        scroll={{ y: 400 }}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有战法，点右上角「新建战法」创建" style={{ marginTop: 40 }} /> }}
      />

      <Modal
        title={editing ? `编辑战法：${editing.name}` : '新建战法'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => { setModalOpen(false); form.resetFields() }}
        okText={editing ? '保存' : '创建'}
        confirmLoading={saving}
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ direction: 'all', scope: 'both' }}>
          <Form.Item name="name" label="战法名"
                     rules={[{ required: true, message: '请输入战法名' },
                             { max: 64, message: '不超过 64 字' }]}
                     extra="AI 命中后将逐字标注此名，建议简短明确（如：EMA20回踩做多）">
            <Input placeholder="如：EMA20回踩做多" maxLength={64} />
          </Form.Item>
          <Form.Item name="description" label="战法剧本（AI 判定的唯一依据）"
                     rules={[{ required: true, message: '请输入战法剧本' },
                             { max: 2000, message: '不超过 2000 字' }]}
                     extra="描述入场条件、结构要求、确认信号等；写得越具体 AI 判定越稳">
            <Input.TextArea
              placeholder={'如：多头排列（EMA21>55>144）中价格回踩 EMA20/21 附近（±1%内），\n出现看涨确认K（锤形/吞没/启明星），量能不高于 2× 均量（放量回踩无效）；\n止损放 EMA55 下方，仅顺 4h 背景方向。'}
              rows={8} maxLength={2000} showCount />
          </Form.Item>
          <Space size={24} wrap>
            <Form.Item name="direction" label="适用方向" style={{ marginBottom: 8 }}>
              <Radio.Group
                options={[
                  { label: '多空通用', value: 'all' },
                  { label: '仅做多', value: 'long' },
                  { label: '仅做空', value: 'short' },
                ]}
                optionType="button" buttonStyle="solid" />
            </Form.Item>
            <Form.Item name="scope" label="适用环境" style={{ marginBottom: 8 }}>
              <Radio.Group
                options={[
                  { label: '测试网', value: 'testnet' },
                  { label: '正式网', value: 'live' },
                  { label: '双环境', value: 'both' },
                ]}
                optionType="button" buttonStyle="solid" />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  )
}
