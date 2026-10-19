import { describe, expect, it } from 'vitest'
import type { AppMembership } from '@/api/admin'
import { hasPermission, permissionsOf } from './perm'

const apps: AppMembership[] = [
  { app_id: 'a', app_name: 'A', role_id: 'viewer', permissions: ['app.read', 'log.read'] },
  { app_id: 'b', app_name: 'B', role_id: 'app_admin', permissions: ['app.read', 'kb.write'] },
]

describe('permissionsOf', () => {
  it('没有当前应用时返回空数组', () => {
    expect(permissionsOf(apps, null)).toEqual([])
  })

  it('返回该应用的权限', () => {
    expect(permissionsOf(apps, 'b')).toEqual(['app.read', 'kb.write'])
  })

  it('应用不在列表里时返回空数组', () => {
    expect(permissionsOf(apps, 'missing')).toEqual([])
  })
})

describe('hasPermission', () => {
  it('超级管理员一律放行（包括不存在的权限名）', () => {
    expect(hasPermission([], null, 'system.write', true)).toBe(true)
  })

  it('按当前应用判断', () => {
    expect(hasPermission(apps, 'b', 'kb.write')).toBe(true)
  })

  it('不把别的应用的权限借过来', () => {
    expect(hasPermission(apps, 'a', 'kb.write')).toBe(false)
  })

  it('没有应用上下文时只放行超管', () => {
    expect(hasPermission(apps, null, 'app.read')).toBe(false)
  })
})
