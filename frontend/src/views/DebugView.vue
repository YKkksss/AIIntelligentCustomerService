<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Fold, Position, Refresh, VideoPause } from '@element-plus/icons-vue'
import {
  debugApi,
  streamTurn,
  useTurnAbort,
  type DebugCitation,
  type DebugCompactResult,
  type DebugMessageRow,
  type DebugTrace,
  type DebugUsage,
} from '@/api/debug'
import { useAuthStore } from '@/stores/auth'
import { Conversation, ConversationContent } from '@/components/ai-elements/conversation'
import { Message, MessageContent, MessageResponse } from '@/components/ai-elements/message'
import { Loader } from '@/components/ai-elements/loader'

/** 页面上的一条消息（用户 / AI / 系统提示）。 */
interface Turn {
  key: string
  role: 'user' | 'assistant' | 'system'
  content: string
  streaming?: boolean
  citations?: DebugCitation[]
  trace?: DebugTrace | null
  usage?: DebugUsage
  latencyMs?: number
  failed?: boolean
}

const route = useRoute()
const auth = useAuthStore()
const appId = computed(() => String(route.params.appId ?? auth.currentAppId ?? ''))

const conversationId = ref<string | null>(null)
const turns = ref<Turn[]>([])
const question = ref('')
const trace = ref<DebugTrace | null>(null)
const citations = ref<DebugCitation[]>([])
const usage = ref<DebugUsage | null>(null)
const latency = ref<number | null>(null)
const compactResult = ref<DebugCompactResult | null>(null)
const loadingHistory = ref(false)
const showRaw = ref(false)
const { running, start, stop, finish } = useTurnAbort()
let seq = 0

const shortId = computed(() => (conversationId.value ? conversationId.value.slice(0, 8) : '新会话'))
const lastTrace = computed(() => trace.value)

const vectorHits = computed(
  () => (lastTrace.value?.vector_hits as unknown[] | undefined)?.length ?? 0,
)
const keywordHits = computed(
  () => (lastTrace.value?.keyword_hits as unknown[] | undefined)?.length ?? 0,
)
const fusedHits = computed(() => (lastTrace.value?.fused as unknown[] | undefined)?.length ?? 0)
const rerankedHits = computed(
  () => (lastTrace.value?.reranked as unknown[] | undefined)?.length ?? 0,
)
const intent = computed(() => (lastTrace.value?.intent ?? null) as Record<string, unknown> | null)
const timings = computed(() =>
  Object.entries((lastTrace.value?.timings ?? {}) as Record<string, number>).filter(
    ([, value]) => typeof value === 'number',
  ),
)

function pushTurn(turn: Omit<Turn, 'key'>): Turn {
  const item: Turn = { ...turn, key: `t${++seq}` }
  turns.value = [...turns.value, item]
  return turns.value[turns.value.length - 1] as Turn
}

function patchTurn(key: string, patch: Partial<Turn>): void {
  const index = turns.value.findIndex((item) => item.key === key)
  if (index >= 0) turns.value[index] = { ...turns.value[index], ...patch } as Turn
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const box = document.querySelector('.thread-body')
  if (box) box.scrollTop = box.scrollHeight
}

function reset(): void {
  stop()
  conversationId.value = null
  turns.value = []
  trace.value = null
  citations.value = []
  usage.value = null
  latency.value = null
  compactResult.value = null
  showRaw.value = false
  question.value = ''
}

async function send(): Promise<void> {
  const text = question.value.trim()
  if (!text || running.value) return
  if (!appId.value) {
    ElMessage.error('缺少应用 ID')
    return
  }
  question.value = ''
  pushTurn({ role: 'user', content: text })
  const reply = pushTurn({ role: 'assistant', content: '', streaming: true })
  trace.value = null
  citations.value = []
  usage.value = null
  latency.value = null
  await scrollToBottom()

  const controller = start()
  try {
    await streamTurn(
      appId.value,
      auth.accessToken ?? '',
      {
        question: text,
        conversation_id: conversationId.value,
      },
      {
        onStart: (body) => {
          conversationId.value = body.conversation_id
        },
        onTrace: (body) => {
          trace.value = body
          patchTurn(reply.key, { trace: body })
        },
        onCitations: (body) => {
          citations.value = body
          patchTurn(reply.key, { citations: body })
        },
        onDelta: (delta) => {
          const current = turns.value.find((item) => item.key === reply.key)
          patchTurn(reply.key, { content: `${current?.content ?? ''}${delta}` })
          void scrollToBottom()
        },
        onEnd: (body) => {
          patchTurn(reply.key, {
            content: body.answer,
            streaming: false,
            usage: body.usage,
            latencyMs: body.latency_ms,
            citations: citations.value,
          })
          usage.value = body.usage ?? null
          latency.value = body.latency_ms ?? null
          void scrollToBottom()
        },
        onError: (body) => {
          patchTurn(reply.key, {
            content: `请求失败：${body.message}（${body.code}）`,
            streaming: false,
            failed: true,
          })
        },
      },
      controller.signal,
    )
  } catch (error) {
    if ((error as Error).name !== 'AbortError') {
      ElMessage.error(`调试对话失败：${(error as Error).message}`)
      patchTurn(reply.key, { content: '请求失败', streaming: false, failed: true })
    } else {
      patchTurn(reply.key, { streaming: false })
    }
  } finally {
    finish()
  }
}

function halt(): void {
  stop()
  ElMessage.info('已停止本轮（服务端会按中断轮次落库）')
}

async function reload(): Promise<void> {
  if (!conversationId.value) return
  loadingHistory.value = true
  try {
    const { data } = await debugApi.history(appId.value, conversationId.value)
    turns.value = data.items.map((row: DebugMessageRow) => ({
      key: `h${row.id}`,
      role: row.role === 'user' ? 'user' : row.role === 'assistant' ? 'assistant' : 'system',
      content: row.content,
      citations: row.citations ?? [],
      trace: row.trace ?? null,
    }))
    const last = [...data.items].reverse().find((row) => row.trace)
    trace.value = last?.trace ?? null
    citations.value = last?.citations ?? []
    await scrollToBottom()
  } catch {
    // 拦截器已提示
  } finally {
    loadingHistory.value = false
  }
}

async function compact(): Promise<void> {
  if (!conversationId.value) {
    ElMessage.warning('先进行一轮对话')
    return
  }
  try {
    const { data } = await debugApi.compact(appId.value, conversationId.value)
    compactResult.value = data
    ElMessage.success(data.compacted ? `已压缩 ${data.batches ?? 0} 批` : '上下文尚未达到压缩阈值')
  } catch {
    // 拦截器已提示
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

onBeforeUnmount(() => stop())
</script>

<template>
  <div class="debug-page aics-ai">
    <section class="chat-card aics-card">
      <header class="chat-head">
        <div class="head-left">
          <el-icon class="head-icon"><ChatDotRound /></el-icon>
          <span class="head-title">调试对话</span>
          <el-tag size="small" type="info" effect="plain">{{ shortId }}</el-tag>
          <el-tag v-if="conversationId" size="small" effect="plain">channel=debug</el-tag>
        </div>
        <div class="head-right">
          <el-tooltip content="重新加载当前会话的历史消息" placement="top">
            <el-button text :disabled="!conversationId || loadingHistory" @click="reload">
              <el-icon><Refresh /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip content="立即对当前调试会话执行一次上下文压缩" placement="top">
            <el-button text :disabled="!conversationId" @click="compact">压缩</el-button>
          </el-tooltip>
          <el-button text @click="reset">新会话</el-button>
        </div>
      </header>

      <div class="thread">
        <Conversation>
          <ConversationContent class="thread-body">
            <div v-if="!turns.length" class="empty">
              <el-icon class="empty-icon"><ChatDotRound /></el-icon>
              <p class="empty-title">向这个应用提问，看看检索与生成的过程</p>
              <p class="empty-hint">
                调试轮次记为 channel=debug，不影响对外统计；右侧面板显示本轮意图、检索命中与耗时。
              </p>
            </div>

            <Message
              v-for="turn in turns"
              :key="turn.key"
              :from="turn.role === 'user' ? 'user' : 'assistant'"
            >
              <MessageContent>
                <div v-if="turn.role === 'system'" class="system-note">{{ turn.content }}</div>
                <template v-else>
                  <MessageResponse v-if="turn.role === 'assistant'" :content="turn.content" />
                  <div v-else class="user-text">{{ turn.content }}</div>
                </template>
                <div v-if="turn.streaming" class="streaming">
                  <Loader :size="14" />
                  <span>生成中…</span>
                </div>
                <div v-if="turn.citations?.length" class="turn-citations">
                  <el-tag
                    v-for="c in turn.citations"
                    :key="`${turn.key}-${c.n}`"
                    size="small"
                    effect="plain"
                  >
                    [{{ c.n }}] {{ c.document_title ?? c.document_id }}
                  </el-tag>
                </div>
                <div v-if="turn.latencyMs" class="turn-meta">
                  {{ turn.latencyMs }} ms · {{ turn.usage?.total_tokens ?? 0 }} tokens
                </div>
              </MessageContent>
            </Message>
          </ConversationContent>
        </Conversation>
      </div>

      <footer class="composer">
        <el-input
          v-model="question"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 5 }"
          resize="none"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          :disabled="running"
          @keydown="onKeydown"
        />
        <div class="composer-actions">
          <span class="hint">临时参数可在「应用配置」调整后对比；调试会话不计入对外统计</span>
          <el-button v-if="running" type="danger" plain @click="halt">
            <el-icon><VideoPause /></el-icon>
            停止
          </el-button>
          <el-button v-else type="primary" :disabled="!question.trim()" @click="send">
            <el-icon><Position /></el-icon>
            发送
          </el-button>
        </div>
      </footer>
    </section>

    <aside class="panel-card aics-card">
      <header class="panel-head">
        <span class="panel-title">检索面板</span>
        <el-button text :disabled="!lastTrace" @click="showRaw = !showRaw">
          <el-icon><Fold /></el-icon>
          {{ showRaw ? '收起原始 trace' : '原始 trace' }}
        </el-button>
      </header>

      <el-scrollbar class="panel-body">
        <template v-if="lastTrace">
          <section class="panel-section">
            <h4>意图</h4>
            <div class="kv">
              <span class="k">name</span>
              <span class="v">{{ intent?.name ?? '—' }}</span>
            </div>
            <div class="kv">
              <span class="k">confidence</span>
              <span class="v">{{ intent?.confidence ?? '—' }}</span>
            </div>
            <div class="kv">
              <span class="k">source</span>
              <span class="v">{{ intent?.source ?? '—' }}</span>
            </div>
            <div class="kv">
              <span class="k">decision</span>
              <span class="v">{{ lastTrace.decision ?? '—' }}</span>
            </div>
            <p v-if="lastTrace.decision_reason" class="reason">
              {{ lastTrace.decision_reason }}
            </p>
          </section>

          <section class="panel-section">
            <h4>检索命中</h4>
            <div class="counts">
              <div class="count">
                <b>{{ vectorHits }}</b
                ><span>向量</span>
              </div>
              <div class="count">
                <b>{{ keywordHits }}</b
                ><span>关键词</span>
              </div>
              <div class="count">
                <b>{{ fusedHits }}</b
                ><span>融合</span>
              </div>
              <div class="count">
                <b>{{ rerankedHits }}</b
                ><span>重排</span>
              </div>
            </div>
            <p v-if="lastTrace.rewritten_query" class="reason">
              改写：{{ lastTrace.rewritten_query }}
            </p>
            <p v-if="lastTrace.degraded?.length" class="reason">
              降级：{{ (lastTrace.degraded as string[]).join('、') }}
            </p>
          </section>

          <section class="panel-section">
            <h4>耗时（ms）</h4>
            <div v-for="[name, value] in timings" :key="name" class="kv">
              <span class="k">{{ name }}</span>
              <span class="v">{{ value }}</span>
            </div>
            <div v-if="!timings.length" class="muted">本轮没有耗时数据</div>
          </section>

          <section v-if="citations.length" class="panel-section">
            <h4>引用</h4>
            <div v-for="c in citations" :key="c.n" class="citation">
              <div class="citation-title">[{{ c.n }}] {{ c.document_title ?? c.document_id }}</div>
              <div class="citation-meta">
                {{ c.heading_path || '—' }}<template v-if="c.page"> · p{{ c.page }}</template>
                <template v-if="c.score != null"> · score {{ c.score }}</template>
              </div>
              <div class="citation-text">{{ c.text || c.snippet }}</div>
            </div>
          </section>

          <section v-if="usage || latency" class="panel-section">
            <h4>本轮</h4>
            <div class="kv">
              <span class="k">latency</span>
              <span class="v">{{ latency ?? 0 }} ms</span>
            </div>
            <div class="kv">
              <span class="k">tokens</span>
              <span class="v">{{ usage?.total_tokens ?? 0 }}</span>
            </div>
          </section>

          <section v-if="compactResult" class="panel-section">
            <h4>压缩</h4>
            <div class="kv">
              <span class="k">compacted</span>
              <span class="v">{{ compactResult.compacted }}</span>
            </div>
            <div class="kv">
              <span class="k">batches</span>
              <span class="v">{{ compactResult.batches ?? 0 }}</span>
            </div>
            <div class="kv">
              <span class="k">version</span>
              <span class="v">{{ compactResult.version ?? '—' }}</span>
            </div>
          </section>

          <section v-if="showRaw" class="panel-section">
            <h4>原始 trace</h4>
            <pre class="raw">{{ JSON.stringify(lastTrace, null, 2) }}</pre>
          </section>
        </template>

        <div v-else class="panel-empty">
          <el-icon class="empty-icon"><Fold /></el-icon>
          <p>发一轮消息后，这里显示意图、检索命中与各阶段耗时</p>
        </div>
      </el-scrollbar>
    </aside>
  </div>
</template>

<style scoped>
.debug-page {
  display: grid;
  grid-template-columns: minmax(0, 692fr) minmax(360px, 464fr);
  gap: 16px;
  height: calc(100vh - 56px - 48px);
  min-height: 560px;
}
@media (max-width: 1180px) {
  .debug-page {
    grid-template-columns: minmax(0, 1fr);
    height: auto;
  }
  .panel-card {
    min-height: 420px;
  }
}
.chat-card,
.panel-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
  border-radius: 8px;
}
.chat-head,
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 52px;
  padding: 0 12px 0 16px;
  border-bottom: 1px solid var(--aics-split);
}
.head-left {
  display: flex;
  gap: 8px;
  align-items: center;
}
.head-icon {
  color: var(--aics-primary);
}
.head-title {
  font-size: 15px;
  font-weight: 600;
}
.head-right {
  display: flex;
  gap: 2px;
  align-items: center;
}
.thread {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}
.thread :deep(.thread-body) {
  padding-bottom: 8px;
}
.empty {
  padding: 72px 24px;
  color: var(--aics-text3);
  text-align: center;
}
.empty-icon {
  margin-bottom: 12px;
  font-size: 36px;
  color: var(--aics-border);
}
.empty-title {
  margin: 0 0 6px;
  font-size: 15px;
  color: var(--aics-text2);
}
.empty-hint {
  max-width: 420px;
  margin: 0 auto;
  font-size: 12px;
  line-height: 1.7;
}
.user-text {
  white-space: pre-wrap;
}
.system-note {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--aics-text2);
  background: var(--aics-bg, #f5f5f5);
  border-radius: 6px;
}
.streaming {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
  font-size: 12px;
  color: var(--aics-text3);
}
.turn-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.turn-meta {
  margin-top: 8px;
  font-size: 12px;
  color: var(--aics-text3);
}
.composer {
  padding: 12px 16px 14px;
  border-top: 1px solid var(--aics-split);
}
.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}
.hint {
  font-size: 12px;
  color: var(--aics-text3);
}
.panel-title {
  font-size: 15px;
  font-weight: 600;
}
.panel-body {
  flex: 1;
  min-height: 0;
  padding: 4px 16px 16px;
}
.panel-section {
  padding: 14px 0;
  border-bottom: 1px solid var(--aics-split);
}
.panel-section:last-child {
  border-bottom: 0;
}
.panel-section h4 {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 600;
  color: var(--aics-text);
}
.kv {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
  font-size: 12px;
}
.k {
  color: var(--aics-text3);
}
.v {
  max-width: 62%;
  overflow: hidden;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.reason {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--aics-text2);
}
.counts {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.count {
  padding: 10px 8px;
  text-align: center;
  background: var(--aics-bg, #f5f5f5);
  border-radius: 6px;
}
.count b {
  display: block;
  font-size: 18px;
  font-weight: 600;
  color: var(--aics-text);
}
.count span {
  font-size: 12px;
  color: var(--aics-text3);
}
.citation {
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--aics-bg, #f5f5f5);
  border-radius: 6px;
}
.citation-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--aics-text);
}
.citation-meta {
  margin: 4px 0;
  font-size: 12px;
  color: var(--aics-text3);
}
.citation-text {
  display: -webkit-box;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.6;
  color: var(--aics-text2);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}
.raw {
  max-height: 320px;
  padding: 10px;
  overflow: auto;
  font-family: var(--aics-font-mono, monospace);
  font-size: 11px;
  line-height: 1.6;
  color: #d4d4d4;
  background: #141414;
  border-radius: 6px;
}
.panel-empty {
  padding: 64px 16px;
  color: var(--aics-text3);
  text-align: center;
}
.muted {
  font-size: 12px;
  color: var(--aics-text3);
}
</style>
