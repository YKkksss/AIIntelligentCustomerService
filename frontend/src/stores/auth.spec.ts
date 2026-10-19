import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/admin', () => ({
  authApi: {
    login: vi.fn(),
    refresh: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
    changePassword: vi.fn(),
  },
}))

import { authApi } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const apps = [
  { app_id: 'a', app_name: 'A', role_id: 'viewer' as const, permissions: ['app.read'] },
  {
    app_id: 'b',
    app_name: 'B',
    role_id: 'app_admin' as const,
    permissions: ['app.read', 'kb.write'],
  },
]
const user = { id: 'u1', email: 'admin@example.com', display_name: '管理员', is_super_admin: false }

const session = {
  access_token: 'access-1',
  refresh_token: 'refresh-1',
  token_type: 'bearer' as const,
  expires_in: 1800,
  user,
  apps,
}

beforeEach(() => {
  setActivePinia(createPinia())
  sessionStorage.clear()
  vi.mocked(authApi.login).mockReset()
  vi.mocked(authApi.refresh).mockReset()
  vi.mocked(authApi.me).mockReset()
})

describe('登录', () => {
  it('写入令牌、用户与可见应用，并默认选中第一个应用', async () => {
    vi.mocked(authApi.login).mockResolvedValue(session)
    const auth = useAuthStore()

    await auth.login('admin@example.com', 'secret')

    expect(auth.isLoggedIn).toBe(true)
    expect(auth.currentAppId).toBe('a')
    expect(sessionStorage.getItem('aics.access_token')).toBe('access-1')
    expect(sessionStorage.getItem('aics.refresh_token')).toBe('refresh-1')
    expect(auth.can('app.read')).toBe(true)
    expect(auth.can('kb.write')).toBe(false)
  })

  it('切换应用后权限跟着变', async () => {
    vi.mocked(authApi.login).mockResolvedValue(session)
    const auth = useAuthStore()
    await auth.login('admin@example.com', 'secret')

    auth.switchApp('b')

    expect(auth.can('kb.write')).toBe(true)
    expect(sessionStorage.getItem('aics.app_id')).toBe('b')
  })

  it('超级管理员在任何应用里都放行', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      ...session,
      user: { ...user, is_super_admin: true },
      apps: [],
    })
    const auth = useAuthStore()
    await auth.login('root@example.com', 'secret')

    expect(auth.can('system.write')).toBe(true)
  })
})

describe('刷新令牌', () => {
  it('成功时换发新令牌', async () => {
    vi.mocked(authApi.refresh).mockResolvedValue({ ...session, access_token: 'access-2' })
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')

    const token = await auth.refresh()

    expect(token).toBe('access-2')
    expect(auth.accessToken).toBe('access-2')
  })

  it('失败时清空登录态并返回 null', async () => {
    vi.mocked(authApi.refresh).mockRejectedValue(new Error('401'))
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')

    const token = await auth.refresh()

    expect(token).toBeNull()
    expect(auth.isLoggedIn).toBe(false)
    expect(sessionStorage.getItem('aics.access_token')).toBeNull()
  })

  it('没有 refresh token 时不发请求', async () => {
    const auth = useAuthStore()

    expect(await auth.refresh()).toBeNull()
    expect(authApi.refresh).not.toHaveBeenCalled()
  })
})

describe('ensureLoaded', () => {
  it('有 access token 时用 /auth/me 补身份，只补一次', async () => {
    vi.mocked(authApi.me).mockResolvedValue({ user, apps })
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')

    await auth.ensureLoaded()
    await auth.ensureLoaded()

    expect(authApi.me).toHaveBeenCalledTimes(1)
    expect(auth.currentAppId).toBe('a')
  })

  it('access 过期时先刷新再读身份', async () => {
    vi.mocked(authApi.me)
      .mockRejectedValueOnce(new Error('401'))
      .mockResolvedValueOnce({ user, apps })
    vi.mocked(authApi.refresh).mockResolvedValue({ ...session, access_token: 'access-2' })
    const auth = useAuthStore()
    auth.setTokens('access-1', 'refresh-1')

    await auth.ensureLoaded()

    expect(auth.accessToken).toBe('access-2')
    expect(auth.loaded).toBe(true)
  })
})
