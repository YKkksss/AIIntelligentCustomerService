import { http } from '@/api/http'

/** M4.5 ②：管理版 API Key 接口（`/api/v1/admin/apps/{app_id}/keys`）。 */

export interface KeyRow {
  id: string
  name: string
  prefix: string
  hint: string
  scopes: string[]
  rpm_limit: number
  daily_quota: number | null
  ip_allowlist: string[]
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
  created_at: string | null
  status: 'active' | 'expired' | 'revoked'
}

export interface KeyPage {
  items: KeyRow[]
}

/** 明文只在创建时返回一次。 */
export interface CreatedKey extends KeyRow {
  key: string
}

export interface KeyCreate {
  name: string
  scopes: string[]
  rpm_limit?: number
  daily_quota?: number | null
  expires_at?: string | null
  ip_allowlist?: string[] | null
}

/** `null` 表示清空；不传表示保持原值（后端用哨兵区分）。 */
export interface KeyPatch {
  rpm_limit?: number
  daily_quota?: number | null
  ip_allowlist?: string[] | null
}

export interface KeyExample {
  key_id: string
  name: string
  curl: string
  python: string
  javascript: string
  url: string
}

export const keyApi = {
  list: (appId: string) => http.get<KeyPage>(`/admin/apps/${appId}/keys`).then((r) => r.data.items),
  create: (appId: string, body: KeyCreate) =>
    http.post<CreatedKey>(`/admin/apps/${appId}/keys`, body).then((r) => r.data),
  update: (appId: string, keyId: string, body: KeyPatch) =>
    http.patch<KeyRow>(`/admin/apps/${appId}/keys/${keyId}`, body).then((r) => r.data),
  remove: (appId: string, keyId: string) =>
    http.delete<KeyRow>(`/admin/apps/${appId}/keys/${keyId}`).then((r) => r.data),
  example: (appId: string, keyId: string) =>
    http.get<KeyExample>(`/admin/apps/${appId}/keys/${keyId}/example`).then((r) => r.data),
}

export const KEY_SCOPES = ['chat', 'kb:read', 'kb:write', 'handoff:write'] as const
