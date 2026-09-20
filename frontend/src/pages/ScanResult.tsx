import { useEffect, useRef } from 'react'
import { Card, Row, Col, Button, Space, Tabs, message } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useTabMemory } from '../utils/tabMemory'
import ScanStatus from '../components/ScanStatus'
import ResultTable from '../components/ResultTable'
import TaskList from '../components/TaskList'
import LogsPanel from '../components/LogsPanel'
import ScanConfigPanel from '../components/ScanConfigPanel'
import TradingConfigPanel from '../components/TradingConfigPanel'
import SystemConfigPanel from '../components/SystemConfigPanel'
import EnvironmentTag from '../components/EnvironmentTag'
import StrategyPromptPanel from '../components/StrategyPromptPanel'
import WatchlistPanel from '../components/WatchlistPanel'
import SearchPanel from '../components/SearchPanel'
import ReviewStatsPanel from '../components/ReviewStatsPanel'
import ReviewActualPanel from '../components/ReviewActualPanel'
import ReviewPatchesPanel from '../components/ReviewPatchesPanel'
import BlacklistPanel from '../components/BlacklistPanel'
import TradesPanel from '../components/TradesPanel'
import TransfersPanel from '../components/TransfersPanel'
import FundAnalysisPanel from '../components/FundAnalysisPanel'
import ColorSchemeToggle from '../components/ColorSchemeToggle'
import { useScanStore } from '../stores/scanStore'

export default function ScanResult() {
  const {
    fetchStatus,
    fetchResults,
    fetchTasks,
    fetchConfig,
    fetchAiAnalyses,
    triggerScan,
    status,
    aiConfig,
    currentScanId,
  } = useScanStore()

  const wasScanning = useRef(false)
  // Tab 记忆：刷新后保留刷新前的聚焦 tab
  const [activeTab, switchTab] = useTabMemory(
    'tabs.scanResult',
    ['results', 'watchlist', 'search', 'strategy', 'review', 'trades', 'transfers', 'fund-analysis', 'tasks', 'logs'],
  )

  useEffect(() => {
    // 初始加载：状态、配置、结果、任务记录
    fetchStatus()
    fetchConfig().then(() => {
      // fetchResults 内部会根据 AI 开关加载已有 AI 分析结果（不自动触发）
      fetchResults()
    })
    fetchTasks()

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

  const isScanning = !!status?.is_scanning

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Card style={{ flexShrink: 0, marginBottom: 12 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <ScanStatus />
          </Col>
          <Col>
            <Space>
              <ColorSchemeToggle />
              <ScanConfigPanel />
              <TradingConfigPanel />
              <SystemConfigPanel />
              <EnvironmentTag />
            </Space>
          </Col>
        </Row>
      </Card>

      <Tabs
        activeKey={activeTab}
        onChange={switchTab}
        style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        tabBarStyle={{ flexShrink: 0 }}
        items={[
          {
            key: 'results',
            label: '扫描结果',
            children: (
              <div style={{ height: '100%', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
                <div style={{ flexShrink: 0, display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
                  <Button
                    type="primary"
                    icon={<ThunderboltOutlined />}
                    onClick={handleTrigger}
                    disabled={isScanning}
                    loading={isScanning}
                  >
                    {isScanning ? '扫描中...' : '立即扫描'}
                  </Button>
                </div>
                <div style={{ flex: 1, minHeight: 0 }}>
                  <ResultTable />
                </div>
              </div>
            ),
          },
          {
            key: 'watchlist',
            label: '关注列表',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><WatchlistPanel /></div>,
          },
          {
            key: 'search',
            label: '搜索币种',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><SearchPanel /></div>,
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
          {
            key: 'review',
            label: '复盘',
            children: (
              <div style={{ height: '100%', overflow: 'auto' }}>
                <Tabs
                  defaultActiveKey="actual"
                  items={[
                    {
                      key: 'actual',
                      label: '实盘归因',
                      children: <ReviewActualPanel />,
                    },
                    {
                      key: 'suggestion',
                      label: 'AI建议质量',
                      children: <ReviewStatsPanel />,
                    },
                    {
                      key: 'patches',
                      label: '规则补丁',
                      children: <ReviewPatchesPanel />,
                    },
                    {
                      key: 'blacklist',
                      label: '币种黑名单',
                      children: <BlacklistPanel />,
                    },
                  ]}
                />
              </div>
            ),
          },
          {
            key: 'trades',
            label: '交易记录',
            children: <div style={{ height: '100%', overflow: 'auto' }}><TradesPanel /></div>,
          },
          {
            key: 'transfers',
            label: '资金划转',
            children: <div style={{ height: '100%', overflow: 'auto' }}><TransfersPanel /></div>,
          },
          {
            key: 'fund-analysis',
            label: '资金分析',
            children: <div style={{ height: '100%', overflow: 'auto' }}><FundAnalysisPanel /></div>,
          },
          {
            key: 'tasks',
            label: '任务记录',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><TaskList /></div>,
          },
          {
            key: 'logs',
            label: '系统日志',
            children: <div style={{ height: '100%', overflow: 'hidden' }}><LogsPanel /></div>,
          },
        ]}
      />
    </div>
  )
}
