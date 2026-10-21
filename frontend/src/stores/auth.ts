import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { appApi, authApi, type AdminUser, type AppMembership } from '@/api/admin'
import { hasPermission } from '@/utils/perm'

// access / refresh token 存内存 + localStorage：有效期由登录时选的「保持登录」天数决定
// （1 天 / 7 天，后端按 JWT exp 与 Redis TTL 双重把关），见管理后台设计 §1
const ACCESS = 'aics.access_token'
const REFRESH = 'aics.refresh_token'
const APP = 'aics.app_id'
const REMEMBER = 'aics.remember_days'

/** 记住登录：先读 localStorage，同时兼容旧版本留在 sessionStorage 里的令牌。 */
function read(key: string): string | null {
  return localStorage.getItem(key) ?? sessionStorage.getItem(key)
}

function write(key: string, value: string | null) {
  if (value) localStorage.setItem(key, value)
  else localStorage.removeItem(key)
  sessionStorage.removeItem(key)
}

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(read(ACCESS))
  const refreshToken = ref<string | null>(read(REFRESH))
  const user = ref<AdminUser | null>(null)
  const apps = ref<AppMembership[]>([])
  const currentAppId = ref<string | null>(read(APP))
  /** 登录时选的「保持登录」天数（1 / 7），只用来给下次登录预选。 */
  const rememberDays = ref<number>(Number(read(REMEMBER)) || 1)
  const loaded = ref(false)

  const isLoggedIn = computed(() => !!accessToken.value)
  const isSuperAdmin = computed(() => !!user.value?.is_super_admin)
  const currentApp = computed(
    () => apps.value.find((app) => app.app_id === currentAppId.value) ?? null,
  )

  function setTokens(access: string | null, refresh: string | null) {
    accessToken.value = access
    refreshToken.value = refresh
    write(ACCESS, access)
    write(REFRESH, refresh)
  }

  function setApps(next: AppMembership[]) {
    apps.value = next
    const known = next.some((app) => app.app_id === currentAppId.value)
    if (!known) switchApp(next[0]?.app_id ?? null)
  }

  /**
   * 超级管理员不是任何应用的成员，`/me` 的 apps 为空；这里用可见应用列表补齐，
   * 否则侧栏的应用菜单与标题里的应用名都取不到。
   */
  async function fillSuperAdminApps() {
    if (!isSuperAdmin.value || apps.value.length > 0) return
    try {
      const body = await appApi.list(100, 0)
      const next: AppMembership[] = body.items.map((app) => ({
        app_id: app.id,
        app_name: app.name,
        role_id: 'app_admin',
        permissions: [],
      }))
      setApps(next)
    } catch {
      // 列表拉不到时保持为空，页面各自兜底
    }
  }

  function switchApp(appId: string | null) {
    currentAppId.value = appId
    write(APP, appId)
  }

  /** 应用作用域权限；不传 appId 时用当前应用。 */
  function can(permission: string, appId = currentAppId.value): boolean {
    return hasPermission(apps.value, appId, permission, isSuperAdmin.value)
  }

  async function login(email: string, password: string, days = 1) {
    const session = await authApi.login(email, password, days)
    rememberDays.value = days
    write(REMEMBER, String(days))
    setTokens(session.access_token, session.refresh_token)
    user.value = session.user
    setApps(session.apps)
    loaded.value = true
    await fillSuperAdminApps()
  }

  /** 刷新令牌；失败即清空登录态（返回 null 让调用方决定是否跳登录）。 */
  async function refresh(): Promise<string | null> {
    if (!refreshToken.value) return null
    try {
      const session = await authApi.refresh(refreshToken.value)
      setTokens(session.access_token, session.refresh_token)
      // 后端正常会把 user / apps 一起回；万一只回令牌（老形状），就保留内存里的身份，
      // 绝不能因为 setApps(undefined) 抛错把登录态清掉 —— 那会让 access 一过期就掉登录
      if (session.user) {
        user.value = session.user
        loaded.value = true
      }
      if (session.apps) setApps(session.apps)
      await fillSuperAdminApps()
      return session.access_token
    } catch {
      setTokens(null, null)
      user.value = null
      apps.value = []
      loaded.value = false
      return null
    }
  }

  /** 页面刷新后用它把身份补回来（路由守卫在首次进入时调用）。 */
  async function ensureLoaded() {
    if (loaded.value || !accessToken.value) return
    try {
      const me = await authApi.me()
      user.value = me.user
      setApps(me.apps)
      loaded.value = true
      await fillSuperAdminApps()
    } catch {
      if (!(await refresh())) return
      const me = await authApi.me()
      user.value = me.user
      setApps(me.apps)
      loaded.value = true
      await fillSuperAdminApps()
    }
  }

  /** 改密成功后换发的新令牌（旧 access 立即失效，必须整体替换）。 */
  async function changePassword(current: string, next: string) {
    const session = await authApi.changePassword(current, next)
    setTokens(session.access_token, session.refresh_token)
    user.value = session.user
    loaded.value = true
    await fillSuperAdminApps()
  }
  async function logout() {
    const token = refreshToken.value
    setTokens(null, null)
    user.value = null
    apps.value = []
    loaded.value = false
    if (token) await authApi.logout(token).catch(() => undefined)
  }

  return {
    accessToken,
    refreshToken,
    user,
    apps,
    currentAppId,
    currentApp,
    rememberDays,
    loaded,
    isLoggedIn,
    isSuperAdmin,
    setTokens,
    setApps,
    switchApp,
    can,
    login,
    changePassword,
    refresh,
    ensureLoaded,
    logout,
  }
})
