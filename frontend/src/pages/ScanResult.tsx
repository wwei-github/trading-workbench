import { useEffect } from 'react'
import { Card, Row, Col, Button, Space, message } from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import ScanStatus from '../components/ScanStatus'
import ResultTable from '../components/ResultTable'
import HistoryList from '../components/HistoryList'
import { useScanStore } from '../stores/scanStore'

export default function ScanResult() {
  const { fetchStatus, fetchResults, fetchHistory, triggerScan, loading, status } = useScanStore()

  useEffect(() => {
    fetchStatus()
    fetchResults()
    fetchHistory()
    // 每 30 秒刷新状态
    const timer = setInterval(() => {
      fetchStatus()
      if (status?.is_scanning) fetchResults()
    }, 30000)
    return () => clearInterval(timer)
  }, [])

  const handleTrigger = async () => {
    try {
      await triggerScan()
      message.success('扫描任务已提交，请稍候...')
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '触发扫描失败')
    }
  }

  const handleRefresh = () => {
    fetchStatus()
    fetchResults()
    fetchHistory()
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card>
        <Row justify="space-between" align="middle">
          <Col>
            <ScanStatus />
          </Col>
          <Col>
            <Space>
              <Button icon={<ReloadOutlined />} onClick={handleRefresh}>
                刷新
              </Button>
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                onClick={handleTrigger}
                loading={status?.is_scanning}
              >
                立即扫描
              </Button>
            </Space>
          </Col>
        </Row>
      </Card>

      <ResultTable />

      <HistoryList />
    </Space>
  )
}
