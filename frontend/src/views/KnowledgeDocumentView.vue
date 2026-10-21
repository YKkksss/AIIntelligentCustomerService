<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  knowledgeApi,
  type ChunkQuestion,
  type DocumentChunk,
  type DocumentRow,
  type DuplicateRow,
} from '@/api/knowledge'
import { useAuthStore } from '@/stores/auth'
import { documentTagType, excerpt, formatBytes, reviewTagType } from '@/utils/knowledge'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const appId = computed(() => String(route.params.appId))
const documentId = computed(() => String(route.params.documentId))
const canWrite = computed(() => auth.can('kb.write'))

const doc = ref<DocumentRow | null>(null)
const chunks = ref<DocumentChunk[]>([])
const duplicates = ref<DuplicateRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(false)
const busy = ref<string | null>(null)

// 正文编辑 / 手工新增共用一个编辑对话框
const editor = reactive({
  open: false,
  mode: 'edit' as 'edit' | 'create',
  id: '',
  text: '',
  heading: '',
})
const validity = reactive({ open: false, effective: '', expires: '' })
const drawer = reactive({
  open: false,
  chunkId: '',
  title: '',
  items: [] as ChunkQuestion[],
  draft: '',
})

/** 日期选择器给的是本地时间字符串，统一转成带时区的 ISO。 */
function toIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null
}

function toLocal(value: string | null): string {
  if (!value) return ''
  const d = new Date(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}:${pad(d.getSeconds())}`
}

async function loadDoc() {
  doc.value = await knowledgeApi.document(appId.value, documentId.value)
}

async function loadChunks() {
  loading.value = true
  try {
    const body = await knowledgeApi.documentChunks(appId.value, documentId.value, {
      page: page.value,
      page_size: pageSize,
    })
    chunks.value = body.items
    total.value = body.total
  } finally {
    loading.value = false
  }
}

async function loadDuplicates() {
  try {
    const body = await knowledgeApi.duplicates(appId.value, documentId.value)
    duplicates.value = body.items
  } catch {
    duplicates.value = []
  }
}

async function reload() {
  await Promise.all([loadDoc(), loadChunks(), loadDuplicates()])
}

async function onToggle(row: DocumentChunk) {
  busy.value = row.id
  try {
    await knowledgeApi.patchChunk(appId.value, row.id, { enabled: row.enabled })
    ElMessage.success(row.enabled ? '已启用' : '已禁用（不再被检索到）')
  } catch {
    row.enabled = !row.enabled
  } finally {
    busy.value = null
  }
}

async function onApprove(row: DocumentChunk) {
  busy.value = row.id
  try {
    await knowledgeApi.patchChunk(appId.value, row.id, { review_status: 'approved' })
    ElMessage.success('已复核通过')
    await reload()
  } finally {
    busy.value = null
  }
}

function openEdit(row: DocumentChunk) {
  editor.mode = 'edit'
  editor.id = row.id
  editor.text = row.text
  editor.heading = row.heading_path ?? ''
  editor.open = true
}

function openCreate() {
  editor.mode = 'create'
  editor.id = ''
  editor.text = ''
  editor.heading = ''
  editor.open = true
}

async function submitEditor() {
  const text = editor.text.trim()
  if (!text) {
    ElMessage.warning('正文不能为空')
    return
  }
  busy.value = 'editor'
  try {
    if (editor.mode === 'create') {
      await knowledgeApi.createChunk(appId.value, documentId.value, {
        text,
        heading_path: editor.heading.trim() || undefined,
      })
      ElMessage.success('已新增手工切块（重建后仍保留）')
    } else {
      await knowledgeApi.patchChunk(appId.value, editor.id, {
        text,
        heading_path: editor.heading.trim(),
      })
      ElMessage.success('已保存并重新向量化该切块')
    }
    editor.open = false
    await reload()
  } finally {
    busy.value = null
  }
}

async function onDelete(row: DocumentChunk) {
  await ElMessageBox.confirm(`删除第 ${row.ordinal} 块？向量点会一并删除。`, '删除切块', {
    type: 'warning',
  })
  busy.value = row.id
  try {
    await knowledgeApi.deleteChunk(appId.value, row.id)
    ElMessage.success('已删除')
    await reload()
  } finally {
    busy.value = null
  }
}

function openValidity() {
  validity.effective = toLocal(doc.value?.effective_at ?? null)
  validity.expires = toLocal(doc.value?.expires_at ?? null)
  validity.open = true
}

async function submitValidity() {
  busy.value = 'validity'
  try {
    await knowledgeApi.patchDocument(appId.value, documentId.value, {
      effective_at: toIso(validity.effective),
      expires_at: toIso(validity.expires),
    })
    ElMessage.success('有效期已更新（立即影响可检索范围）')
    validity.open = false
    await reload()
  } finally {
    busy.value = null
  }
}

async function openQuestions(row: DocumentChunk) {
  drawer.chunkId = row.id
  drawer.title = `第 ${row.ordinal} 块 · 补充问题`
  drawer.draft = ''
  drawer.open = true
  const body = await knowledgeApi.questions(appId.value, row.id)
  drawer.items = body.items
}

async function addQuestions() {
  const lines = drawer.draft
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (lines.length === 0) {
    ElMessage.warning('每行一个问题')
    return
  }
  busy.value = 'questions'
  try {
    await knowledgeApi.addQuestions(appId.value, drawer.chunkId, lines)
    drawer.draft = ''
    const body = await knowledgeApi.questions(appId.value, drawer.chunkId)
    drawer.items = body.items
    ElMessage.success('已添加')
  } finally {
    busy.value = null
  }
}

async function removeQuestion(item: ChunkQuestion) {
  busy.value = item.id
  try {
    await knowledgeApi.deleteQuestion(appId.value, item.id)
    drawer.items = drawer.items.filter((q) => q.id !== item.id)
  } finally {
    busy.value = null
  }
}

async function onReindex() {
  const ack = await knowledgeApi.reindex(appId.value, documentId.value)
  ElMessage.success(`已提交重建任务 ${ack.job_id.slice(0, 8)}`)
  await reload()
}

onMounted(reload)
</script>

<template>
  <div class="document">
    <div class="toolbar">
      <el-button @click="router.push({ name: 'app-knowledge', params: { appId } })"
        >返回列表</el-button
      >
      <h3 class="title">{{ doc?.title ?? '文档' }}</h3>
      <el-tag v-if="doc" size="small" :type="documentTagType(doc.status)">{{ doc.status }}</el-tag>
      <div class="spacer" />
      <el-button v-if="canWrite" @click="openValidity">有效期</el-button>
      <el-button v-if="canWrite" @click="openCreate">手工新增切块</el-button>
      <el-button v-if="canWrite" @click="onReindex">重建</el-button>
      <el-button @click="reload">刷新</el-button>
    </div>

    <el-alert
      v-if="duplicates.length > 0"
      type="warning"
      :closable="false"
      show-icon
      title="检测到与已有文档高度相似（只提示，不阻止入库）"
    >
      <div class="duplicates">
        <span v-for="item in duplicates" :key="item.document_id">
          <el-link
            type="primary"
            @click="
              router.push({
                name: 'app-knowledge-document',
                params: { appId, documentId: item.document_id },
              })
            "
          >
            {{ item.title }}{{ item.document_id ? '（当前文档）' : '' }}
          </el-link>
          <em>距离 {{ item.distance }}</em>
        </span>
      </div>
    </el-alert>

    <el-descriptions v-if="doc" :column="4" border size="small">
      <el-descriptions-item label="切块数">{{ doc.chunk_count }}</el-descriptions-item>
      <el-descriptions-item label="构建版本">v{{ doc.index_version }}</el-descriptions-item>
      <el-descriptions-item label="大小">{{ formatBytes(doc.size_bytes) }}</el-descriptions-item>
      <el-descriptions-item label="更新时间">{{ doc.updated_at }}</el-descriptions-item>
      <el-descriptions-item label="生效时间" :span="2">
        {{ doc.effective_at ? doc.effective_at : '立即生效' }}
      </el-descriptions-item>
      <el-descriptions-item label="失效时间" :span="2">
        {{ doc.expires_at ? doc.expires_at : '长期有效' }}
      </el-descriptions-item>
      <el-descriptions-item label="来源" :span="4">
        {{ doc.source_type === 'url' ? doc.source_uri : doc.source_uri || '（本地上传）' }}
      </el-descriptions-item>
    </el-descriptions>

    <el-alert v-if="doc?.error" type="error" :closable="false" show-icon :title="doc.error" />

    <el-empty v-if="!loading && chunks.length === 0" description="还没有切块（文档可能仍在处理）" />

    <el-table v-else v-loading="loading" :data="chunks" row-key="id">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="chunk-text">{{ row.text }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="ordinal" label="序号" width="80" />
      <el-table-column label="标题路径" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.heading_path || '-' }}</template>
      </el-table-column>
      <el-table-column label="正文" min-width="260">
        <template #default="{ row }">{{ excerpt(row.text, 100) }}</template>
      </el-table-column>
      <el-table-column label="来源" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.source === 'parsed' ? 'info' : 'success'">
            {{ row.source }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="tokens" width="90">
        <template #default="{ row }">{{ row.tokens }}</template>
      </el-table-column>
      <el-table-column label="复核" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="reviewTagType(row.review_status)">
            {{ row.review_status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :disabled="!canWrite || busy === row.id"
            @change="onToggle(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canWrite" link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link @click="openQuestions(row)">补充问题</el-button>
          <el-button
            v-if="canWrite && row.review_status === 'flagged'"
            link
            type="primary"
            @click="onApprove(row)"
          >
            复核
          </el-button>
          <el-button v-if="canWrite" link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-if="total > pageSize"
      v-model:current-page="page"
      :page-size="pageSize"
      :total="total"
      layout="prev, pager, next"
      class="pager"
      @current-change="loadChunks"
    />

    <el-dialog
      v-model="editor.open"
      :title="editor.mode === 'create' ? '手工新增切块' : '编辑切块正文'"
      width="720px"
    >
      <el-form label-width="72px">
        <el-form-item label="标题路径">
          <el-input v-model="editor.heading" placeholder="例如：退换货 / 退款时效（可留空）" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input
            v-model="editor.text"
            type="textarea"
            :rows="12"
            maxlength="4000"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <p class="hint">
        保存后只重新计算该切块的分词与向量；人工改过的切块标记为 edited，重建索引时不会覆盖。
      </p>
      <template #footer>
        <el-button @click="editor.open = false">取消</el-button>
        <el-button type="primary" :loading="busy === 'editor'" @click="submitEditor"
          >保存</el-button
        >
      </template>
    </el-dialog>

    <el-dialog v-model="validity.open" title="文档有效期" width="480px">
      <el-form label-width="90px">
        <el-form-item label="生效时间">
          <el-date-picker
            v-model="validity.effective"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="留空表示立即可检索"
            clearable
          />
        </el-form-item>
        <el-form-item label="失效时间">
          <el-date-picker
            v-model="validity.expires"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="留空表示长期有效"
            clearable
          />
        </el-form-item>
      </el-form>
      <p class="hint">时间窗只影响检索：时间外的切块不会进入召回，向量点仍保留（见 ISSUE-96）。</p>
      <template #footer>
        <el-button @click="validity.open = false">取消</el-button>
        <el-button type="primary" :loading="busy === 'validity'" @click="submitValidity"
          >保存</el-button
        >
      </template>
    </el-dialog>

    <el-drawer v-model="drawer.open" :title="drawer.title" size="460px">
      <div class="questions">
        <el-empty v-if="drawer.items.length === 0" description="还没有补充问题" />
        <div v-for="item in drawer.items" :key="item.id" class="question">
          <span>{{ item.question }}</span>
          <el-tag size="small" type="info">{{ item.source }}</el-tag>
          <el-button link type="danger" @click="removeQuestion(item)">删除</el-button>
        </div>
        <el-input
          v-if="canWrite"
          v-model="drawer.draft"
          type="textarea"
          :rows="4"
          placeholder="每行一个问题，例如：退款多久到账？"
        />
        <el-button
          v-if="canWrite"
          type="primary"
          :loading="busy === 'questions'"
          @click="addQuestions"
        >
          添加
        </el-button>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.document {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title {
  margin: 0;
}
.spacer {
  flex: 1;
}
.chunk-text {
  white-space: pre-wrap;
  padding: 8px 16px;
  color: var(--el-text-color-regular);
}
.pager {
  justify-content: flex-end;
}
.hint {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.duplicates {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 12px;
}
.questions {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.question {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
</style>
