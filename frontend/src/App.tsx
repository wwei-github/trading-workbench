import { Layout, Segmented, Typography } from 'antd'
import Logo from './components/Logo'
import MarketOverviewBar from './components/MarketOverviewBar'
import ScanResult, { type BizMode } from './pages/ScanResult'
import { useTabMemory } from './utils/tabMemory'

const { Header, Content } = Layout
const { Title } = Typography

export default function App() {
  // 合约/现货模式（Header 右上角切换）：useTabMemory 持久化，key 与原页面内切换
  // 完全一致（tabs.mode）——刷新后模式记忆不丢
  const [modeRaw, setModeRaw] = useTabMemory('tabs.mode', ['futures', 'spot'])
  const mode = modeRaw as BizMode

  return (
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <Header
        style={{
          background: '#001529',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          flexShrink: 0,
          paddingInline: 16,
        }}
      >
        <Logo />
        <Title level={4} style={{ color: '#fff', margin: 0, whiteSpace: 'nowrap' }}>
          虚拟币筛选工作台
        </Title>
        {/* 大盘环境/市场情绪（中部弹性居中，空间不足时优先收缩的是它两侧 gap） */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', justifyContent: 'center', overflow: 'hidden' }}>
          <MarketOverviewBar />
        </div>
        <Segmented<BizMode>
          value={mode}
          onChange={(v) => setModeRaw(v)}
          options={[
            { label: '合约', value: 'futures' },
            { label: '现货', value: 'spot' },
          ]}
        />
      </Header>
      <Content style={{ padding: 16, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        <ScanResult mode={mode} />
      </Content>
    </Layout>
  )
}
