import { http } from '@/api/http'

/** M4.5 ③：会话日志、待回访与反馈（`/api/v1/admin/apps/{app_id}`）。 */

export interface ConversationRow {
  id: string
  channel: 'api' | 'openai' | 'debug' | 'eval'
  status: 'active' | 'idle' | 'closed'
  title: string | null
  external_user_id: string | null
  intent: string | null
  downvotes: number
  handoff_status: 'none' | 'pending' | 'accepted' | 'released'
  needs_callback: boolean
  message_count: number
  created_at: string | null
  last_activity_at: string | null
  closed_at: string | null
  close_reason: string | null
}

export interface ConversationPage {
  items: ConversationRow[]
  next_cursor: string | null
}

export interface LogMessage {
  id: string
  role: string
  kind: string | null
  agent_id: string | null
  content: string
  created_at: string | null
  answer_type: string | null
  intent: string | null
  handoff: boolean | null
  degraded: boolean | null
}

export interface LogSummary {
  value: Record<string, unknown>
  version: number
  until_msg_id: string | null
  updated_at: string | null
}

export interface LogFact {
  key: string
  value: unknown
  category: string | null
  confidence: number | null
  version: number
  first_seen_at: string | null
}

export interface ConversationDetail {
  conversation: ConversationRow
  messages: LogMessage[]
  summary: LogSummary | null
  facts: LogFact[]
}

export interface LogFilter {
  channels?: string
  status?: string
  handoffStatus?: string
  closeReason?: string
  needsCallback?: boolean
  intents?: string
  downvoted?: boolean
  since?: string
  until?: string
  q?: string
  limit?: number
  cursor?: string
}

export interface FeedbackMessage {
  id: string
  role: string
  intent: string | null
  excerpt: string
}

export interface FeedbackRow {
  id: string
  message_id: string
  conversation_id: string | null
  rating: number
  comment: string | null
  source: 'api' | 'admin'
  handled: boolean
  created_at: string | null
  updated_at: string | null
  message: FeedbackMessage | null
}

export interface FeedbackPage {
  items: FeedbackRow[]
  next_cursor: string | null
}

const BASE = (appId: string) => `/admin/apps/${appId}`

export const logApi = {
  conversations: (appId: string, filter: LogFilter) =>
    http
      .get<ConversationPage>(`${BASE(appId)}/conversations`, {
        params: {
          channel: filter.channels || undefined,
          status: filter.status || undefined,
          handoff_status: filter.handoffStatus || undefined,
          close_reason: filter.closeReason || undefined,
          needs_callback: filter.needsCallback === true ? true : undefined,
          intent: filter.intents || undefined,
          downvoted: filter.downvoted === true ? true : undefined,
          since: filter.since || undefined,
          until: filter.until || undefined,
          q: filter.q || undefined,
          limit: filter.limit,
          cursor: filter.cursor || undefined,
        },
      })
      .then((r) => r.data),
  detail: (appId: string, conversationId: string) =>
    http
      .get<ConversationDetail>(`${BASE(appId)}/conversations/${conversationId}`)
      .then((r) => r.data),
  release: (appId: string, conversationId: string, note?: string) =>
    http
      .post<ConversationDetail>(`${BASE(appId)}/conversations/${conversationId}/release`, {
        note: note || null,
      })
      .then((r) => r.data),
  callbacks: (appId: string, includeCleared = false) =>
    http
      .get<ConversationPage>(`${BASE(appId)}/callbacks`, {
        params: { include_cleared: includeCleared },
      })
      .then((r) => r.data),
  clearCallback: (appId: string, conversationId: string, note?: string) =>
    http
      .post<ConversationDetail>(`${BASE(appId)}/callbacks/${conversationId}`, {
        note: note || null,
      })
      .then((r) => r.data),
}

export const feedbackApi = {
  list: (appId: string, params: { rating?: number; handled?: boolean; limit?: number } = {}) =>
    http
      .get<FeedbackPage>(`${BASE(appId)}/feedback`, {
        params: {
          rating: params.rating,
          handled: params.handled,
          limit: params.limit,
        },
      })
      .then((r) => r.data),
  handle: (appId: string, feedbackId: string, handled = true) =>
    http
      .patch<FeedbackRow>(`${BASE(appId)}/feedback/${feedbackId}`, { handled })
      .then((r) => r.data),
}
