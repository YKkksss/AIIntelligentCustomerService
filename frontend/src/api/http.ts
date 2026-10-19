import axios, { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details?: Record<string, unknown>
    request_id?: string | null
  }
}

// 统一 axios 实例：/api/v1 前缀由 Vite 代理到本机后端（见 vite.config.ts）
export const http = axios.create({ baseURL: '/api/v1', timeout: 30_000 })

/** 同一时刻只允许一个刷新请求，其余 401 排队等它的结果（管理后台设计 §3）。 */
let refreshing: Promise<string | null> | null = null

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore()
  if (auth.accessToken) config.headers.Authorization = `Bearer ${auth.accessToken}`
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status
    const code = error.response?.data?.error?.code
    const config = error.config as (AxiosRequestConfig & { _retried?: boolean }) | undefined

    if (status === 401 && code === 'TOKEN_EXPIRED' && config && !config._retried) {
      config._retried = true
      const auth = useAuthStore()
      refreshing ??= auth.refresh().finally(() => {
        refreshing = null
      })
      const token = await refreshing
      if (token) {
        config.headers = { ...(config.headers ?? {}), Authorization: `Bearer ${token}` }
        return http.request(config)
      }
    }

    if (!config?.suppressErrorToast) {
      const body = error.response?.data?.error
      const message = body?.message ?? error.message
      ElMessage.error(body?.request_id ? `${message}（request_id: ${body.request_id}）` : message)
    }
    return Promise.reject(error)
  },
)

declare module 'axios' {
  export interface AxiosRequestConfig {
    /** 由调用方自己处理错误提示时置 true（例如刷新令牌失败不想弹两次）。 */
    suppressErrorToast?: boolean
    /** 401 后刷新重试过一次的标记，防止无限循环。 */
    _retried?: boolean
  }
}

export interface ReadyResponse {
  status: 'ok' | 'error'
  checks: Record<string, 'ok' | 'error'>
}

export async function fetchReady(): Promise<ReadyResponse> {
  const resp = await axios.get<ReadyResponse>('/readyz', { validateStatus: () => true })
  return resp.data
}
