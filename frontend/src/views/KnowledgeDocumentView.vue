<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { knowledgeApi, type DocumentChunk, type DocumentRow } from '@/api/knowledge'
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
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(false)
const busy = ref<string | null>(null)

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

async function reload() {
  await Promise.all([loadDoc(), loadChunks()])
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
      <el-button v-if="canWrite" @click="onReindex">重建</el-button>
      <el-button @click="reload">刷新</el-button>
    </div>

    <el-descriptions v-if="doc" :column="4" border size="small">
      <el-descriptions-item label="切块数">{{ doc.chunk_count }}</el-descriptions-item>
      <el-descriptions-item label="构建版本">v{{ doc.index_version }}</el-descriptions-item>
      <el-descriptions-item label="大小">{{ formatBytes(doc.size_bytes) }}</el-descriptions-item>
      <el-descriptions-item label="更新时间">{{ doc.updated_at }}</el-descriptions-item>
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
      <el-table-column label="标题路径" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.heading_path || '-' }}</template>
      </el-table-column>
      <el-table-column label="正文" min-width="280">
        <template #default="{ row }">{{ excerpt(row.text, 100) }}</template>
      </el-table-column>
      <el-table-column label="tokens" width="90">
        <template #default="{ row }">{{ row.tokens }}</template>
      </el-table-column>
      <el-table-column label="复核" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="reviewTagType(row.review_status)">
            {{ row.review_status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            :disabled="!canWrite || busy === row.id"
            @change="onToggle(row)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="canWrite && row.review_status === 'flagged'"
            link
            type="primary"
            @click="onApprove(row)"
          >
            复核通过
          </el-button>
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
</style>
