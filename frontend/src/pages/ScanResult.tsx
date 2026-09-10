import { useEffect, useRef } from 'react'
import { Card, Row, Col, Button, Space, Switch, Tooltip, Tabs, message } from 'antd'
import { ReloadOutlined, ThunderboltOutlined, RobotOutlined, FileTextOutlined } from '@ant-design/icons'
import ScanStatus from '../components/ScanStatus'
import ResultTable from '../components/ResultTable'
import HistoryList from '../components/HistoryList'
import ScanConfigPanel from '../components/ScanConfigPanel'
import StrategyPromptPanel from '../components/StrategyPromptPanel'
import { useScanStore } from '../stores/scanStore'

export default function ScanResult() {
  const {
    fetchStatus,
    fetchResults,
    fetchHistory,
    fetchConfig,
    fetchAiAnalyses,
    triggerScan,
    updateConfig,
    status,
    aiConfig,
    toggleAi,
    currentScanId,
  } = useScanStore()

  const wasScanning = useRef(false)

  useEffect(() => {
    // 初始加载：状态、配置、结果、历史
    fetchStatus()
    fetchConfig().then(() => {
      // fetchResults 内部会根据 AI 开关加载已有 AI 分析结果（不自动触发）
      fetchResults()
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
    const aiEnabled = !!aiConfig?.ai_analysis_enabled
    // 扫描完成时刷新结果
    if (wasScanning.current && !isScanning && aiEnabled && currentScanId) {
      fetchResults(currentScanId).then(() => {
        message.success('扫描完成')
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

  const handleRefresh = async () => {
    // 先刷新状态拿到最新扫描记录 ID，再拉取该扫描的结果
    await fetchStatus()
    const latestScanId = useScanStore.getState().status?.last_scan?.id
    if (latestScanId) {
      await fetchResults(latestScanId)
    } else {
      fetchResults()
    }
    fetchHistory()
  }

  const handleAiToggle = async (checked: boolean) => {
    try {
      await toggleAi(checked)
      message.success(checked ? 'AI 分析已开启' : 'AI 分析已关闭')
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '切换 AI 开关失败')
    }
  }

  const handleStrategyToggle = async (checked: boolean) => {
    try {
      await updateConfig({ strategy_prompt_enabled: checked })
      message.success(checked ? '策略开关已开启，AI 分析时将携带策略' : '策略开关已关闭')
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '切换策略开关失败')
    }
  }

  const isScanning = !!status?.is_scanning
  const aiEnabled = !!aiConfig?.ai_analysis_enabled
  const aiConfigured = !!aiConfig?.ai_configured
  const strategyEnabled = !!aiConfig?.strategy_prompt_enabled
  const hasStrategy = !!aiConfig?.strategy_prompt

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Card style={{ flexShrink: 0, marginBottom: 12 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <ScanStatus />
          </Col>
          <Col>
            <Space>
              <Tooltip
                title={
                  aiConfigured
                    ? '开启后展示各币种的 AI 分析结果（展开行查看），可在展开行中点击"AI 分析"手动触发'
                    : '后端未配置 AI_API_KEY，无法使用 AI 分析'
                }
              >
                <Space>
                  <RobotOutlined style={{ fontSize: 16 }} />
                  <Switch
                    checkedChildren="AI"
                    unCheckedChildren="AI"
                    checked={aiEnabled}
                    onChange={handleAiToggle}
                    disabled={!aiConfigured}
                  />
                </Space>
              </Tooltip>
              <Tooltip
                title={
                  hasStrategy
                    ? '开启后，AI 分析时将"策略提示词"一并发给 AI 参考'
                    : '请先在"策略提示词"页签中编写并保存策略'
                }
              >
                <Space>
                  <FileTextOutlined style={{ fontSize: 16 }} />
                  <Switch
                    checkedChildren="策略"
                    unCheckedChildren="策略"
                    checked={strategyEnabled}
                    onChange={handleStrategyToggle}
                  />
                </Space>
              </Tooltip>
              <ScanConfigPanel />
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

      <Tabs
        defaultActiveKey="results"
        style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        tabBarStyle={{ flexShrink: 0 }}
        items={[
          {
            key: 'results',
            label: '扫描结果',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><ResultTable /></div>,
          },
          {
            key: 'history',
            label: '历史记录',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><HistoryList /></div>,
          },
          {
            key: 'strategy',
            label: '策略提示词',
            children: (
              <div style={{ height: '100%', overflow: 'auto' }}>
                <StrategyPromptPanel />
              </div>
            ),
          },
        ]}
      />
    </div>
  )
}
