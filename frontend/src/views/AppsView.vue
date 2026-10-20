<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MoreFilled, Plus, Search, Service } from '@element-plus/icons-vue'
import { appApi, type AppCard } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const rows = ref<AppCard[]>([])
const loading = ref(false)
const createVisible = ref(false)
const creating = ref(false)
const form = reactive({ name: '', description: '' })
const query = ref('')
const status = ref<'all' | 'active' | 'disabled' | 'deleting'>('all')

const canWrite = () => auth.can('app.write')

const STATUS_LABEL: Record<string, string> = {
  active: '运行中',
  disabled: '已停用',
  deleting: '清理中',
}

const counts = computed(() => ({
  all: rows.value.length,
  active: rows.value.filter((row) => row.status === 'active').length,
  disabled: rows.value.filter((row) => row.status === 'disabled').length,
  deleting: rows.value.filter((row) => row.status === 'deleting').length,
}))

const filters = computed(() => [
  { key: 'all' as const, label: `全部 ${counts.value.all}` },
  { key: 'active' as const, label: `运行中 ${counts.value.active}` },
  { key: 'disabled' as const, label: `已停用 ${counts.value.disabled}` },
  { key: 'deleting' as const, label: `清理中 ${counts.value.deleting}` },
])

const visible = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (status.value !== 'all' && row.status !== status.value) return false
    if (!keyword) return true
    return (
      row.name.toLowerCase().includes(keyword) ||
      (row.description ?? '').toLowerCase().includes(keyword)
    )
  })
})

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function load() {
  loading.value = true
  try {
    rows.value = (await appApi.list()).items
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  creating.value = true
  try {
    const created = await appApi.create({ ...form })
    ElMessage.success('已创建')
    createVisible.value = false
    Object.assign(form, { name: '', description: '' })
    await router.push({ name: 'app-config', params: { appId: created.id } })
  } finally {
    creating.value = false
  }
}

async function onCopy(row: AppCard) {
  await ElMessageBox.confirm(`复制「${row.name}」的配置（不含文档、Key 与会话）？`, '复制应用')
  await appApi.copy(row.id)
  ElMessage.success('已复制')
  await load()
}

async function onDelete(row: AppCard) {
  await ElMessageBox.confirm(
    `删除后该应用的文档、会话与 Key 会被异步清理，接口立即不可用。确定删除「${row.name}」？`,
    '删除应用',
    { type: 'warning' },
  )
  await appApi.remove(row.id)
  ElMessage.success('已提交清理任务')
  await load()
}

async function onCommand(row: AppCard, command: string) {
  if (command === 'knowledge')
    await router.push({ name: 'app-knowledge', params: { appId: row.id } })
  if (command === 'copy') await onCopy(row)
  if (command === 'delete') await onDelete(row)
}

onMounted(load)
</script>

<template>
  <!-- 设计稿 02 画板：标题 + 副标题 + 搜索 + 新建 + 状态筛选 + 380×196 卡片 -->
  <section class="apps">
    <header class="hero">
      <div>
        <h1 class="aics-page-title">应用</h1>
        <p class="aics-page-subtitle">每个应用是一个独立的客服机器人，拥有自己的知识库与 API Key</p>
      </div>
      <div class="hero-tools">
        <el-input v-model="query" class="search" placeholder="搜索应用名称">
          <template #suffix
            ><el-icon><Search /></el-icon
          ></template>
        </el-input>
        <el-button v-if="canWrite()" type="primary" :icon="Plus" @click="createVisible = true">
          新建应用
        </el-button>
      </div>
    </header>

    <div class="filters">
      <button
        v-for="item in filters"
        :key="item.key"
        type="button"
        class="filter"
        :class="{ active: status === item.key }"
        @click="status = item.key"
      >
        {{ item.label }}
      </button>
    </div>

    <el-empty v-if="!loading && visible.length === 0" description="还没有可见的应用" />

    <div v-else class="grid" :class="{ loading }">
      <article v-for="row in visible" :key="row.id" class="app-card">
        <header class="app-head">
          <span class="app-icon"
            ><el-icon><Service /></el-icon
          ></span>
          <div class="app-title">
            <h2>{{ row.name }}</h2>
            <p>{{ row.description || '（无描述）' }}</p>
          </div>
          <span class="status-tag" :class="row.status">
            {{ STATUS_LABEL[row.status] ?? row.status }}
          </span>
        </header>

        <div class="divider" />

        <div class="stats">
          <div class="stat">
            <span class="stat-label">文档数</span>
            <span class="stat-value">{{ row.stats.documents }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">近 7 天对话</span>
            <el-tooltip content="会话统计在 M4.6 提供" placement="top">
              <span class="stat-value muted">—</span>
            </el-tooltip>
          </div>
          <div class="stat">
            <span class="stat-label">模型</span>
            <el-tooltip content="模型名随配置返回（M4.5 统一）" placement="top">
              <span class="stat-text">—</span>
            </el-tooltip>
          </div>
        </div>

        <footer class="app-foot">
          <span class="updated">更新于 {{ formatTime(row.updated_at) }}</span>
          <div class="app-actions">
            <el-tooltip content="调试对话在 M4.4 提供" placement="top">
              <span class="action disabled">调试</span>
            </el-tooltip>
            <el-button
              link
              type="primary"
              @click="router.push({ name: 'app-config', params: { appId: row.id } })"
            >
              进入
            </el-button>
            <el-dropdown trigger="click" @command="(cmd: string) => onCommand(row, cmd)">
              <span class="more"
                ><el-icon><MoreFilled /></el-icon
              ></span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="knowledge">知识库</el-dropdown-item>
                  <el-dropdown-item v-if="canWrite()" command="copy">复制</el-dropdown-item>
                  <el-dropdown-item v-if="canWrite()" command="delete" divided>
                    删除
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </footer>
      </article>
    </div>

    <el-dialog v-model="createVisible" title="新建应用" width="420px">
      <el-form label-width="72px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如：售后小助手" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.hero-tools {
  display: flex;
  align-items: center;
  gap: 12px;
}
.search {
  width: 240px;
}

.filters {
  display: flex;
  gap: 8px;
  margin: 24px 0 16px;
}
.filter {
  height: 22px;
  padding: 0 8px;
  border-radius: 4px;
  border: 1px solid var(--aics-border);
  background: #fafafa;
  color: var(--aics-text-2);
  font-size: 12px;
  line-height: 20px;
  cursor: pointer;
}
.filter.active {
  background: var(--aics-primary-bg);
  border-color: #91caff;
  color: #0958d9;
}

.grid {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.grid.loading {
  opacity: 0.6;
}

.app-card {
  width: 380px;
  height: 196px;
  box-sizing: border-box;
  padding: 20px;
  background: var(--aics-white);
  border-radius: var(--aics-radius);
  display: flex;
  flex-direction: column;
}

.app-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.app-icon {
  width: 40px;
  height: 40px;
  flex: 0 0 40px;
  border-radius: var(--aics-radius);
  background: var(--aics-primary-bg);
  color: var(--aics-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
}
.app-title {
  flex: 1;
  min-width: 0;
}
.app-title h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
  color: var(--aics-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.app-title p {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--aics-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.status-tag {
  flex: 0 0 auto;
  height: 22px;
  padding: 0 8px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 20px;
  border: 1px solid var(--aics-border);
  background: #fafafa;
  color: var(--aics-text-2);
}
.status-tag.active {
  background: #f6ffed;
  border-color: #b7eb8f;
  color: #389e0d;
}
.status-tag.deleting {
  background: #fff7e6;
  border-color: #ffd591;
  color: #d46b08;
}

.divider {
  height: 1px;
  margin: 16px 0 12px;
  background: var(--aics-split);
}

.stats {
  display: flex;
  gap: 60px;
  flex: 1;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-label {
  font-size: 12px;
  color: var(--aics-text-3);
}
.stat-value {
  font-size: 20px;
  font-weight: 500;
  color: var(--aics-text);
  line-height: 1.2;
}
.stat-value.muted {
  color: var(--aics-text-3);
}
.stat-text {
  font-size: 13px;
  color: var(--aics-text-2);
}

.app-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.updated {
  font-size: 12px;
  color: var(--aics-text-3);
}
.app-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.action {
  font-size: 14px;
  color: var(--aics-primary);
  cursor: pointer;
}
.action.disabled {
  color: var(--aics-text-3);
  cursor: not-allowed;
}
.more {
  display: inline-flex;
  color: var(--aics-text-3);
  cursor: pointer;
}
</style>
