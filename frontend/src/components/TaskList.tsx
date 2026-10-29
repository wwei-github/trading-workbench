import { useEffect, useState } from 'react'
import { Button, Card, List, Tag, Typography, Pagination, Empty, Select, Tooltip, Modal, Input, message } from 'antd'
import { DownOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons'
import { bj } from '../utils/dayjs'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import { schemeTag } from '../utils/scheme'
import { observationsApi } from '../api/observations'
import type { ObservationTask, TaskRecord } from '../types'

const { Text } = Typography

// 任务类型展示
const typeMeta: Record<string, { label: string; color: string }> = {
  scan: { label: '扫描', color: 'blue' },
  ai: { label: 'AI', color: 'geekblue' },
  trade: { label: '交易', color: 'purple' },
  review: { label: '复盘', color: 'cyan' },
  spot: { label: '现货', color: 'gold' },
}

// 触发方式
const triggerMeta: Record<string, { label: string; color: string }> = {
  scheduled: { label: '定时', color: 'blue' },
  manual: { label: '手动', color: 'orange' },
  system: { label: '系统', color: 'default' },
}

const statusMeta: Record<string, { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '跳过', color: 'default' },
}

// 复盘结论展示（review 任务 detail.items[].outcome）：盈=涨色、亏=跌色（随全局方案翻转）
const outcomeMeta = (scheme: ColorScheme): Record<string, { label: string; color: string }> => ({
  win_tp1: { label: 'TP1达成', color: schemeTag(scheme).up },
  win_tp2: { label: 'TP2达成', color: schemeTag(scheme).up },
  loss: { label: '止损', color: schemeTag(scheme).down },
  censored: { label: '未触发(删失)', color: 'default' },
  expired: { label: '无效计划', color: 'default' },
})

const DIR_LABEL: Record<string, string> = { long: '多', short: '空' }

// 详情值格式化：对象/数组转 JSON，其余 String
function fmtDetailValue(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// detail.items（复盘明细）渲染：逐条 币种/方向/结论/K线数/评分
function renderReviewItems(items: Record<string, unknown>[], scheme: ColorScheme) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {items.map((it, i) => {
        const o = outcomeMeta(scheme)[String(it.outcome)] || { label: String(it.outcome), color: 'default' }
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <Text strong>{String(it.symbol ?? '-')}</Text>
            <Tag color={it.direction === 'long' ? schemeTag(scheme).up : schemeTag(scheme).down} style={{ marginInlineEnd: 0 }}>
              {DIR_LABEL[String(it.direction)] || '-'}
            </Tag>
            <Tag color={o.color} style={{ marginInlineEnd: 0 }}>{o.label}</Tag>
            <Text type="secondary">
              {it.bars_to_exit != null ? `${it.bars_to_exit} 根K线` : ''}{it.recommendation != null ? ` · 评分 ${it.recommendation}` : ''}
            </Text>
          </div>
        )
      })}
    </div>
  )
}

// detail JSON → 键值行（counts 等扁平对象）；items 数组单独渲染
function renderDetail(detail: Record<string, unknown>, scheme: ColorScheme) {
  const items = detail.items
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
      {Array.isArray(items) && items.length > 0 && renderReviewItems(items as Record<string, unknown>[], scheme)}
      {Array.isArray(items) && items.length === 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>本轮无明细</Text>
      )}
      {Object.entries(detail)
        .filter(([k]) => k !== 'items')
        .map(([k, v]) => (
          <div key={k} style={{ fontSize: 12 }}>
            <Text type="secondary">{k}：</Text>
            <Text>{fmtDetailValue(v)}</Text>
          </div>
        ))}
    </div>
  )
}

export default function TaskList() {
  const {
    tasks, tasksTotal, tasksPage, tasksPageSize, tasksType,
    fetchTasks, fetchResults,
  } = useScanStore()
  // 全局涨跌配色方案（红涨绿跌/绿涨红跌）：复盘结论/方向标签随方案翻转
  const colorScheme = useScanStore((s) => s.colorScheme)
  // 展开查看详情的任务 id 集合
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // 观察期任务（docs/23 §3）：到期在顶部「待决策」区块提醒并记录结论
  const [obs, setObs] = useState<ObservationTask[]>([])
  const [obsDue, setObsDue] = useState(0)
  const [deciding, setDeciding] = useState<ObservationTask | null>(null)
  const [decisionText, setDecisionText] = useState('')
  const [decisionSaving, setDecisionSaving] = useState(false)

  const loadObservations = async () => {
    try {
      const data = await observationsApi.list()
      setObs(data.items)
      setObsDue(data.counts?.due ?? 0)
    } catch {
      // 观察期接口异常不影响任务列表主体
    }
  }

  useEffect(() => {
    void loadObservations()
  }, [])

  const submitDecision = async () => {
    if (!deciding) return
    const text = decisionText.trim()
    if (text.length < 2) {
      message.warning('请填写决策结论与依据')
      return
    }
    setDecisionSaving(true)
    try {
      await observationsApi.decide(deciding.id, text)
      message.success('已归档')
      setDeciding(null)
      setDecisionText('')
      await loadObservations()
    } catch {
      message.error('保存失败')
    } finally {
      setDecisionSaving(false)
    }
  }

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <>
    <Card
      title="任务记录"
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
    >
      {/* 待决策（docs/23 §3）：观察期到期的参数/阈值改动，结论按登记判据核对后归档 */}
      {obs.length > 0 && (
        <div style={{
          flexShrink: 0, marginBottom: 12, padding: '8px 12px', borderRadius: 8,
          border: '1px solid #ffe7ba', background: '#fffbe6',
        }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <Text strong>待决策 {obs.length}</Text>
            {obsDue > 0 && <Tag color="red" style={{ marginInlineEnd: 0 }}>已到期 {obsDue}</Tag>}
            <Text type="secondary" style={{ fontSize: 12 }}>
              观察期结束，按登记判据核对后记录结论
            </Text>
          </div>
          {obs.map((o) => (
            <div key={o.id} style={{ marginTop: 8, paddingTop: 8, borderTop: '1px dashed #f0e6c8' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Tag color={o.status === 'due' ? 'red' : 'blue'} style={{ marginInlineEnd: 0 }}>
                  {o.status === 'due' ? '已到期' : '观察中'}
                </Tag>
                <Text strong>{o.name}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  到期 {bj(o.due_at).format('MM-DD HH:mm')}{o.config_key ? ` · ${o.config_key}` : ''}
                </Text>
                <Button
                  size="small"
                  type="link"
                  style={{ paddingInline: 4 }}
                  onClick={() => { setDeciding(o); setDecisionText('') }}
                >
                  记录决策
                </Button>
              </div>
              {o.decision_rule && (
                <div style={{ fontSize: 12, color: '#8c6d1f', marginTop: 2 }}>判据：{o.decision_rule}</div>
              )}
              {o.metrics && (
                <div style={{ fontSize: 12, color: '#666', marginTop: 2 }}>
                  读数：{Object.entries(o.metrics).slice(0, 4)
                    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
                    .join(' · ')}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <Select
          style={{ width: 130 }}
          value={tasksType || 'all'}
          onChange={(v) => fetchTasks(1, undefined, v === 'all' ? '' : v)}
          options={[
            { value: 'all', label: '全部类型' },
            { value: 'scan', label: '扫描任务' },
            { value: 'ai', label: 'AI 分析任务' },
            { value: 'trade', label: '交易任务' },
            { value: 'review', label: '复盘任务' },
            { value: 'spot', label: '现货任务' },
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => { void fetchTasks(1); void loadObservations() }}>刷新</Button>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <List
          dataSource={tasks}
          locale={{ emptyText: <Empty description="暂无任务记录" /> }}
          renderItem={(item: TaskRecord) => {
            const t = typeMeta[item.task_type] || { label: item.task_type, color: 'default' }
            const g = triggerMeta[item.trigger] || triggerMeta.system
            const s = statusMeta[item.status] || statusMeta.completed
            const failed = item.status === 'failed'
            const clickable = item.task_type === 'scan' && item.scan_record_id
            const hasDetail = Boolean(item.detail && Object.keys(item.detail).length > 0)
            const open = expanded.has(item.id)
            return (
              <List.Item
                style={{ cursor: clickable ? 'pointer' : 'default', display: 'block' }}
                onClick={() => {
                  if (clickable) fetchResults(item.scan_record_id!)
                }}
              >
                <List.Item.Meta
                  title={
                    <span>
                      <Tag color={t.color}>{t.label}</Tag>
                      <Tag color={g.color}>{g.label}</Tag>
                      <Tag color={s.color}>{s.label}</Tag>
                      <Text strong>{item.summary || item.task_name}</Text>
                      {item.task_type === 'scan' && item.scan_record_id && (
                        <Text type="secondary" style={{ marginLeft: 8 }}>点击加载该次扫描结果</Text>
                      )}
                      {/* 详情展开开关：有 detail 或 error 才显示 */}
                      {(hasDetail || (failed && item.error)) && (
                        <Button
                          type="text"
                          size="small"
                          icon={open ? <DownOutlined /> : <RightOutlined />}
                          onClick={(e) => {
                            e.stopPropagation()
                            toggle(item.id)
                          }}
                          style={{ marginLeft: 8, paddingInline: 4 }}
                        >
                          {open ? '收起' : '详情'}
                        </Button>
                      )}
                    </span>
                  }
                  description={
                    <span>
                      <Text type="secondary">{bj(item.started_at).format('YYYY-MM-DD HH:mm:ss')}</Text>
                      {failed && item.error && !open && (
                        <Tooltip title={item.error}>
                          <Text type="danger" style={{ marginLeft: 12 }} ellipsis>
                            {item.error}
                          </Text>
                        </Tooltip>
                      )}
                    </span>
                  }
                />
                {/* 展开区：完整错误 / 任务明细（复盘逐条结论、键值详情） */}
                {open && (
                  <div
                    style={{
                      margin: '4px 0 8px',
                      padding: '8px 12px',
                      background: '#fafafa',
                      borderRadius: 8,
                      border: '1px solid #f0f0f0',
                    }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {failed && item.error && (
                      <div style={{ marginBottom: hasDetail ? 8 : 0 }}>
                        <Text type="danger" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                          {item.error}
                        </Text>
                      </div>
                    )}
                    {hasDetail && renderDetail(item.detail!, colorScheme)}
                  </div>
                )}
              </List.Item>
            )
          }}
        />
      </div>
      <div style={{ flexShrink: 0, paddingTop: 8, borderTop: '1px solid #f0f0f0' }}>
        <Pagination
          align="end"
          current={tasksPage}
          pageSize={tasksPageSize}
          total={tasksTotal}
          showTotal={(t) => `共 ${t} 条`}
          showSizeChanger
          pageSizeOptions={[10, 20, 50]}
          onChange={(page, pageSize) => fetchTasks(page, pageSize)}
        />
      </div>
    </Card>
    <Modal
      open={!!deciding}
      title={`记录决策 · ${deciding?.name ?? ''}`}
      okText="保存并归档"
      cancelText="取消"
      confirmLoading={decisionSaving}
      onOk={() => { void submitDecision() }}
      onCancel={() => { if (!decisionSaving) setDeciding(null) }}
    >
      {deciding?.decision_rule && (
        <div style={{ marginBottom: 8, fontSize: 12, color: '#8c6d1f' }}>
          登记判据：{deciding.decision_rule}
        </div>
      )}
      {deciding && (
        <div style={{ marginBottom: 8, fontSize: 12, color: '#666' }}>
          到期 {bj(deciding.due_at).format('YYYY-MM-DD HH:mm')} · 开始 {bj(deciding.started_at).format('YYYY-MM-DD HH:mm')}
        </div>
      )}
      <Input.TextArea
        rows={4}
        value={decisionText}
        onChange={(e) => setDecisionText(e.target.value)}
        placeholder="结论 + 依据（例：切换；差集 avgR +0.18，SPY 未误放）"
      />
    </Modal>
    </>
  )
}
