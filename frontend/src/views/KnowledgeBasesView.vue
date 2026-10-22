<script setup lang="ts">
/**
 * 公共知识库（M4.7 ⑤）：平台级资源，不再挂在某个智能客服下面。
 *
 * 这里只做库本身的运营：建库、改名 / 描述、停用、删除，以及查看库里的文档和被哪些客服在用。
 * 文档上传仍走智能客服页（`documents.app_id` 还没去掉，见 ISSUE-106）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Collection, Plus, Refresh, Search } from '@element-plus/icons-vue'

import { kbApi, type KBDocumentRow, type KnowledgeBaseRow } from '@/api/knowledge-bases'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rows = ref<KnowledgeBaseRow[]>([])
const total = ref(0)
const loading = ref(false)
const query = ref('')
const status = ref<'all' | 'active' | 'disabled'>('all')
const page = ref(1)
const pageSize = 20

const STATUS_LABEL: Record<string, string> = {
  active: '运行中',
  disabled: '已停用',
  deleting: '删除中',
}
const STATUS_TAG: Record<string, 'success' | 'info' | 'warning' | 'danger'> = {
  active: 'success',
  disabled: 'info',
  deleting: 'warning',
}

const canWrite = () => auth.can('kb.write')

const counts = computed(() => ({
  active: rows.value.filter((row) => row.status === 'active').length,
  disabled: rows.value.filter((row) => row.status === 'disabled').length,
}))

const visible = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (status.value !== 'all' && row.status !== status.value) return false
    if (!keyword) return true
    return (
      row.name.toLowerCase().includes(keyword) || row.description.toLowerCase().includes(keyword)
    )
  })
})

async function load() {
  loading.value = true
  try {
    const body = await kbApi.list({
      q: query.value.trim() || undefined,
      page: page.value,
      pageSize,
    })
    rows.value = body.items
    total.value = body.total
  } finally {
    loading.value = false
  }
}

/* ---------------------------------------------------------------- 新建 / 编辑 */
const formVisible = ref(false)
const editing = ref<KnowledgeBaseRow | null>(null)
const form = reactive({ name: '', description: '' })
const saving = ref(false)

function openCreate() {
  editing.value = null
  form.name = ''
  form.description = ''
  formVisible.value = true
}

function openEdit(row: KnowledgeBaseRow) {
  editing.value = row
  form.name = row.name
  form.description = row.description
  formVisible.value = true
}

async function save() {
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('给知识库起个名字')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await kbApi.update(editing.value.id, { name, description: form.description.trim() })
      ElMessage.success('已保存')
    } else {
      await kbApi.create({ name, description: form.description.trim() })
      ElMessage.success('已创建')
    }
    formVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(row: KnowledgeBaseRow) {
  await ElMessageBox.confirm(
    `删除「${row.name}」？库里还有文档或仍被智能客服关联时会被拒绝。`,
    '删除知识库',
    { type: 'warning' },
  )
  await kbApi.remove(row.id)
  ElMessage.success('已删除')
  if (current.value?.id === row.id) {
    drawerVisible.value = false
    current.value = null
  }
  await load()
}

async function toggle(row: KnowledgeBaseRow) {
  const next = row.status === 'active' ? 'disabled' : 'active'
  await kbApi.update(row.id, { status: next })
  ElMessage.success(next === 'active' ? '已启用' : '已停用')
  await load()
}

/* ---------------------------------------------------------------- 库内文档 */
const drawerVisible = ref(false)
const current = ref<KnowledgeBaseRow | null>(null)
const docs = ref<KBDocumentRow[]>([])
const docsTotal = ref(0)
const docsLoading = ref(false)

async function openDocs(row: KnowledgeBaseRow) {
  current.value = row
  drawerVisible.value = true
  docsLoading.value = true
  try {
    const body = await kbApi.documents(row.id, { pageSize: 100 })
    docs.value = body.items
    docsTotal.value = body.total
  } finally {
    docsLoading.value = false
  }
}

function formatTime(raw: string) {
  return raw ? raw.replace('T', ' ').slice(0, 16) : '—'
}

onMounted(load)
</script>

<template>
  <section class="kb-page">
    <header class="hero">
      <div class="hero-text">
        <h1 class="aics-page-title">知识库</h1>
        <p class="aics-page-subtitle">
          知识库是平台公共资源：一个库可以同时提供给多个智能客服，改一次内容，所有关联的客服一起生效。
        </p>
        <p v-if="rows.length" class="hero-count">
          共 {{ total }} 个知识库 · {{ counts.active }} 个运行中 · {{ counts.disabled }} 个已停用
        </p>
      </div>
      <div class="hero-tools">
        <el-input v-model="query" class="search" placeholder="搜索名称或描述" clearable>
          <template #prefix
            ><el-icon><Search /></el-icon
          ></template>
        </el-input>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button v-if="canWrite()" type="primary" :icon="Plus" @click="openCreate">
          新建知识库
        </el-button>
      </div>
    </header>

    <div class="filters">
      <button
        v-for="item in [
          { key: 'all', label: '全部', count: rows.length },
          { key: 'active', label: '运行中', count: counts.active },
          { key: 'disabled', label: '已停用', count: counts.disabled },
        ]"
        :key="item.key"
        type="button"
        class="chip"
        :class="{ active: status === item.key }"
        @click="status = item.key as typeof status"
      >
        {{ item.label }} {{ item.count }}
      </button>
    </div>

    <el-table v-loading="loading" :data="visible" class="kb-table" empty-text="还没有知识库">
      <el-table-column label="知识库" min-width="260">
        <template #default="{ row }">
          <div class="kb-cell">
            <span class="kb-icon"
              ><el-icon><Collection /></el-icon
            ></span>
            <div class="kb-meta">
              <div class="kb-name">{{ row.name }}</div>
              <div class="kb-desc">{{ row.description || '还没有描述' }}</div>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="STATUS_TAG[row.status] ?? 'info'" size="small" effect="light">
            {{ STATUS_LABEL[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="文档 / 切块" width="130">
        <template #default="{ row }">
          <span class="num">{{ row.document_count }}</span>
          <span class="num-sep">/</span>
          <span class="num muted">{{ row.chunk_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="关联智能客服" min-width="200">
        <template #default="{ row }">
          <span v-if="!row.apps.length" class="muted">未关联</span>
          <el-tag
            v-for="app in row.apps.slice(0, 3)"
            :key="app.app_id"
            class="app-chip"
            size="small"
            effect="plain"
          >
            {{ app.name }}
          </el-tag>
          <span v-if="row.apps.length > 3" class="muted">+{{ row.apps.length - 3 }}</span>
        </template>
      </el-table-column>
      <el-table-column label="更新于" width="150">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="230" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDocs(row)">文档</el-button>
          <template v-if="canWrite()">
            <el-button link @click="openEdit(row)">改名</el-button>
            <el-button link @click="toggle(row)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="formVisible"
      :title="editing ? '编辑知识库' : '新建知识库'"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form label-width="72px" label-position="left">
        <el-form-item label="名称" required>
          <el-input
            v-model="form.name"
            maxlength="100"
            show-word-limit
            placeholder="例如：售后政策库"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="这个库放什么内容、给哪些客服共用"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="drawerVisible" :title="current?.name ?? '库内文档'" size="720px">
      <template #header>
        <div class="drawer-head">
          <span class="drawer-title">{{ current?.name ?? '库内文档' }}</span>
          <el-button v-if="current" type="danger" plain size="small" @click="remove(current)">
            删除知识库
          </el-button>
        </div>
      </template>
      <p class="drawer-hint">
        共 {{ docsTotal }} 篇。文档仍属于上传它的智能客服（上传入口在客服页），这里只做查看。
      </p>
      <el-table v-loading="docsLoading" :data="docs" size="small" empty-text="这个库还没有文档">
        <el-table-column label="标题" min-width="200">
          <template #default="{ row }">
            <div class="doc-title">{{ row.title }}</div>
            <div class="doc-sub">来自 {{ row.app_name }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="切块" width="80" prop="chunk_count" />
        <el-table-column label="更新于" width="140">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </section>
</template>

<style scoped>
.kb-page {
  padding: 24px;
}
.hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}
.aics-page-subtitle {
  margin: 6px 0 0;
  max-width: 760px;
  color: var(--aics-text-2);
  font-size: 13px;
  line-height: 1.6;
}
.hero-count {
  margin: 8px 0 0;
  color: var(--aics-text-3);
  font-size: 12px;
}
.hero-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.search {
  width: 220px;
}
.filters {
  display: flex;
  gap: 8px;
  margin: 18px 0 12px;
}
.chip {
  height: 26px;
  padding: 0 12px;
  border: 1px solid var(--aics-border);
  border-radius: 13px;
  background: #fafafa;
  color: var(--aics-text-2);
  font-size: 12px;
  cursor: pointer;
}
.chip.active {
  border-color: #91caff;
  background: #e6f4ff;
  color: #0958d9;
}
.kb-table {
  border-radius: 12px;
  background: #fff;
}
.kb-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.kb-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: #e6f4ff;
  color: #1677ff;
  font-size: 17px;
}
.kb-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--aics-text);
}
.kb-desc {
  max-width: 420px;
  overflow: hidden;
  color: var(--aics-text-3);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.num {
  font-size: 14px;
  font-weight: 500;
}
.num-sep {
  margin: 0 4px;
  color: var(--aics-text-3);
}
.num.muted {
  color: var(--aics-text-3);
  font-weight: 400;
}
.muted {
  color: var(--aics-text-3);
  font-size: 12px;
}
.app-chip {
  margin-right: 4px;
}
.doc-title {
  font-size: 13px;
  color: var(--aics-text);
}
.doc-sub {
  color: var(--aics-text-3);
  font-size: 12px;
}
.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 8px;
}
.drawer-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--aics-text);
}
.drawer-hint {
  margin: 0 0 12px;
  color: var(--aics-text-3);
  font-size: 12px;
}
</style>
