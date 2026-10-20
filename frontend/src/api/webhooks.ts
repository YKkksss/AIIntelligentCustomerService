import { http } from '@/api/http'

/** M4.5 ④：Webhook 配置与投递记录（`/api/v1/admin/apps/{app_id}/webhook*`）。 */

export interface WebhookBody {
  url: string | null
  secret_prefix: string | null
  ready: boolean
}

export interface DeliveryRow {
  id: string
  event: string
  seq: number
  conversation_id: string | null
  status: 'pending' | 'delivered' | 'failed'
  attempts: number
  status_code: number | null
  error: string | null
  next_attempt_at: string | null
  delivered_at: string | null
  created_at: string
}

export interface DeliveryPage {
  items: DeliveryRow[]
}

export interface WebhookTestResult {
  ok: boolean
  status_code: number | null
  error: string | null
  elapsed_ms: number
}

export interface WebhookSecret {
  secret: string
  secret_prefix: string
}

const BASE = (appId: string) => `/admin/apps/${appId}`

export const webhookApi = {
  config: (appId: string) => http.get<WebhookBody>(`${BASE(appId)}/webhook`).then((r) => r.data),
  deliveries: (appId: string, params: { status?: string; limit?: number; before?: string } = {}) =>
    http
      .get<DeliveryPage>(`${BASE(appId)}/webhook-deliveries`, {
        params: { status: params.status || undefined, limit: params.limit, before: params.before },
      })
      .then((r) => r.data.items),
  redeliver: (appId: string, deliveryId: string) =>
    http
      .post<DeliveryRow>(`${BASE(appId)}/webhook-deliveries/${deliveryId}/redeliver`)
      .then((r) => r.data),
  test: (appId: string) =>
    http.post<WebhookTestResult>(`${BASE(appId)}/webhook/test`).then((r) => r.data),
  rotateSecret: (appId: string) =>
    http.post<WebhookSecret>(`${BASE(appId)}/webhook/secret`).then((r) => r.data),
}
