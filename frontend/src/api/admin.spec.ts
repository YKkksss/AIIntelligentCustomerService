import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({
  http: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/api/http'
import { auditApi, authApi, memberApi, userApi } from '@/api/admin'

const get = vi.mocked(http.get)
const post = vi.mocked(http.post)
const patch = vi.mocked(http.patch)
const put = vi.mocked(http.put)
const del = vi.mocked(http.delete)

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue({ data: {} } as never)
  post.mockResolvedValue({ data: {} } as never)
  patch.mockResolvedValue({ data: {} } as never)
  put.mockResolvedValue({ data: {} } as never)
  del.mockResolvedValue({ data: {} } as never)
})

describe('admin api 与后端路径一一对应', () => {
  it('认证', async () => {
    await authApi.login('a@b.c', 'secret123')
    expect(post.mock.calls[0]).toEqual([
      '/admin/auth/login',
      { email: 'a@b.c', password: 'secret123' },
    ])

    await authApi.refresh('r1')
    expect(post.mock.calls[1]).toEqual([
      '/admin/auth/refresh',
      { refresh_token: 'r1' },
      { suppressErrorToast: true },
    ])

    await authApi.logout('r1')
    expect(post.mock.calls[2]?.[0]).toBe('/admin/auth/logout')

    await authApi.me()
    expect(get).toHaveBeenCalledWith('/admin/auth/me')

    await authApi.changePassword('a'.repeat(8), 'b'.repeat(8))
    expect(post.mock.calls[3]).toEqual([
      '/admin/auth/password',
      { current_password: 'a'.repeat(8), new_password: 'b'.repeat(8) },
    ])
  })

  it('用户', async () => {
    await userApi.list('a', 20)
    expect(get).toHaveBeenCalledWith('/admin/users', { params: { q: 'a', limit: 20 } })

    await userApi.create({ email: 'a@b.c', display_name: 'A', password: 'x'.repeat(8) })
    expect(post).toHaveBeenCalledWith('/admin/users', expect.objectContaining({ email: 'a@b.c' }))

    await userApi.patch('u1', { status: 'disabled' })
    expect(patch).toHaveBeenCalledWith('/admin/users/u1', { status: 'disabled' })

    await userApi.remove('u1')
    expect(del).toHaveBeenCalledWith('/admin/users/u1')

    await userApi.resetPassword('u1')
    expect(post).toHaveBeenCalledWith('/admin/users/u1/reset-password')
  })

  it('成员', async () => {
    await memberApi.list('app-1')
    expect(get).toHaveBeenCalledWith('/admin/apps/app-1/members')

    await memberApi.set('app-1', 'u1', 'viewer')
    expect(put).toHaveBeenCalledWith('/admin/apps/app-1/members/u1', { role_id: 'viewer' })

    await memberApi.remove('app-1', 'u1')
    expect(del).toHaveBeenCalledWith('/admin/apps/app-1/members/u1')
  })

  it('审计', async () => {
    await auditApi.page({ appId: 'app-1', pageSize: 50 })
    expect(get).toHaveBeenCalledWith('/admin/audit', {
      params: {
        app_id: 'app-1',
        action: undefined,
        actor_id: undefined,
        cursor: undefined,
        page_size: 50,
      },
    })

    get.mockResolvedValueOnce({ data: { items: ['user.create'] } } as never)
    await expect(auditApi.actions()).resolves.toEqual(['user.create'])
    expect(get).toHaveBeenLastCalledWith('/admin/audit/actions')
  })
})
