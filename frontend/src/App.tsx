import { Layout, Typography } from 'antd'
import ScanResult from './pages/ScanResult'

const { Header, Content } = Layout
const { Title } = Typography

export default function App() {
  return (
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <Header style={{ background: '#001529', display: 'flex', alignItems: 'center', flexShrink: 0 }}>
        <Title level={4} style={{ color: '#fff', margin: 0 }}>
          虚拟币筛选工作台
        </Title>
      </Header>
      <Content style={{ padding: 16, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        <ScanResult />
      </Content>
    </Layout>
  )
}
