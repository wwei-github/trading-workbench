// 战法 API（用户自建战术剧本，仅借鉴标注不做硬闸）
import axios from 'axios'
import type { Tactic } from '../types'

const api = axios.create({ baseURL: '/api', timeout: 15000 })

export interface TacticPayload {
  name: string
  description: string
  direction: 'all' | 'long' | 'short'
  scope: 'testnet' | 'live' | 'both'
  enabled?: boolean
}

export const tacticsApi = {
  list: () => api.get<Tactic[]>('/tactics').then((r) => r.data),
  create: (payload: TacticPayload) =>
    api.post<Tactic>('/tactics', payload).then((r) => r.data),
  update: (id: string, payload: TacticPayload) =>
    api.put<Tactic>(`/tactics/${id}`, payload).then((r) => r.data),
  toggle: (id: string, enabled: boolean) =>
    api.patch<{ id: string; enabled: boolean }>(`/tactics/${id}/enabled`, { enabled }).then((r) => r.data),
  remove: (id: string) => api.delete<{ deleted: string }>(`/tactics/${id}`).then((r) => r.data),
}
