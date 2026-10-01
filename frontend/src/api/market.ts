// 大盘环境/市场情绪 API（Header 大盘条，后端聚合现有数据源带 TTL 缓存）
import axios from 'axios'
import type { MarketOverview } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 15000 })

export const marketApi = {
  overview: () => api.get<MarketOverview>('/market/overview').then((r) => r.data),
}
