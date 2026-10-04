// 影子模式 API（docs/14，2026-10-04）：只读评估报告，无任何写路径/开关
import axios from 'axios'
import type { ShadowFeatureReport } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 15000 })

export const shadowApi = {
  report: (windowDays = 30) =>
    api
      .get<ShadowFeatureReport[]>('/shadow/report', { params: { window_days: windowDays } })
      .then((r) => r.data),
}
