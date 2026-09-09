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
    triggerAiAnalysis,
    fetchAiAnalyses,
  } = useScanStore()

  const wasScanning = useRef(false)

  useEffect(() => {
    // 初始加载
    fetchStatus()
    fetchResults().then(() => {
      // fetchResults 完成后，如果有 currentScanId 且 AI 开启，会自动加载 AI 分析
    })
    fetchHistory()

    // 每 30 秒刷新状态
    const timer = setInterval(() => {
      fetchStatus()
    }, 30000)
    return () => clearInterval(timer)
  }, [])

  // 扫描状态变化处理
  useEffect(() => {
    const isScanning = !!status?.is_scanning
    // 扫描完成时自动触发 AI 分析
    if (wasScanning.current && !isScanning && aiEnabled && currentScanId) {
      // 先刷新结果
      fetchResults(currentScanId).then(() => {
        triggerAiAnalysis(currentScanId)
          .then(() => {
            message.success('扫描完成，已自动触发 AI 分析')
          })
          .catch((e: any) => {
            message.error(e?.message || 'AI 分析触发失败')
          })
      })
    }
    // 扫描进行中时持续刷新结果
    if (isScanning) {
      fetchResults()
    }
    wasScanning.current = isScanning
  }, [status?.is_scanning])

  const handleTrigger = async () => {
    // 前端也检查是否正在扫描
    if (status?.is_scanning) {
      message.warning('扫描进行中，请等待完成')
      return
    }
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

  const isScanning = !!status?.is_scanning

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
                disabled={isScanning}
                loading={isScanning}
              >
                {isScanning ? '扫描中...' : '立即扫描'}
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
