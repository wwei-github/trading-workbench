import { useEffect, useRef } from 'react'
import { Card, Row, Col, Button, Space, Switch, Tooltip, message } from 'antd'
import { ReloadOutlined, ThunderboltOutlined, RobotOutlined } from '@ant-design/icons'
import ScanStatus from '../components/ScanStatus'
import ResultTable from '../components/ResultTable'
import HistoryList from '../components/HistoryList'
import { useScanStore } from '../stores/scanStore'

export default function ScanResult() {
  const {
    fetchStatus,
    fetchResults,
    fetchHistory,
    triggerScan,
    loading,
    status,
    aiEnabled,
    setAiEnabled,
    currentScanId,
    aiAnalyses,
    fetchAiAnalyses,
    triggerAiAnalysis,
  } = useScanStore()

  const wasScanning = useRef(false)

  useEffect(() => {
    fetchStatus()
    fetchResults()
    fetchHistory()
    if (currentScanId) fetchAiAnalyses(currentScanId)
    // 每 30 秒刷新状态
    const timer = setInterval(() => {
      fetchStatus()
      if (status?.is_scanning) fetchResults()
    }, 30000)
    return () => clearInterval(timer)
  }, [])

  // 扫描完成时自动触发 AI 分析
  useEffect(() => {
    if (wasScanning.current && !status?.is_scanning && aiEnabled && currentScanId) {
      triggerAiAnalysis(currentScanId)
        .then(() => {
          message.success('扫描完成，已自动触发 AI 分析')
          setTimeout(() => fetchAiAnalyses(currentScanId), 5000)
        })
        .catch((e: any) => {
          message.error(e?.message || 'AI 分析触发失败')
        })
    }
    wasScanning.current = !!status?.is_scanning
  }, [status?.is_scanning])

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
    if (currentScanId) fetchAiAnalyses(currentScanId)
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
              <Tooltip title="开启后，每次扫描完成自动将命中币种发给 AI 分析">
                <Space>
                  <RobotOutlined style={{ fontSize: 16 }} />
                  <Switch
                    checkedChildren="AI"
                    unCheckedChildren="AI"
                    checked={aiEnabled}
                    onChange={setAiEnabled}
                  />
                </Space>
              </Tooltip>
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
