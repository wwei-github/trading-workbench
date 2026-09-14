import { useState } from 'react'
import { Button, Card, List, Tag, Typography, Pagination, Empty, Select, Tooltip } from 'antd'
import { DownOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'
import type { TaskRecord } from '../types'

const { Text } = Typography

// 任务类型展示
const typeMeta: Record<string, { label: string; color: string }> = {
  scan: { label: '扫描', color: 'blue' },
  ai: { label: 'AI', color: 'geekblue' },
  trade: { label: '交易', color: 'purple' },
  review: { label: '复盘', color: 'cyan' },
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

// 复盘结论展示（review 任务 detail.items[].outcome）
const OUTCOME_META: Record<string, { label: string; color: string }> = {
  win_tp1: { label: 'TP1达成', color: 'green' },
  win_tp2: { label: 'TP2达成', color: 'green' },
  loss: { label: '止损', color: 'red' },
  expired: { label: '未定论', color: 'default' },
}

const DIR_LABEL: Record<string, string> = { long: '多', short: '空' }

// 详情值格式化：对象/数组转 JSON，其余 String
function fmtDetailValue(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// detail.items（复盘明细）渲染：逐条 币种/方向/结论/K线数/评分
function renderReviewItems(items: Record<string, unknown>[]) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {items.map((it, i) => {
        const o = OUTCOME_META[String(it.outcome)] || { label: String(it.outcome), color: 'default' }
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <Text strong>{String(it.symbol ?? '-')}</Text>
            <Tag color={it.direction === 'long' ? 'green' : 'red'} style={{ marginInlineEnd: 0 }}>
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
function renderDetail(detail: Record<string, unknown>) {
  const items = detail.items
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
      {Array.isArray(items) && items.length > 0 && renderReviewItems(items as Record<string, unknown>[])}
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
  // 展开查看详情的任务 id 集合
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <Card
      title="任务记录"
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
    >
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
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => fetchTasks(1)}>刷新</Button>
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
                    {hasDetail && renderDetail(item.detail!)}
                  </div>
                )}
              </List.Item>
            )
          }}
        />
      </div>
      <div style={{ flexShrink: 0, paddingTop: 8, borderTop: '1px solid #f0f0f0', textAlign: 'right' }}>
        <Pagination
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
  )
}
