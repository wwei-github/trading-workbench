import { useEffect, useRef } from 'react'
import { Card, Row, Col, Button, Space, Tabs, message, Segmented } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useTabMemory } from '../utils/tabMemory'
import ScanStatus from '../components/ScanStatus'
import ResultTable from '../components/ResultTable'
import PendingPoolPanel from '../components/PendingPoolPanel'
import TaskList from '../components/TaskList'
import LogsPanel from '../components/LogsPanel'
import CodeWikiPanel from '../components/CodeWikiPanel'
import ScanConfigPanel from '../components/ScanConfigPanel'
import TradingConfigPanel from '../components/TradingConfigPanel'
import SystemConfigPanel from '../components/SystemConfigPanel'
import StrategyPromptPanel from '../components/StrategyPromptPanel'
import TacticsPanel from '../components/TacticsPanel'
import WatchlistPanel from '../components/WatchlistPanel'
import SearchPanel from '../components/SearchPanel'
import ReviewStatsPanel from '../components/ReviewStatsPanel'
import ReviewActualPanel from '../components/ReviewActualPanel'
import ReviewPatchesPanel from '../components/ReviewPatchesPanel'
import BlacklistPanel from '../components/BlacklistPanel'
import ShadowPanel from '../components/ShadowPanel'
import TradesPanel from '../components/TradesPanel'
import TransfersPanel from '../components/TransfersPanel'
import FundAnalysisPanel from '../components/FundAnalysisPanel'
import ColorSchemeToggle from '../components/ColorSchemeToggle'
import SpotOverviewPanel from '../components/SpotOverviewPanel'
import SpotLogsPanel from '../components/SpotLogsPanel'
import SpotHistoryPanel from '../components/SpotHistoryPanel'
import SpotSettingsPanel from '../components/SpotSettingsPanel'
import { useScanStore } from '../stores/scanStore'

// 业务域模式（docs/18 §9.1 方案 C）：合约 = 现有全部功能；现货 = 屯币系统（独立模块）。
// 切换入口在 Header 右上角（App.tsx），本组件受控接收 props
export type BizMode = 'futures' | 'spot'

interface ScanResultProps {
  mode: BizMode
}

export default function ScanResult({ mode }: ScanResultProps) {
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
    activeProfile,
    setActiveProfile,
  } = useScanStore()

  // 激活扫描套运行态：优先取 status.profiles 匹配条目，缺失回落全局（旧后端=反映 s1）
  const activeProf = status?.profiles?.find((p) => p.key === activeProfile)
  const isScanning = activeProf ? activeProf.is_scanning : !!status?.is_scanning

  // 扫描套选择器选项：优先用 status.profiles（含后端标签），缺失用两套静态标签
  const profileOptions = status?.profiles?.length
    ? status.profiles.map((p) => ({ value: p.key, label: p.label }))
    : [
        { value: 's1', label: '1h·4h' },
        { value: 's2', label: '4h·1d' },
      ]

  const wasScanning = useRef(false)
  // 合约内部 tab 记忆（既有 key 不动）
  const [activeTab, switchTab] = useTabMemory(
    'tabs.scanResult',
    ['results', 'pending-pool', 'watchlist', 'search', 'strategy', 'review', 'trades', 'transfers', 'fund-analysis', 'tasks', 'logs', 'wiki'],
  )
  // 现货内部 tab 记忆（独立 key，与合约无冲突）
  const [spotTab, switchSpotTab] = useTabMemory('tabs.spot', ['overview', 'history', 'settings', 'logs'])

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

  // 切换激活扫描套：重取状态（把 currentScanId 切到该套最近扫描）→ 重取结果（含 AI 分析）。
  // 首渲染跳过（初始加载已取）。
  const profileReady = useRef(false)
  useEffect(() => {
    if (!profileReady.current) {
      profileReady.current = true
      return
    }
    ;(async () => {
      await fetchStatus()
      // 重置扫描态基线为新套，避免切换瞬间被下方完成判定误报「扫描完成」
      const st = useScanStore.getState().status
      const prof = st?.profiles?.find((p) => p.key === activeProfile)
      wasScanning.current = prof ? prof.is_scanning : !!st?.is_scanning
      await fetchResults(undefined, 1)
    })()
  }, [activeProfile])

  // 扫描状态变化处理（按激活扫描套）
  useEffect(() => {
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
  }, [isScanning])

  const handleTrigger = async () => {
    // 前端也检查是否正在扫描
    if (isScanning) {
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

  // 扫描套选择器（2026-10-14）：从 Tab 栏右上角移入扫描结果区，与「立即扫描」
  // 同排左右分布（扫描套贴最左、立即扫描+刷新贴最右）
  const profileSelector = (
    <Space size={8}>
      <span style={{ color: '#8c8c8c', fontSize: 13 }}>扫描套</span>
      <Segmented
        size="small"
        options={profileOptions}
        value={activeProfile}
        onChange={(v) => setActiveProfile(v as 's1' | 's2')}
      />
    </Space>
  )

  // ── 合约 Tabs（现有 10 个 Tab，原样保留——docs/18 零侵入约束）──
  const futuresItems = [
    {
      key: 'results',
      label: '扫描结果',
      children: <div style={{ height: '100%', overflow: 'hidden' }}><ResultTable headerLeading={profileSelector} headerExtra={scanButton} /></div>,
    },
    {
      key: 'pending-pool',
      label: '待确认池',
      children: <div style={{ height: '100%', overflow: 'hidden' }}><PendingPoolPanel /></div>,
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
      key: 'tactics',
      label: '战法',
      children: (
        <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}>
          <TacticsPanel />
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
              {
                key: 'shadow',
                label: '影子实验',
                children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><ShadowPanel /></div>,
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
    {
      key: 'wiki',
      label: 'CODE WIKI',
      children: <div style={{ height: '100%', overflow: 'hidden' }}><CodeWikiPanel /></div>,
    },
  ]

  // ── 现货 Tabs（docs/18：持仓总览 / 历史记录 / 策略配置）──
  const spotItems = [
    {
      key: 'overview',
      label: '持仓总览',
      children: <div style={{ height: '100%', overflowY: 'auto', overflowX: 'hidden' }}><SpotOverviewPanel /></div>,
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
    {
      key: 'logs',
      label: '日志',
      children: <div style={{ height: '100%', overflow: 'hidden' }}><SpotLogsPanel /></div>,
    },
  ]

  const showFutures = mode !== 'spot'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Card style={{ flexShrink: 0, marginBottom: 12 }}>
        <Row justify="space-between" align="middle">
          <Col>
            <Space size={16}>{showFutures && <ScanStatus />}</Space>
          </Col>
          <Col>
            <Space>
              <ColorSchemeToggle />
              {showFutures && (
                <>
                  <ScanConfigPanel />
                  <TradingConfigPanel />
                  <SystemConfigPanel />
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
