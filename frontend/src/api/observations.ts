import axios from 'axios'
import type { ObservationList, ObservationTask } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 30000 })

export interface ObservationCreateBody {
  name: string
  days?: number
  due_at?: string
  config_key?: string
  decision_rule?: string
  metrics?: Record<string, unknown>
  note?: string
}

/** 观察期任务（docs/23 §3）：登记 / 列表 / 决策 */
export const observationsApi = {
  async list(status?: string): Promise<ObservationList> {
    const r = await api.get('/observations', { params: status ? { status } : undefined })
    return r.data
  },
  async create(body: ObservationCreateBody): Promise<ObservationTask> {
    const r = await api.post('/observations', body)
    return r.data
  },
  async decide(id: string, decision: string, note?: string): Promise<ObservationTask> {
    const r = await api.post(`/observations/${id}/decide`, { decision, note })
    return r.data
  },
  async update(
    id: string,
    patch: { metrics?: Record<string, unknown>; note?: string; due_at?: string; status?: string },
  ): Promise<ObservationTask> {
    const r = await api.patch(`/observations/${id}`, patch)
    return r.data
  },
}
