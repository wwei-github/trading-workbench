import { useEffect, useRef } from 'react'
import { Card, Row, Col, Button, Space, Segmented, Tabs, message } from 'antd'
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
import SpotOverviewPanel from '../components/SpotOverviewPanel'
import SpotBacktestPanel from '../components/SpotBacktestPanel'
import SpotHistoryPanel from '../components/SpotHistoryPanel'
import SpotSettingsPanel from '../components/SpotSettingsPanel'
import { useScanStore } from '../stores/scanStore'

// 业务域模式（docs/18 §9.1 方案 C）：合约 = 现有全部功能；现货 = 屯币系统（独立模块）
type BizMode = 'futures' | 'spot'

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
  // 业务域记忆：刷新后保持合约/现货模式（useTabMemory 为 string 口径，此处收窄到 BizMode）
  const [modeRaw, setModeRaw] = useTabMemory('tabs.mode', ['futures', 'spot'])
  const mode = modeRaw as BizMode
  const setMode = (v: BizMode) => setModeRaw(v)
  // 合约内部 tab 记忆（既有 key 不动）
  const [activeTab, switchTab] = useTabMemory(
    'tabs.scanResult',
    ['results', 'watchlist', 'search', 'strategy', 'review', 'trades', 'transfers', 'fund-analysis', 'tasks', 'logs'],
  )
  // 现货内部 tab 记忆（独立 key，与合约无冲突）
  const [spotTab, switchSpotTab] = useTabMemory('tabs.spot', ['overview', 'backtest', 'history', 'settings'])

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

  // 立即扫描按钮：置于扫描结果 Tab 内、ResultTable「刷新」左侧
  const scanButton = (
    <Button
      type="primary"
      icon={<ThunderboltOutlined />}
      onClick={handleTrigger}
      disabled={isScanning}
      loading={isScanning}
    >
      {isScanning ? '扫描中...' : '立即扫描'}
    </Button>
  )

  // ── 合约 Tabs（现有 10 个 Tab，原样保留——docs/18 零侵入约束）──
  const futuresItems = [
    {
      key: 'results',
      label: '扫描结果',
      children: <div style={{ height: '100%', overflow: 'hidden' }}><ResultTable headerExtra={scanButton} /></div>,
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
        <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}>
          <StrategyPromptPanel />
        </div>
      ),
    },
    {
      key: 'fund-analysis',
      label: '资金分析',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><FundAnalysisPanel /></div>,
    },
    {
      key: 'trades',
      label: '交易记录',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><TradesPanel /></div>,
    },
    {
      key: 'review',
      label: '复盘',
      children: (
        <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}>
          <Tabs
            defaultActiveKey="actual"
            items={[
              {
                key: 'actual',
                label: '实盘归因',
                // 嵌套 Tabs 的 tabpane 被全局 CSS 锁 height:100%（overflow:hidden），
                // 内容超高会被裁——内层各 tab 统一包纵向滚动容器（横向不滚，宽内容由表格内部滚动消化）
                children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><ReviewActualPanel /></div>,
              },
              {
                key: 'suggestion',
                label: 'AI建议质量',
                children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><ReviewStatsPanel /></div>,
              },
              {
                key: 'patches',
                label: '规则补丁',
                children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><ReviewPatchesPanel /></div>,
              },
              {
                key: 'blacklist',
                label: '币种黑名单',
                children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><BlacklistPanel /></div>,
              },
            ]}
          />
        </div>
      ),
    },
    {
      key: 'transfers',
      label: '资金划转',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><TransfersPanel /></div>,
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
  ]

  // ── 现货 Tabs（docs/18：持仓总览 / 回测 / 历史记录 / 策略配置）──
  const spotItems = [
    {
      key: 'overview',
      label: '持仓总览',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><SpotOverviewPanel /></div>,
    },
    {
      key: 'backtest',
      label: '回测',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><SpotBacktestPanel /></div>,
    },
    {
      key: 'history',
      label: '历史记录',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><SpotHistoryPanel /></div>,
    },
    {
      key: 'settings',
      label: '策略配置',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><SpotSettingsPanel /></div>,
    },
  ]

  const showFutures = mode !== 'spot'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Card style={{ flexShrink: 0, marginBottom: 12 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <Space size={16}>
              <Segmented<BizMode>
                value={mode}
                onChange={(v) => setMode(v)}
                options={[
                  { label: '合约', value: 'futures' },
                  { label: '现货', value: 'spot' },
                ]}
              />
              {showFutures && <ScanStatus />}
            </Space>
          </Col>
          <Col>
            <Space>
              <ColorSchemeToggle />
              {showFutures && (
                <>
                  <ScanConfigPanel />
                  <TradingConfigPanel />
                  <SystemConfigPanel />
                  <EnvironmentTag />
                </>
              )}
            </Space>
          </Col>
        </Row>
      </Card>

      {showFutures ? (
        <Tabs
          activeKey={activeTab}
          onChange={switchTab}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          tabBarStyle={{ flexShrink: 0 }}
          items={futuresItems}
        />
      ) : (
        <Tabs
          activeKey={spotTab}
          onChange={switchSpotTab}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          tabBarStyle={{ flexShrink: 0 }}
          items={spotItems}
        />
      )}
    </div>
  )
}
