import { http } from '@/api/http'

/** 管理接口（`/api/v1/admin`）的请求与响应类型。
 *
 * 后端这些路由目前用 `JSONResponse` 直接返回字典（没有 `response_model`），所以
 * `npm run gen:api` 生成的 `schema.d.ts` 里这些响应体是空的；类型先手写，等路由补上
 * `response_model` 后再改为从生成文件引用（ADR-0018 §3）。
 */

export interface AdminUser {
  id: string
  email: string
  display_name: string
  is_super_admin: boolean
  must_change_password?: boolean
  status?: 'active' | 'disabled'
  last_login_at?: string | null
  created_at?: string
}

export interface AppMembership {
  app_id: string
  app_name: string
  role_id: 'app_admin' | 'viewer'
  permissions: string[]
}

export interface SessionBody {
  access_token: string
  refresh_token: string
  token_type: 'bearer'
  expires_in: number
  user: AdminUser
  apps: AppMembership[]
}

export interface MeBody {
  user: AdminUser
  apps: AppMembership[]
}

export interface MemberRow {
  user_id: string
  email: string
  display_name: string
  role_id: 'app_admin' | 'viewer'
  status: string
}

export interface AuditRow {
  id: number
  action: string
  actor_type: 'user' | 'api_key' | 'system'
  actor_id: string | null
  app_id: string | null
  target_type: string | null
  target_id: string | null
  diff: Record<string, unknown> | null
  ip: string | null
  user_agent: string | null
  request_id: string | null
  created_at: string
}

export interface AuditPage {
  items: AuditRow[]
  next_cursor: string | null
}

export const authApi = {
  async login(email: string, password: string): Promise<SessionBody> {
    const { data } = await http.post<SessionBody>('/admin/auth/login', { email, password })
    return data
  },
  async refresh(refreshToken: string): Promise<SessionBody> {
    const { data } = await http.post<SessionBody>(
      '/admin/auth/refresh',
      { refresh_token: refreshToken },
      { suppressErrorToast: true },
    )
    return data
  },
  async logout(refreshToken: string | null): Promise<void> {
    await http.post(
      '/admin/auth/logout',
      { refresh_token: refreshToken },
      { suppressErrorToast: true },
    )
  },
  async me(): Promise<MeBody> {
    const { data } = await http.get<MeBody>('/admin/auth/me')
    return data
  },
  async changePassword(currentPassword: string, newPassword: string): Promise<SessionBody> {
    const { data } = await http.post<SessionBody>('/admin/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    })
    return data
  },
}

export const userApi = {
  async list(q = '', limit = 100): Promise<{ items: AdminUser[] }> {
    const { data } = await http.get<{ items: AdminUser[] }>('/admin/users', {
      params: { q, limit },
    })
    return data
  },
  async create(body: {
    email: string
    display_name: string
    password: string
    is_super_admin?: boolean
  }) {
    const { data } = await http.post<AdminUser>('/admin/users', body)
    return data
  },
  async patch(userId: string, body: Record<string, unknown>) {
    const { data } = await http.patch<AdminUser>(`/admin/users/${userId}`, body)
    return data
  },
  async remove(userId: string): Promise<void> {
    await http.delete(`/admin/users/${userId}`)
  },
  async resetPassword(userId: string): Promise<{ password: string }> {
    const { data } = await http.post<{ password: string }>(`/admin/users/${userId}/reset-password`)
    return data
  },
}

export const memberApi = {
  async list(appId: string): Promise<{ items: MemberRow[] }> {
    const { data } = await http.get<{ items: MemberRow[] }>(`/admin/apps/${appId}/members`)
    return data
  },
  async set(appId: string, userId: string, roleId: 'app_admin' | 'viewer'): Promise<void> {
    await http.put(`/admin/apps/${appId}/members/${userId}`, { role_id: roleId })
  },
  async remove(appId: string, userId: string): Promise<void> {
    await http.delete(`/admin/apps/${appId}/members/${userId}`)
  },
}

export const auditApi = {
  async page(params: {
    appId?: string
    action?: string
    actorId?: string
    cursor?: string | null
    pageSize?: number
  }): Promise<AuditPage> {
    const { data } = await http.get<AuditPage>('/admin/audit', {
      params: {
        app_id: params.appId || undefined,
        action: params.action || undefined,
        actor_id: params.actorId || undefined,
        cursor: params.cursor || undefined,
        page_size: params.pageSize ?? 50,
      },
    })
    return data
  },
  async actions(): Promise<string[]> {
    const { data } = await http.get<{ items: string[] }>('/admin/audit/actions')
    return data.items
  },
}
