import { Card, List, Tag, Typography, Pagination, Empty, Select, Button, Tooltip } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
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

export default function TaskList() {
  const {
    tasks, tasksTotal, tasksPage, tasksPageSize, tasksType,
    fetchTasks, fetchResults,
  } = useScanStore()

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
            return (
              <List.Item
                style={{ cursor: clickable ? 'pointer' : 'default' }}
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
                    </span>
                  }
                  description={
                    <span>
                      <Text type="secondary">{bj(item.started_at).format('YYYY-MM-DD HH:mm:ss')}</Text>
                      {failed && item.error && (
                        <Tooltip title={item.error}>
                          <Text type="danger" style={{ marginLeft: 12 }} ellipsis>
                            {item.error}
                          </Text>
                        </Tooltip>
                      )}
                    </span>
                  }
                />
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
