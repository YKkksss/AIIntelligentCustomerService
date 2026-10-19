import type { AppMembership } from '@/api/admin'

/** 权限判断的唯一实现：路由守卫、`v-perm` 指令与菜单过滤都走这里。 */
export function permissionsOf(apps: AppMembership[], appId: string | null): string[] {
  if (!appId) return []
  return apps.find((app) => app.app_id === appId)?.permissions ?? []
}

export function hasPermission(
  apps: AppMembership[],
  appId: string | null,
  permission: string,
  isSuperAdmin = false,
): boolean {
  if (isSuperAdmin) return true
  return permissionsOf(apps, appId).includes(permission)
}
