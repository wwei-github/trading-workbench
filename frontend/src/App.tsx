import { Layout, Typography } from 'antd'
import ScanResult from './pages/ScanResult'

const { Header, Content } = Layout
const { Title } = Typography

export default function App() {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#001529', display: 'flex', alignItems: 'center' }}>
        <Title level={4} style={{ color: '#fff', margin: 0 }}>
          币安币种筛选工作台
        </Title>
      </Header>
      <Content style={{ padding: 24 }}>
        <ScanResult />
      </Content>
    </Layout>
  )
}
