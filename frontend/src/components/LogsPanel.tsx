import { useEffect, useState } from 'react'
import { Card, List, Tag, Typography, Pagination, Empty, Select, Button, Input } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'
import type { AppLog } from '../types'

const { Text, Paragraph } = Typography

const levelTag = (lv: string): string => (lv === 'ERROR' || lv === 'CRITICAL' ? 'error' : 'warning')

export default function LogsPanel() {
  const {
    logs, logsTotal, logsPage, logsPageSize, logLevel, logQuery,
    fetchLogs,
  } = useScanStore()
  const [input, setInput] = useState(logQuery)

  // 初始加载 + 每 30s 自动刷新第 1 页
  useEffect(() => {
    fetchLogs(1)
    const timer = setInterval(() => fetchLogs(1), 30000)
    return () => clearInterval(timer)
  }, [])

  const handleSearch = (v: string) => {
    const q = v.trim()
    setInput(q)
    fetchLogs(1, undefined, undefined, q)
  }

  const handleLevel = (v: string) => {
    fetchLogs(1, undefined, v === 'all' ? '' : v)
  }

  return (
    <Card
      title="系统日志"
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
    >
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', gap: 8 }}>
        <Select
          style={{ width: 120 }}
          value={logLevel || 'all'}
          onChange={handleLevel}
          options={[
            { value: 'all', label: '全部级别' },
            { value: 'ERROR', label: '仅报错' },
          ]}
        />
        <Input.Search
          placeholder="搜索报错内容"
          allowClear
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onSearch={handleSearch}
          style={{ width: 240 }}
          enterButton="搜索"
        />
        <Button icon={<ReloadOutlined />} onClick={() => fetchLogs(1)}>刷新</Button>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <List
          dataSource={logs}
          locale={{ emptyText: <Empty description="暂无日志（记录 WARNING 及以上，保留 7 天）" /> }}
          renderItem={(item: AppLog) => {
            return (
              <List.Item>
                <List.Item.Meta
                  title={
                    <span>
                      <Tag color={levelTag(item.level)}>{item.level}</Tag>
                      <Text code style={{ fontSize: 12 }}>{item.logger_name}</Text>
                      <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                        {bj(item.created_at).format('MM-DD HH:mm:ss')}
                      </Text>
                    </span>
                  }
                  description={
                    <Paragraph
                      ellipsis={{ rows: 3, expandable: 'collapsible' }}
                      style={{ marginBottom: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}
                    >
                      {item.message}
                    </Paragraph>
                  }
                />
              </List.Item>
            )
          }}
        />
      </div>
      <div style={{ flexShrink: 0, paddingTop: 8, borderTop: '1px solid #f0f0f0', textAlign: 'right' }}>
        <Pagination
          current={logsPage}
          pageSize={logsPageSize}
          total={logsTotal}
          showTotal={(t) => `共 ${t} 条`}
          showSizeChanger
          pageSizeOptions={[10, 20, 50]}
          onChange={(page, pageSize) => fetchLogs(page, pageSize)}
        />
      </div>
    </Card>
  )
}
