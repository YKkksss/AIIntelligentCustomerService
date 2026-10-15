import axios from 'axios'
import { useAuthStore } from '@/stores/auth'

// 统一 axios 实例；M4 增加 TOKEN_EXPIRED 自动刷新一次的拦截器
export const http = axios.create({ baseURL: '/api/v1', timeout: 30_000 })

http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) config.headers.Authorization = `Bearer ${auth.token}`
  return config
})

export interface ReadyResponse {
  status: 'ok' | 'error'
  checks: Record<string, 'ok' | 'error'>
}

export async function fetchReady(): Promise<ReadyResponse> {
  const resp = await axios.get<ReadyResponse>('/readyz', { validateStatus: () => true })
  return resp.data
}
