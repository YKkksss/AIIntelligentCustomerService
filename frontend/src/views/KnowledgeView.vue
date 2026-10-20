<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type UploadFile } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { appApi, type ChunkPreset } from '@/api/admin'
import {
  knowledgeApi,
  type ChunkRow,
  type DocumentRow,
  type JobRow,
  type PreviewResult,
  type ReviewStatus,
} from '@/api/knowledge'
import { useAuthStore } from '@/stores/auth'
import {
  documentTagType,
  excerpt,
  formatBytes,
  formatChunkConfig,
  parseChunkConfig,
} from '@/utils/knowledge'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const appId = computed(() => String(route.params.appId))
const canWrite = computed(() => auth.can('kb.write'))

const tab = ref<'documents' | 'flagged' | 'jobs'>('documents')

// ---- 文档列表
const docs = ref<DocumentRow[]>([])
const docsTotal = ref(0)
const docsLoading = ref(false)
const docQuery = reactive({ status: '', q: '', page: 1, page_size: 20 })

async function loadDocs() {
  docsLoading.value = true
  try {
    const page = await knowledgeApi.listDocuments(appId.value, {
      status: docQuery.status || undefined,
      q: docQuery.q || undefined,
      page: docQuery.page,
      page_size: docQuery.page_size,
    })
    docs.value = page.items
    docsTotal.value = page.total
  } finally {
    docsLoading.value = false
  }
}

// ---- 待复核切块
const flagged = ref<ChunkRow[]>([])
const flaggedTotal = ref(0)
const flaggedLoading = ref(false)
const flaggedPage = ref(1)

async function loadFlagged() {
  flaggedLoading.value = true
  try {
    const page = await knowledgeApi.chunks(appId.value, {
      review_status: 'flagged' as ReviewStatus,
      page: flaggedPage.value,
      page_size: 20,
    })
    flagged.value = page.items
    flaggedTotal.value = page.total
  } finally {
    flaggedLoading.value = false
  }
}

// ---- 任务
const jobs = ref<JobRow[]>([])
const jobsTotal = ref(0)
const jobsLoading = ref(false)
const jobsPage = ref(1)

async function loadJobs() {
  jobsLoading.value = true
  try {
    const page = await knowledgeApi.jobs(appId.value, { page: jobsPage.value, page_size: 20 })
    jobs.value = page.items
    jobsTotal.value = page.total
  } finally {
    jobsLoading.value = false
  }
}

// ---- 上传
const presets = ref<ChunkPreset[]>([])
const uploadVisible = ref(false)
const uploading = ref(false)
const previewing = ref(false)
const previewResult = ref<PreviewResult | null>(null)
const uploadForm = reactive({ title: '', preset: '', configText: '', file: null as File | null })

/** 拖拽/点选后直接进入上传对话框，保留切块预览这一步。 */
function onDropFile(file: UploadFile) {
  onFileChange(file)
  uploadForm.preset = ''
  uploadForm.configText = ''
  uploadVisible.value = true
}

const urlForm = reactive({ url: '', title: '' })
const importingUrl = ref(false)

async function onImportUrl() {
  if (!urlForm.url.trim()) {
    ElMessage.warning('请填写链接')
    return
  }
  importingUrl.value = true
  try {
    const ack = await knowledgeApi.importUrl(appId.value, {
      url: urlForm.url.trim(),
      title: urlForm.title.trim() || undefined,
    })
    ElMessage.success(`已提交导入任务：${ack.job_id}`)
    urlForm.url = ''
    urlForm.title = ''
    await loadDocs()
  } finally {
    importingUrl.value = false
  }
}

function onFileChange(file: UploadFile) {
  uploadForm.file = (file.raw as File | undefined) ?? null
  previewResult.value = null
  if (!uploadForm.title && file.name) uploadForm.title = file.name.replace(/\.[^.]+$/, '')
}

function onPresetChange(name: string) {
  const preset = presets.value.find((item) => item.name === name)
  uploadForm.configText = preset ? formatChunkConfig(preset.config) : ''
  previewResult.value = null
}

function chunkConfig() {
  const parsed = parseChunkConfig(uploadForm.configText)
  if (parsed.error) {
    ElMessage.error(parsed.error)
    return undefined
  }
  return parsed.value && Object.keys(parsed.value).length > 0 ? parsed.value : undefined
}

async function onPreview() {
  if (!uploadForm.file) {
    ElMessage.warning('先选择文件')
    return
  }
  const config = chunkConfig()
  if (uploadForm.configText.trim() && !config) return
  previewing.value = true
  try {
    previewResult.value = await knowledgeApi.previewUpload(appId.value, uploadForm.file, {
      title: uploadForm.title || undefined,
      chunkConfig: config,
    })
  } finally {
    previewing.value = false
  }
}

async function onUpload() {
  if (!uploadForm.file) {
    ElMessage.warning('先选择文件')
    return
  }
  const config = chunkConfig()
  if (uploadForm.configText.trim() && !config) return
  uploading.value = true
  try {
    const result = await knowledgeApi.upload(appId.value, uploadForm.file, {
      title: uploadForm.title || undefined,
      chunkConfig: config,
    })
    ElMessage.success(result.duplicate ? '内容未变化，已跳过入库' : '已提交入库')
    uploadVisible.value = false
    Object.assign(uploadForm, { title: '', preset: '', configText: '', file: null })
    previewResult.value = null
    await Promise.all([loadDocs(), loadJobs()])
  } finally {
    uploading.value = false
  }
}

// ---- 行操作
async function onReindex(row: DocumentRow) {
  const ack = await knowledgeApi.reindex(appId.value, row.id)
  ElMessage.success(`已提交重建任务 ${ack.job_id.slice(0, 8)}`)
  await loadJobs()
}

async function onDelete(row: DocumentRow) {
  await ElMessageBox.confirm(
    `删除「${row.title}」后其切块与向量会被异步清理。确定删除？`,
    '删除文档',
    { type: 'warning' },
  )
  await knowledgeApi.remove(appId.value, row.id)
  ElMessage.success('已提交删除任务')
  await Promise.all([loadDocs(), loadJobs()])
}

// ---- 替换文件
const replaceTarget = ref<DocumentRow | null>(null)
const replaceFile = ref<File | null>(null)
const replacing = ref(false)

function onReplaceChange(file: UploadFile) {
  replaceFile.value = (file.raw as File | undefined) ?? null
}

async function onReplace() {
  if (!replaceTarget.value || !replaceFile.value) return
  replacing.value = true
  try {
    const result = await knowledgeApi.replaceFile(
      appId.value,
      replaceTarget.value.id,
      replaceFile.value,
    )
    ElMessage.success(result.unchanged ? '内容未变化，已跳过重建' : '已提交替换与重建')
    replaceTarget.value = null
    replaceFile.value = null
    await Promise.all([loadDocs(), loadJobs()])
  } finally {
    replacing.value = false
  }
}

// ---- 切块复核
async function approve(chunk: ChunkRow) {
  await knowledgeApi.patchChunk(appId.value, chunk.id, { review_status: 'approved' })
  ElMessage.success('已复核通过')
  await Promise.all([loadFlagged(), loadDocs()])
}

async function disable(chunk: ChunkRow) {
  await knowledgeApi.patchChunk(appId.value, chunk.id, { enabled: false })
  ElMessage.success('已禁用该切块')
  await loadFlagged()
}

function openDocument(row: { document_id?: string; id?: string }) {
  const documentId = row.document_id ?? row.id
  if (!documentId) return
  void router.push({ name: 'app-knowledge-document', params: { appId: appId.value, documentId } })
}

onMounted(async () => {
  await loadDocs()
  try {
    presets.value = await appApi.chunkPresets()
  } catch {
    // 预设只是便利项，取不到时继续用空文本（跟随应用配置）
  }
})
</script>

<template>
  <div class="knowledge">
    <div class="page-head">
      <div>
        <h2 class="aics-page-title">知识库</h2>
        <p class="aics-page-subtitle">
          上传文档后自动解析、切块并建立索引；命中测试与文档有效期在后续里程碑提供。
        </p>
      </div>
      <div class="head-actions">
        <el-tooltip content="批量重建索引将在 M4.3b 提供" placement="top">
          <span><el-button disabled>全量重建索引</el-button></span>
        </el-tooltip>
        <el-button v-if="canWrite" type="primary" @click="uploadVisible = true">上传文档</el-button>
      </div>
    </div>

    <div class="import-row">
      <div class="aics-card dropzone">
        <el-upload
          drag
          :auto-upload="false"
          :show-file-list="false"
          :disabled="!canWrite"
          accept=".pdf,.docx,.doc,.md,.markdown,.txt,.html,.htm"
          :on-change="onDropFile"
        >
          <el-icon class="drop-icon"><UploadFilled /></el-icon>
          <div class="drop-text">把文件拖到这里，或<em>点击选择</em></div>
          <div class="drop-hint">
            支持 PDF / Word / Markdown / 文本 / HTML；上传后自动切块并建立索引
          </div>
        </el-upload>
      </div>
      <div class="aics-card url-import">
        <h3>从链接导入</h3>
        <p class="hint">抓取网页正文入库，域名需在后端白名单内。</p>
        <el-input v-model="urlForm.url" placeholder="https://example.com/doc" />
        <el-input v-model="urlForm.title" placeholder="标题（可选）" />
        <el-button type="primary" :loading="importingUrl" :disabled="!canWrite" @click="onImportUrl"
          >导入</el-button
        >
      </div>
    </div>

    <div class="aics-card table-card">
      <el-tabs
        v-model="tab"
        @tab-change="tab === 'flagged' ? loadFlagged() : tab === 'jobs' ? loadJobs() : loadDocs()"
      >
        <el-tab-pane label="文档" name="documents">
          <div class="filters">
            <el-select
              v-model="docQuery.status"
              placeholder="全部状态"
              clearable
              style="width: 140px"
              @change="loadDocs"
            >
              <el-option label="排队中" value="queued" />
              <el-option label="处理中" value="processing" />
              <el-option label="已完成" value="done" />
              <el-option label="失败" value="failed" />
              <el-option label="删除中" value="deleting" />
            </el-select>
            <el-input
              v-model="docQuery.q"
              placeholder="按标题搜索"
              clearable
              style="width: 220px"
              @keyup.enter="loadDocs"
            />
            <el-button :loading="docsLoading" @click="loadDocs">刷新</el-button>
          </div>

          <el-empty v-if="!docsLoading && docs.length === 0" description="还没有文档，先上传一份" />

          <el-table v-else v-loading="docsLoading" :data="docs" row-key="id">
            <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="documentTagType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="切块" width="90">
              <template #default="{ row }">{{ row.chunk_count }}</template>
            </el-table-column>
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
            </el-table-column>
            <el-table-column prop="updated_at" label="更新时间" width="180" />
            <el-table-column label="操作" width="240" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openDocument(row)">切块</el-button>
                <el-button v-if="canWrite" link type="primary" @click="onReindex(row)"
                  >重建</el-button
                >
                <el-button v-if="canWrite" link type="primary" @click="replaceTarget = row"
                  >替换</el-button
                >
                <el-button v-if="canWrite" link type="danger" @click="onDelete(row)"
                  >删除</el-button
                >
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            v-if="docsTotal > docQuery.page_size"
            v-model:current-page="docQuery.page"
            :page-size="docQuery.page_size"
            :total="docsTotal"
            layout="prev, pager, next"
            class="pager"
            @current-change="loadDocs"
          />
        </el-tab-pane>

        <el-tab-pane label="待复核切块" name="flagged">
          <el-empty v-if="!flaggedLoading && flagged.length === 0" description="没有待复核的切块" />
          <el-table v-else v-loading="flaggedLoading" :data="flagged" row-key="id">
            <el-table-column
              prop="document_title"
              label="文档"
              min-width="180"
              show-overflow-tooltip
            />
            <el-table-column prop="ordinal" label="序号" width="80" />
            <el-table-column label="命中的疑问" width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.flag_reason || '-' }}</template>
            </el-table-column>
            <el-table-column label="正文" min-width="280">
              <template #default="{ row }">{{ excerpt(row.text) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button v-if="canWrite" link type="primary" @click="approve(row)"
                  >复核通过</el-button
                >
                <el-button v-if="canWrite" link type="danger" @click="disable(row)">禁用</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="flaggedTotal > 20"
            v-model:current-page="flaggedPage"
            :page-size="20"
            :total="flaggedTotal"
            layout="prev, pager, next"
            class="pager"
            @current-change="loadFlagged"
          />
        </el-tab-pane>

        <el-tab-pane label="任务" name="jobs">
          <div class="filters">
            <el-button :loading="jobsLoading" @click="loadJobs">刷新</el-button>
          </div>
          <el-empty v-if="!jobsLoading && jobs.length === 0" description="还没有任务" />
          <el-table v-else v-loading="jobsLoading" :data="jobs" row-key="id">
            <el-table-column prop="type" label="类型" width="140" />
            <el-table-column prop="status" label="状态" width="120" />
            <el-table-column label="进度" width="100">
              <template #default="{ row }">{{ row.progress ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.error || '-' }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="180" />
            <el-table-column prop="finished_at" label="结束时间" width="180" />
          </el-table>
          <el-pagination
            v-if="jobsTotal > 20"
            v-model:current-page="jobsPage"
            :page-size="20"
            :total="jobsTotal"
            layout="prev, pager, next"
            class="pager"
            @current-change="loadJobs"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog v-model="uploadVisible" title="上传文档" width="640px">
      <el-upload :auto-upload="false" :limit="1" :on-change="onFileChange" drag>
        <template #default>
          <div>把文件拖到这里，或点击选择</div>
        </template>
      </el-upload>
      <el-form label-width="90px" class="form">
        <el-form-item label="标题">
          <el-input v-model="uploadForm.title" placeholder="默认取文件名" />
        </el-form-item>
        <el-form-item label="切块预设">
          <el-select
            v-model="uploadForm.preset"
            placeholder="跟随应用配置"
            clearable
            style="width: 100%"
            @change="onPresetChange"
          >
            <el-option
              v-for="item in presets"
              :key="item.name"
              :label="item.label"
              :value="item.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="切块参数">
          <el-input
            v-model="uploadForm.configText"
            type="textarea"
            :rows="5"
            placeholder='留空跟随应用配置；否则填 JSON，如 {"chunk_size": 500}'
          />
        </el-form-item>
      </el-form>

      <el-alert v-if="previewResult" type="info" :closable="false" show-icon class="preview">
        <template #title>
          共 {{ previewResult.total_chunks }} 块 · {{ previewResult.total_tokens }} tokens
        </template>
        <div v-for="chunk in previewResult.chunks" :key="chunk.ordinal" class="preview-item">
          <span class="ordinal">#{{ chunk.ordinal }}</span>
          <span class="path">{{ chunk.heading_path || '（无标题）' }}</span>
          <div>{{ excerpt(chunk.text, 120) }}</div>
        </div>
      </el-alert>

      <template #footer>
        <el-button :loading="previewing" @click="onPreview">预览切块</el-button>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="onUpload">上传入库</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="replaceTarget !== null"
      title="替换文件"
      width="520px"
      @close="replaceTarget = null"
    >
      <p class="hint">替换后按正文哈希判断是否需要重建；当前文档：{{ replaceTarget?.title }}</p>
      <el-upload :auto-upload="false" :limit="1" :on-change="onReplaceChange">
        <el-button>选择新文件</el-button>
      </el-upload>
      <template #footer>
        <el-button @click="replaceTarget = null">取消</el-button>
        <el-button type="primary" :loading="replacing" @click="onReplace">替换并重建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.knowledge {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-bottom: 24px;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.page-head .aics-page-title {
  margin: 0;
}
.aics-page-subtitle {
  margin: 6px 0 0;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 4px;
}
.import-row {
  display: grid;
  grid-template-columns: minmax(0, 736fr) minmax(0, 420fr);
  gap: 16px;
}
.dropzone {
  padding: 0;
  overflow: hidden;
}
.dropzone :deep(.el-upload) {
  display: block;
}
.dropzone :deep(.el-upload-dragger) {
  border: none;
  border-radius: 8px;
  padding: 30px 20px;
  background: var(--aics-white);
}
.drop-icon {
  font-size: 30px;
  color: var(--aics-primary);
}
.drop-text {
  margin-top: 8px;
  font-size: 14px;
  color: var(--aics-text2);
}
.drop-text em {
  color: var(--aics-primary);
  font-style: normal;
}
.drop-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--aics-text3);
}
.url-import {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 20px;
}
.url-import h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--aics-text);
}
.url-import .hint {
  margin: 0;
  font-size: 12px;
  color: var(--aics-text3);
}
.url-import .el-button {
  align-self: flex-end;
}
.table-card {
  padding: 8px 20px 16px;
}
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
.form {
  margin-top: 12px;
}
.preview {
  margin-top: 8px;
}
.preview-item {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
}
.ordinal {
  color: var(--el-text-color-secondary);
  margin-right: 6px;
}
.path {
  color: var(--el-color-primary);
  margin-right: 6px;
}
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
}
</style>
