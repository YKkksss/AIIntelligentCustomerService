import { beforeEach, describe, expect, it, vi } from 'vitest'

const ensureLoaded = vi.fn()
const can = vi.fn()
const state = { isLoggedIn: false }

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    get isLoggedIn() {
      return state.isLoggedIn
    },
    ensureLoaded,
    can,
  }),
}))

import { authGuard } from '@/router'

function target(meta: { requiresAuth?: boolean; permission?: string }, fullPath = '/x') {
  return { meta, fullPath }
}

beforeEach(() => {
  state.isLoggedIn = false
  ensureLoaded.mockReset()
  can.mockReset()
})

describe('authGuard', () => {
  it('不需要登录的页面直接放行', async () => {
    await expect(authGuard(target({}))).resolves.toBe(true)
    expect(ensureLoaded).not.toHaveBeenCalled()
  })

  it('未登录去登录页，并带上回跳地址', async () => {
    await expect(authGuard(target({ requiresAuth: true }, '/users'))).resolves.toEqual({
      name: 'login',
      query: { redirect: '/users' },
    })
  })

  it('有 token 但身份没加载时先补 /auth/me', async () => {
    state.isLoggedIn = true
    ensureLoaded.mockResolvedValue(undefined)

    await authGuard(target({ requiresAuth: true }))

    expect(ensureLoaded).toHaveBeenCalledTimes(1)
  })

  it('补身份后发现已经登出，回登录页', async () => {
    state.isLoggedIn = true
    ensureLoaded.mockImplementation(async () => {
      state.isLoggedIn = false
    })

    await expect(authGuard(target({ requiresAuth: true }, '/audit'))).resolves.toEqual({
      name: 'login',
      query: { redirect: '/audit' },
    })
  })

  it('权限不足回概览页', async () => {
    state.isLoggedIn = true
    ensureLoaded.mockResolvedValue(undefined)
    can.mockReturnValue(false)

    await expect(
      authGuard(target({ requiresAuth: true, permission: 'user.admin' })),
    ).resolves.toEqual({ name: 'home' })
    expect(can).toHaveBeenCalledWith('user.admin')
  })

  it('权限足够放行', async () => {
    state.isLoggedIn = true
    ensureLoaded.mockResolvedValue(undefined)
    can.mockReturnValue(true)

    await expect(authGuard(target({ requiresAuth: true, permission: 'audit.read' }))).resolves.toBe(
      true,
    )
  })
})
