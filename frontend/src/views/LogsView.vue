<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  feedbackApi,
  logApi,
  type ConversationDetail,
  type ConversationRow,
  type FeedbackRow,
} from '@/api/logs'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()
const appId = computed(() => String(route.params.appId ?? auth.currentAppId ?? ''))
const canRelease = computed(() => auth.can('handoff.release'))
const canClearCallback = computed(() => auth.can('callback.clear'))
const canHandle = computed(() => auth.can('feedback.write'))

const tab = ref<'conversations' | 'callbacks' | 'feedback'>('conversations')
const loading = ref(false)
const rows = ref<ConversationRow[]>([])
const cursor = ref<string | null>(null)
const feedbackRows = ref<FeedbackRow[]>([])
const filters = reactive({
  channels: '',
  status: '',
  handoff_status: '',
  close_reason: '',
  needs_callback: false,
  intents: '',
  downvoted: false,
  q: '',
  since: '',
  until: '',
})

const detailVisible = ref(false)
const detail = ref<ConversationDetail | null>(null)
const detailLoading = ref(false)

function range(): [string, string] | null {
  if (!filters.since && !filters.until) return null
  const start = filters.since ? new Date(filters.since).toISOString() : ''
  const end = filters.until ? new Date(filters.until).toISOString() : ''
  return [start, end]
}

async function load() {
  if (!appId.value) return
  loading.value = true
  try {
    const [since, until] = range() ?? ['', '']
    if (tab.value === 'feedback') {
      const page = await feedbackApi.list(appId.value, { limit: 50 })
      feedbackRows.value = page.items
    } else if (tab.value === 'callbacks') {
      const page = await logApi.callbacks(appId.value, false)
      rows.value = page.items
    } else {
      const page = await logApi.conversations(appId.value, {
        channels: filters.channels,
        status: filters.status,
        handoffStatus: filters.handoff_status,
        closeReason: filters.close_reason,
        needsCallback: filters.needs_callback,
        intents: filters.intents,
        downvoted: filters.downvoted,
        q: filters.q,
        since,
        until,
        limit: 20,
      })
      rows.value = page.items
      cursor.value = page.next_cursor
    }
  } finally {
    loading.value = false
  }
}

async function more() {
  if (!cursor.value) return
  const [since, until] = range() ?? ['', '']
  const page = await logApi.conversations(appId.value, {
    ...filters,
    handoffStatus: filters.handoff_status,
    channels: filters.channels,
    intents: filters.intents,
    since,
    until,
    cursor: cursor.value,
    limit: 20,
  })
  rows.value = [...rows.value, ...page.items]
  cursor.value = page.next_cursor
}

async function openDetail(row: ConversationRow) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await logApi.detail(appId.value, row.id)
  } finally {
    detailLoading.value = false
  }
}

async function onRelease() {
  if (!detail.value) return
  const { value } = await ElMessageBox.prompt(
    '强制释放会结束当前人工会话，可备注原因',
    '强制释放',
    {
      inputValue: '',
      inputPlaceholder: '可选',
    },
  )
  detail.value = await logApi.release(appId.value, detail.value.conversation.id, value || undefined)
  ElMessage.success('已释放')
  await load()
}

async function onClearCallback(row: ConversationRow) {
  const { value } = await ElMessageBox.prompt('标记已回访，可备注处理结果', '标记已回访', {
    inputValue: '',
    inputPlaceholder: '可选',
  })
  await logApi.clearCallback(appId.value, row.id, value || undefined)
  ElMessage.success('已标记回访')
  await load()
}

async function onHandle(row: FeedbackRow, handled: boolean) {
  await feedbackApi.handle(appId.value, row.id, handled)
  ElMessage.success(handled ? '已标记已处理' : '已恢复未处理')
  await load()
}

const CHANNEL_LABEL: Record<string, string> = {
  api: '开放接口',
  openai: 'OpenAI',
  debug: '调试',
  eval: '评测',
}
const HANDOFF_LABEL: Record<string, string> = {
  none: '未转人工',
  pending: '排队中',
  accepted: '已接单',
  released: '已交还',
}

onMounted(load)
</script>

<template>
  <div>
    <div class="head">
      <div>
        <div class="aics-page-title">会话与反馈</div>
        <div class="aics-page-subtitle">按渠道、状态与关键字回查会话；处理待回访与用户反馈。</div>
      </div>
    </div>

    <el-card class="aics-card">
      <el-tabs v-model="tab" @tab-change="load">
        <el-tab-pane label="会话" name="conversations" />
        <el-tab-pane label="待回访" name="callbacks" />
        <el-tab-pane label="反馈" name="feedback" />
      </el-tabs>

      <template v-if="tab === 'conversations'">
        <div class="filters">
          <el-input
            v-model="filters.q"
            placeholder="关键字（消息内容）"
            clearable
            style="width: 220px"
            @keyup.enter="load"
          />
          <el-select
            v-model="filters.channels"
            placeholder="渠道（默认 api/openai）"
            clearable
            style="width: 200px"
          >
            <el-option label="开放接口" value="api" />
            <el-option label="OpenAI" value="openai" />
            <el-option label="调试" value="debug" />
            <el-option label="评测" value="eval" />
          </el-select>
          <el-select v-model="filters.status" placeholder="会话状态" clearable style="width: 140px">
            <el-option label="进行中" value="active" />
            <el-option label="空闲" value="idle" />
            <el-option label="已结束" value="closed" />
          </el-select>
          <el-select
            v-model="filters.handoff_status"
            placeholder="转人工"
            clearable
            style="width: 140px"
          >
            <el-option
              v-for="(label, key) in HANDOFF_LABEL"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
          <el-input
            v-model="filters.intents"
            placeholder="意图（逗号分隔）"
            clearable
            style="width: 180px"
          />
          <el-checkbox v-model="filters.needs_callback">只看待回访</el-checkbox>
          <el-checkbox v-model="filters.downvoted">只看点踩</el-checkbox>
          <el-date-picker
            v-model="filters.since"
            type="datetime"
            placeholder="起始"
            style="width: 180px"
          />
          <el-date-picker
            v-model="filters.until"
            type="datetime"
            placeholder="截止"
            style="width: 180px"
          />
          <el-button type="primary" @click="load">查询</el-button>
        </div>

        <el-table v-loading="loading" :data="rows" empty-text="没有匹配的会话">
          <el-table-column label="会话" width="150">
            <template #default="{ row }">
              <el-link type="primary" @click="openDetail(row)">{{ row.id.slice(0, 8) }}</el-link>
            </template>
          </el-table-column>
          <el-table-column label="渠道" width="110">
            <template #default="{ row }">{{ CHANNEL_LABEL[row.channel] ?? row.channel }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'closed' ? 'info' : 'success'">{{
                row.status
              }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="转人工" width="110">
            <template #default="{ row }">{{
              HANDOFF_LABEL[row.handoff_status] ?? row.handoff_status
            }}</template>
          </el-table-column>
          <el-table-column label="意图" width="120">
            <template #default="{ row }">{{ row.intent ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="message_count" label="消息" width="80" />
          <el-table-column label="外部用户" width="150">
            <template #default="{ row }">{{ row.external_user_id ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="最近活动" width="180">
            <template #default="{ row }">{{ row.last_activity_at ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="标记" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.needs_callback" size="small" type="warning">待回访</el-tag>
              <el-tag v-if="row.downvotes" size="small" type="danger" class="tag"
                >点踩 {{ row.downvotes }}</el-tag
              >
            </template>
          </el-table-column>
        </el-table>
        <div v-if="cursor" class="more">
          <el-button @click="more">加载更多</el-button>
        </div>
      </template>

      <template v-else-if="tab === 'callbacks'">
        <el-table v-loading="loading" :data="rows" empty-text="没有待回访会话">
          <el-table-column prop="id" label="会话" width="180" />
          <el-table-column prop="external_user_id" label="外部用户" width="180" />
          <el-table-column label="待回访原因" width="160">
            <template #default="{ row }">{{ row.close_reason ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="last_activity_at" label="最近活动" width="190" />
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button v-if="canClearCallback" link type="primary" @click="onClearCallback(row)"
                >标记已回访</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </template>

      <template v-else>
        <el-table v-loading="loading" :data="feedbackRows" empty-text="还没有反馈">
          <el-table-column label="评价" width="90">
            <template #default="{ row }">
              <el-tag :type="row.rating === 1 ? 'success' : 'danger'" size="small">
                {{ row.rating === 1 ? '点赞' : '点踩' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="消息摘录" min-width="300">
            <template #default="{ row }">{{ row.message?.excerpt ?? '（消息已清理）' }}</template>
          </el-table-column>
          <el-table-column prop="comment" label="用户留言" min-width="180" />
          <el-table-column prop="created_at" label="时间" width="180" />
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.handled ? 'success' : 'info'">
                {{ row.handled ? '已处理' : '未处理' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button v-if="canHandle" link type="primary" @click="onHandle(row, !row.handled)">
                {{ row.handled ? '恢复未处理' : '标记已处理' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <el-drawer v-model="detailVisible" title="会话详情" size="720px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="会话">{{ detail.conversation.id }}</el-descriptions-item>
            <el-descriptions-item label="渠道">
              {{ CHANNEL_LABEL[detail.conversation.channel] ?? detail.conversation.channel }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">{{
              detail.conversation.status
            }}</el-descriptions-item>
            <el-descriptions-item label="转人工">{{
              HANDOFF_LABEL[detail.conversation.handoff_status]
            }}</el-descriptions-item>
            <el-descriptions-item label="外部用户">{{
              detail.conversation.external_user_id ?? '—'
            }}</el-descriptions-item>
            <el-descriptions-item label="消息数">{{
              detail.conversation.message_count
            }}</el-descriptions-item>
            <el-descriptions-item label="开始">{{
              detail.conversation.created_at ?? '—'
            }}</el-descriptions-item>
            <el-descriptions-item label="结束">
              {{ detail.conversation.closed_at ?? '—' }}
              <span v-if="detail.conversation.close_reason"
                >（{{ detail.conversation.close_reason }}）</span
              >
            </el-descriptions-item>
          </el-descriptions>

          <div class="drawer-actions">
            <el-button
              v-if="
                canRelease && ['pending', 'accepted'].includes(detail.conversation.handoff_status)
              "
              @click="onRelease"
            >
              强制释放
            </el-button>
          </div>

          <h4>消息</h4>
          <el-timeline>
            <el-timeline-item
              v-for="message in detail.messages"
              :key="message.id"
              :timestamp="message.created_at ?? ''"
            >
              <div class="msg-role">
                {{ message.role }}<span v-if="message.kind"> · {{ message.kind }}</span>
              </div>
              <div class="msg-body">{{ message.content }}</div>
            </el-timeline-item>
          </el-timeline>

          <template v-if="detail.summary">
            <h4>摘要 v{{ detail.summary.version }}</h4>
            <pre>{{ JSON.stringify(detail.summary.value, null, 2) }}</pre>
          </template>

          <template v-if="detail.facts.length">
            <h4>记忆事实</h4>
            <el-table :data="detail.facts" size="small">
              <el-table-column prop="key" label="键" width="160" />
              <el-table-column label="值" min-width="180">
                <template #default="{ row }">{{ JSON.stringify(row.value) }}</template>
              </el-table-column>
              <el-table-column prop="category" label="分类" width="120" />
            </el-table>
          </template>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.head {
  margin-bottom: 16px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}
.more {
  margin-top: 12px;
  text-align: right;
}
.tag {
  margin-left: 6px;
}
.drawer-actions {
  margin: 12px 0;
}
.msg-role {
  font-size: 12px;
  color: var(--aics-text-3, #8c8c8c);
}
.msg-body {
  white-space: pre-wrap;
  word-break: break-word;
}
pre {
  padding: 12px;
  background: #141414;
  color: #e6f4ff;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
}
</style>
