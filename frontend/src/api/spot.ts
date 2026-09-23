// 现货屯币 API（docs/18，独立模块，不混入合约 scanApi）
import axios from 'axios'
import type { SpotConfig, SpotDecision, SpotOrder, SpotOverview, SpotSettings, SpotTimelineItem } from '../types/spot'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

export const spotApi = {
  // 持仓总览（三币持仓 + 现金池 + 实时评分，多账号聚合）
  // 总览（accountKey 可选：正式网单账号查看；缺省=启用账号聚合）
  overview: (accountKey?: string) =>
    api
      .get<SpotOverview>('/spot/overview', { params: accountKey ? { account_key: accountKey } : {} })
      .then((r) => r.data),

  // 单币评分明细（可解释）
  score: (symbol: string) =>
    api.get<SpotDecision>(`/spot/score/${symbol}`).then((r) => r.data),

  // 订单列表（P0 通常为空：手动测试单）
  orders: (limit = 100) =>
    api.get<SpotOrder[]>('/spot/orders', { params: { limit } }).then((r) => r.data),

  // 运营时间轴（买卖/AI研判/每日评分聚合，docs/18 §9.2.3）
  timeline: (limit = 100) =>
    api.get<SpotTimelineItem[]>('/spot/timeline', { params: { limit } }).then((r) => r.data),

  // 配置
  getSettings: () => api.get<SpotSettings>('/spot/settings').then((r) => r.data),
  updateSettings: (patch: Partial<SpotSettings>) =>
    api
      .put<{ ok: boolean; settings: SpotSettings; rejected: string[] }>('/spot/settings', patch)
      .then((r) => r.data),

  // 环境与账号（与合约完全独立）
  getConfig: () => api.get<SpotConfig>('/spot/config').then((r) => r.data),
  updateConfig: (patch: { spot_trading_testnet?: boolean; spot_enabled_keys?: string[] }) =>
    api
      .put<{ ok: boolean } & SpotConfig & { rejected: string[] }>('/spot/config', patch)
      .then((r) => r.data),
}
