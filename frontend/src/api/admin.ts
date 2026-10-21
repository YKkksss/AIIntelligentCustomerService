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

export interface AppCard {
  id: string
  name: string
  description: string | null
  status: string
  answer_style: string
  language: string
  config_version: number
  created_at: string
  updated_at: string
  stats: { documents: number }
}

export interface AppDetail extends AppCard {
  greeting: string | null
  system_prompt: string | null
  fallback_text: string | null
  rag_config: Record<string, unknown>
  handoff_config: Record<string, unknown>
  session_config: Record<string, unknown>
  effective_session_config: Record<string, unknown>
  webhook_config: Record<string, unknown>
  chunk_config: Record<string, unknown>
  custom_dict: string[]
  chunk_config_version: number
}

export interface ModelEntry {
  name: string
  kind: string
  provider?: string
  [key: string]: unknown
}

export interface ChunkPreset {
  name: string
  label: string
  config: Record<string, unknown>
}

export interface SystemSettings {
  default_session_config: Record<string, unknown>
}

export const authApi = {
  /** `rememberDays`：保持登录天数（1 或 7）；不传则用服务端默认值。 */
  async login(email: string, password: string, rememberDays?: number): Promise<SessionBody> {
    const { data } = await http.post<SessionBody>('/admin/auth/login', {
      email,
      password,
      ...(rememberDays ? { remember_days: rememberDays } : {}),
    })
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

export const appApi = {
  async list(limit = 50, offset = 0): Promise<{ items: AppCard[] }> {
    const { data } = await http.get<{ items: AppCard[] }>('/admin/apps', {
      params: { limit, offset },
    })
    return data
  },
  async detail(appId: string): Promise<AppDetail> {
    const { data } = await http.get<AppDetail>(`/admin/apps/${appId}`)
    return data
  },
  /** 只提交改动字段；`expected_config_version` 触发后端乐观校验（409 CONFIG_VERSION_CONFLICT）。 */
  async patch(appId: string, patch: Record<string, unknown>, expectedConfigVersion?: number) {
    const { data } = await http.patch<AppDetail>(`/admin/apps/${appId}`, {
      ...patch,
      expected_config_version: expectedConfigVersion,
    })
    return data
  },
  async create(body: { name: string; description?: string }) {
    const { data } = await http.post<AppDetail>('/admin/apps', body)
    return data
  },
  async copy(appId: string) {
    const { data } = await http.post<AppDetail>(`/admin/apps/${appId}/copy`)
    return data
  },
  async remove(appId: string): Promise<{ job_id: string }> {
    const { data } = await http.delete<{ job_id: string }>(`/admin/apps/${appId}`)
    return data
  },
  async models(): Promise<ModelEntry[]> {
    const { data } = await http.get<ModelEntry[]>('/admin/models')
    return data
  },
  async chunkPresets(): Promise<ChunkPreset[]> {
    const { data } = await http.get<ChunkPreset[]>('/admin/chunk-presets')
    return data
  },
}

export const systemApi = {
  async get(): Promise<SystemSettings> {
    const { data } = await http.get<SystemSettings>('/admin/system/settings')
    return data
  },
  async patch(defaultSessionConfig: Record<string, unknown>): Promise<SystemSettings> {
    const { data } = await http.patch<SystemSettings>('/admin/system/settings', {
      default_session_config: defaultSessionConfig,
    })
    return data
  },
}
