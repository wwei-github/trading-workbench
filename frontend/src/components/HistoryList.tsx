import { Card, List, Tag, Typography, Pagination, Empty } from 'antd'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'

const { Text } = Typography

const typeMap: Record<string, { label: string; color: string }> = {
  scheduled: { label: '定时', color: 'blue' },
  manual: { label: '手动', color: 'orange' },
}

const statusMap: Record<string, { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
}

export default function HistoryList() {
  const {
    history,
    historyTotal,
    historyPage,
    historyPageSize,
    fetchHistory,
    fetchResults,
  } = useScanStore()

  return (
    <Card title="历史扫描记录" size="small">
      <List
        dataSource={history}
        locale={{ emptyText: <Empty description="暂无历史记录（仅显示最近 24 小时）" /> }}
        renderItem={(item) => {
          const t = typeMap[item.scan_type] || typeMap.manual
          const s = statusMap[item.status] || statusMap.completed
          return (
            <List.Item
              style={{ cursor: 'pointer' }}
              onClick={() => fetchResults(item.id)}
            >
              <List.Item.Meta
                title={
                  <span>
                    <Tag color={t.color}>{t.label}</Tag>
                    <Tag color={s.color}>{s.label}</Tag>
                    <Text>命中 {item.hit_count} 个</Text>
                    <Text type="secondary" style={{ marginLeft: 12 }}>
                      扫描 {item.coin_count} 个
                    </Text>
                  </span>
                }
                description={bj(item.started_at).format('YYYY-MM-DD HH:mm:ss')}
              />
            </List.Item>
          )
        }}
      />
      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Pagination
          current={historyPage}
          pageSize={historyPageSize}
          total={historyTotal}
          showTotal={(t) => `共 ${t} 条`}
          showSizeChanger
          pageSizeOptions={[10, 20, 50]}
          onChange={(page, pageSize) => fetchHistory(page, pageSize)}
        />
      </div>
    </Card>
  )
}
