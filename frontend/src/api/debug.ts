import { ref } from 'vue'
import { http } from './http'

/** 调试对话接口（管理接口参考「调试对话」，M4.4 ②）。 */
export interface DebugCitation {
  n: number
  chunk_id?: string
  document_id?: string
  document_title?: string
  heading_path?: string
  page?: number | null
  text?: string
  snippet?: string
  score?: number | null
}

export interface DebugUsage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

export interface DebugTrace {
  rewritten_query?: string
  intent?: Record<string, unknown>
  decision?: string
  decision_reason?: string
  vector_hits?: unknown[]
  keyword_hits?: unknown[]
  fused?: unknown[]
  reranked?: unknown[]
  degraded?: string[]
  timings?: Record<string, number>
  [key: string]: unknown
}

export interface DebugEnd {
  answer: string
  answer_type?: string
  handoff?: boolean
  handoff_reason?: string | null
  intent?: string | null
  used_citations?: number[]
  usage?: DebugUsage
  latency_ms?: number
  reply_by?: string
  handoff_status?: string | null
}

export interface DebugMessageRow {
  id: string
  role: string
  kind?: string
  content: string
  answer_type?: string | null
  handoff?: boolean
  intent?: { name: string; confidence: number | null; source: string | null } | null
  citations?: DebugCitation[]
  trace?: DebugTrace | null
  interrupted?: boolean
  created_at: string
}

export interface DebugHistory {
  conversation: {
    id: string
    channel: string
    status: string
    handoff_status?: string | null
    close_reason?: string | null
    closed_at?: string | null
    created_at: string
    last_activity_at?: string | null
  }
  items: DebugMessageRow[]
}

export interface DebugCompactResult {
  job_id: string
  compacted: boolean
  version?: number
  batches?: number
  until_msg_id?: string | null
  facts?: number
  usage?: DebugUsage
}

export interface TurnHandlers {
  onStart?: (body: { conversation_id: string; message_id: string }) => void
  onTrace?: (trace: DebugTrace) => void
  onCitations?: (citations: DebugCitation[]) => void
  onDelta?: (text: string) => void
  onEnd?: (body: DebugEnd) => void
  onError?: (body: { code: string; message: string }) => void
}

export interface TurnBody {
  question: string
  conversation_id?: string | null
  /** 临时覆盖 rag_config（不落库，只影响本轮）。 */
  debug_config?: Record<string, unknown> | null
}

export const debugApi = {
  history(appId: string, conversationId: string) {
    return http.get<DebugHistory>(`/admin/apps/${appId}/debug/conversations/${conversationId}`)
  },
  compact(appId: string, conversationId: string) {
    return http.post<DebugCompactResult>(`/admin/apps/${appId}/debug/compact`, {
      conversation_id: conversationId,
    })
  },
}

/** 解析一条 SSE 帧（`event:` + 若干 `data:`），注释帧（`: ping`）返回 null。 */
export function parseFrame(raw: string): { event: string; data: unknown } | null {
  let event = ''
  const data: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!event) return null
  const body = data.join('\n')
  try {
    return { event, data: body ? JSON.parse(body) : {} }
  } catch {
    return { event, data: body }
  }
}

/**
 * 发一轮调试对话并解析 SSE。
 *
 * 用 `fetch` 而不是 axios：浏览器里 axios 不给流式响应，`fetch` + `ReadableStream` 才能逐帧渲染。
 * 收到 `end` 或 `error` 即返回；`signal` 中止时抛 `AbortError`（调用方忽略）。
 */
export async function streamTurn(
  appId: string,
  token: string,
  body: TurnBody,
  handlers: TurnHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/v1/admin/apps/${appId}/debug/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '')
    let message = `HTTP ${resp.status}`
    try {
      const parsed = JSON.parse(text) as { error?: { code?: string; message?: string } }
      if (parsed.error?.message) message = parsed.error.message
      handlers.onError?.({ code: parsed.error?.code ?? 'HTTP_ERROR', message })
    } catch {
      handlers.onError?.({ code: 'HTTP_ERROR', message })
    }
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let index = buffer.indexOf('\n\n')
    while (index >= 0) {
      const frame = buffer.slice(0, index)
      buffer = buffer.slice(index + 2)
      const parsed = parseFrame(frame)
      if (parsed) dispatch(parsed.event, parsed.data, handlers)
      index = buffer.indexOf('\n\n')
    }
  }
}

function dispatch(event: string, data: unknown, handlers: TurnHandlers): void {
  if (event === 'start') handlers.onStart?.(data as { conversation_id: string; message_id: string })
  else if (event === 'trace') handlers.onTrace?.(data as DebugTrace)
  else if (event === 'citations')
    handlers.onCitations?.((data as { citations: DebugCitation[] }).citations)
  else if (event === 'delta') handlers.onDelta?.((data as { text: string }).text)
  else if (event === 'end') handlers.onEnd?.(data as DebugEnd)
  else if (event === 'error') handlers.onError?.(data as { code: string; message: string })
}

/** 当前进行中的轮次（用于「停止」按钮与切换会话时中止）。 */
export function useTurnAbort() {
  const controller = ref<AbortController | null>(null)
  const running = ref(false)
  function start(): AbortController {
    const c = new AbortController()
    controller.value = c
    running.value = true
    return c
  }
  function stop(): void {
    controller.value?.abort()
    controller.value = null
    running.value = false
  }
  function finish(): void {
    controller.value = null
    running.value = false
  }
  return { controller, running, start, stop, finish }
}
