import { useEffect, useState } from 'react'
import { Card, List, Tag, Typography, Pagination, Empty, Select, Button, Input } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { bj } from '../utils/dayjs'
import { scanApi } from '../api/scan'
import type { AppLog } from '../types'

const { Text, Paragraph } = Typography

// 现货域 INFO 即落库（log_sink 例外口径），级别标签三色区分
const levelTag = (lv: string): string => {
  if (lv === 'ERROR' || lv === 'CRITICAL') return 'error'
  if (lv === 'WARNING') return 'warning'
  return 'processing'
}

/** 现货日志面板：独立组件直连 /logs?logger=app.spot，不与全局「系统日志」共享 store 状态
 * （零侵入约定：现货新增独立组件，互不干扰合约侧过滤器） */
export default function SpotLogsPanel() {
  const [items, setItems] = useState<AppLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [level, setLevel] = useState('')
  const [query, setQuery] = useState('')
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  const fetchData = async (p = page, ps = pageSize, lv = level, q = query) => {
    setLoading(true)
    try {
      const data = await scanApi.logs(p, ps, lv, q, 'app.spot')
      setItems(data.items)
      setTotal(data.total)
      setPage(p)
      setPageSize(ps)
      setLevel(lv)
      setQuery(q)
    } catch (e) {
      console.error('获取现货日志失败', e)
    } finally {
      setLoading(false)
    }
  }

  // 初始加载 + 每 30s 自动刷新当前页
  useEffect(() => {
    fetchData(1)
    const timer = setInterval(() => fetchData(1), 30000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Card
      title="现货日志"
      size="small"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
    >
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', gap: 8 }}>
        <Select
          style={{ width: 130 }}
          value={level || 'all'}
          onChange={(v) => fetchData(1, pageSize, v === 'all' ? '' : v)}
          options={[
            { value: 'all', label: '全部级别' },
            { value: 'INFO', label: '仅 INFO' },
            { value: 'WARNING', label: '仅 WARNING' },
            { value: 'ERROR', label: '仅报错' },
          ]}
        />
        <Input.Search
          placeholder="搜索日志内容"
          allowClear
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onSearch={(v) => fetchData(1, pageSize, level, v.trim())}
          style={{ width: 240 }}
          enterButton="搜索"
        />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => fetchData(1)}>刷新</Button>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <List
          dataSource={items}
          loading={loading}
          locale={{ emptyText: <Empty description="暂无现货日志（app.spot.*，保留 7 天）" /> }}
          renderItem={(item: AppLog) => (
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
          )}
        />
      </div>
      <div style={{ flexShrink: 0, paddingTop: 8, borderTop: '1px solid #f0f0f0' }}>
        <Pagination
          align="end"
          current={page}
          pageSize={pageSize}
          total={total}
          showTotal={(t) => `共 ${t} 条`}
          showSizeChanger
          pageSizeOptions={[20, 50, 100]}
          onChange={(p, ps) => fetchData(p, ps)}
        />
      </div>
    </Card>
  )
}
