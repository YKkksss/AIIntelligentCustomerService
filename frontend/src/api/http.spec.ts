import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { AxiosError } from 'axios'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'

vi.mock('@/api/admin', () => ({
  authApi: {
    login: vi.fn(),
    refresh: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
    changePassword: vi.fn(),
  },
}))

vi.mock('element-plus', () => ({ ElMessage: { error: vi.fn(), success: vi.fn() } }))

import { authApi } from '@/api/admin'
import { ElMessage } from 'element-plus'
import { http } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

function reply(config: InternalAxiosRequestConfig, status: number, data: unknown): AxiosResponse {
  return { data, status, statusText: String(status), headers: {}, config }
}

/** 假 adapter 必须自己拒绝非 2xx——`validateStatus` 是装在真实 adapter 里的。 */
function fail(config: InternalAxiosRequestConfig, status: number, data: unknown) {
  return Promise.reject(
    new AxiosError('failed', String(status), config, null, reply(config, status, data)),
  )
}

const EXPIRED = { error: { code: 'TOKEN_EXPIRED', message: '登录已过期，请刷新' } }

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(authApi.refresh).mockReset()
  vi.mocked(ElMessage.error).mockClear()
})

describe('http 拦截器', () => {
  it('带上 access token', async () => {
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')
    let seen = ''
    http.defaults.adapter = (async (config) => {
      seen = String(config.headers.Authorization)
      return reply(config, 200, { ok: true })
    }) as AxiosAdapter

    await http.get('/ping')

    expect(seen).toBe('Bearer access-1')
  })

  it('TOKEN_EXPIRED 时刷新一次并重放原请求', async () => {
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')
    vi.mocked(authApi.refresh).mockResolvedValue({
      access_token: 'access-2',
      refresh_token: 'refresh-2',
      token_type: 'bearer',
      expires_in: 1800,
      user: { id: 'u', email: 'e', display_name: 'n', is_super_admin: false },
      apps: [],
    })
    const calls: string[] = []
    http.defaults.adapter = (async (config) => {
      const token = String(config.headers.Authorization)
      calls.push(token)
      return token === 'Bearer access-2'
        ? reply(config, 200, { ok: true })
        : fail(config, 401, EXPIRED)
    }) as AxiosAdapter

    const resp = await http.get('/who')

    expect(resp.status).toBe(200)
    expect(calls).toEqual(['Bearer access-1', 'Bearer access-2'])
    expect(authApi.refresh).toHaveBeenCalledTimes(1)
  })

  it('刷新失败后把错误交给调用方，并提示一次', async () => {
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')
    vi.mocked(authApi.refresh).mockResolvedValue(null as never)
    http.defaults.adapter = (async (config) => fail(config, 401, EXPIRED)) as AxiosAdapter

    await expect(http.get('/who')).rejects.toBeTruthy()

    expect(auth.isLoggedIn).toBe(false)
    expect(ElMessage.error).toHaveBeenCalled()
  })

  it('suppressErrorToast 时不弹提示', async () => {
    const auth = useAuthStore()
    auth.setTokens('access-1', null)
    const body = { error: { code: 'INTERNAL_ERROR', message: '服务内部错误' } }
    http.defaults.adapter = (async (config) => fail(config, 500, body)) as AxiosAdapter

    await expect(http.get('/boom', { suppressErrorToast: true })).rejects.toBeTruthy()

    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('错误提示带 request_id', async () => {
    const auth = useAuthStore()
    auth.setTokens('access-1', null)
    const body = {
      error: { code: 'INTERNAL_ERROR', message: '服务内部错误', request_id: 'req-1' },
    }
    http.defaults.adapter = (async (config) => fail(config, 500, body)) as AxiosAdapter

    await expect(http.get('/boom')).rejects.toBeTruthy()

    expect(vi.mocked(ElMessage.error).mock.calls[0]?.[0]).toContain('req-1')
  })
})
