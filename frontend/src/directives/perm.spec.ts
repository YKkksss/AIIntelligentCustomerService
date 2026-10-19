import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { perm } from './perm'
import { useAuthStore } from '@/stores/auth'

function mountWithPermission(permission: string): HTMLElement {
  const parent = document.createElement('div')
  const el = document.createElement('button')
  parent.appendChild(el)
  document.body.appendChild(parent)
  ;(perm as { mounted: (el: HTMLElement, binding: unknown) => void }).mounted(el, {
    value: permission,
  })
  return el
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

describe('v-perm', () => {
  it('有权限时保留元素', () => {
    const auth = useAuthStore()
    auth.setApps([{ app_id: 'a', app_name: 'A', role_id: 'viewer', permissions: ['app.read'] }])
    expect(mountWithPermission('app.read').isConnected).toBe(true)
  })

  it('没有权限时把元素从 DOM 里移除', () => {
    const auth = useAuthStore()
    auth.setApps([{ app_id: 'a', app_name: 'A', role_id: 'viewer', permissions: ['app.read'] }])
    expect(mountWithPermission('kb.write').isConnected).toBe(false)
  })

  it('超级管理员总能通过', () => {
    const auth = useAuthStore()
    auth.setApps([])
    auth.$patch({ user: { id: 'u', email: 'e', display_name: 'n', is_super_admin: true } })
    expect(mountWithPermission('system.write').isConnected).toBe(true)
  })
})
