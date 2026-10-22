<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatDotRound,
  Collection,
  MoreFilled,
  Plus,
  Search,
  Service,
  Setting,
} from '@element-plus/icons-vue'
import { appApi, type AppCard } from '@/api/admin'
import AssistantAvatar from '@/components/AssistantAvatar.vue'
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

const STYLE_LABEL: Record<string, string> = {
  concise: '简洁',
  detailed: '详细',
  auto: '自动',
}

const LANGUAGE_LABEL: Record<string, string> = {
  auto: '自动识别',
  zh: '中文',
  en: '英文',
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

const totalDocs = computed(() =>
  rows.value.reduce((sum, row) => sum + (row.stats?.documents ?? 0), 0),
)

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function formatDay(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getMonth() + 1}-${pad(date.getDate())}`
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
    await router.push({ name: 'app-overview', params: { appId: created.id } })
  } finally {
    creating.value = false
  }
}

async function onCopy(row: AppCard) {
  await ElMessageBox.confirm(`复制「${row.name}」的配置（不含文档、Key 与会话）？`, '复制智能客服')
  await appApi.copy(row.id)
  ElMessage.success('已复制')
  await load()
}

function openConfig(row: AppCard) {
  return router.push({ name: 'app-config', params: { appId: row.id } })
}

async function onCommand(row: AppCard, command: string) {
  if (command === 'config') await openConfig(row)
  if (command === 'knowledge')
    await router.push({ name: 'app-knowledge', params: { appId: row.id } })
  if (command === 'debug') await router.push({ name: 'app-debug', params: { appId: row.id } })
  if (command === 'copy') await onCopy(row)
}

onMounted(load)
</script>

<template>
  <section class="apps">
    <header class="hero">
      <div class="hero-text">
        <h1 class="aics-page-title">智能客服</h1>
        <p class="aics-page-subtitle">
          每个智能客服都是一套独立的问答能力：自己的知识库、提示词、检索参数与接入渠道。
        </p>
        <p v-if="rows.length" class="hero-count">
          {{ rows.length }} 个智能客服 · {{ counts.active }} 个运行中 · 共
          {{ totalDocs }} 篇知识文档
        </p>
      </div>
      <div class="hero-tools">
        <el-input v-model="query" class="search" placeholder="搜索名称或描述" clearable>
          <template #suffix
            ><el-icon><Search /></el-icon
          ></template>
        </el-input>
        <el-button v-if="canWrite()" type="primary" :icon="Plus" @click="createVisible = true">
          新建智能客服
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

    <div v-loading="loading" class="board">
      <div v-if="!loading && visible.length === 0" class="empty">
        <span class="empty-icon"
          ><el-icon><Service /></el-icon
        ></span>
        <h3>{{ query || status !== 'all' ? '没有匹配的智能客服' : '还没有智能客服' }}</h3>
        <p>
          {{
            query || status !== 'all'
              ? '换个关键词或清空筛选再试试。'
              : '创建一个智能客服，上传知识文档后即可接入对话接口。'
          }}
        </p>
        <el-button
          v-if="canWrite() && !query && status === 'all'"
          type="primary"
          :icon="Plus"
          @click="createVisible = true"
        >
          新建智能客服
        </el-button>
      </div>

      <div v-else class="grid">
        <article v-for="row in visible" :key="row.id" class="app-card" @click="openConfig(row)">
          <header class="app-head">
            <AssistantAvatar
              :avatar="row.avatar"
              :stable-seed="row.id"
              :name="row.name"
              :size="44"
              :radius="12"
            />
            <div class="app-title">
              <div class="app-name-row">
                <h2 :title="row.name">{{ row.name }}</h2>
                <span class="status-tag" :class="row.status">
                  <i class="dot" />{{ STATUS_LABEL[row.status] ?? row.status }}
                </span>
              </div>
              <p :title="row.description || ''">
                {{ row.description || '还没有描述，进入配置补充一句介绍吧。' }}
              </p>
            </div>
            <el-dropdown trigger="click" @command="(cmd: string) => onCommand(row, cmd)">
              <span class="more" @click.stop
                ><el-icon><MoreFilled /></el-icon
              ></span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="config">配置</el-dropdown-item>
                  <el-dropdown-item command="knowledge">知识库</el-dropdown-item>
                  <el-dropdown-item v-if="auth.can('chat.debug')" command="debug">
                    调试对话
                  </el-dropdown-item>
                  <el-dropdown-item v-if="canWrite()" command="copy" divided>复制</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </header>

          <div class="chips">
            <span class="chip">配置 v{{ row.config_version }}</span>
            <span class="chip">{{ STYLE_LABEL[row.answer_style] ?? row.answer_style }}回答</span>
            <span class="chip">{{ LANGUAGE_LABEL[row.language] ?? row.language }}</span>
          </div>

          <div class="stats">
            <div class="stat">
              <span class="stat-value">{{ row.stats?.documents ?? 0 }}</span>
              <span class="stat-label">知识文档</span>
            </div>
            <div class="stat">
              <span class="stat-value">v{{ row.config_version }}</span>
              <span class="stat-label">配置版本</span>
            </div>
            <div class="stat">
              <span class="stat-value">{{ formatDay(row.updated_at) }}</span>
              <span class="stat-label">最近更新</span>
            </div>
          </div>

          <footer class="app-foot">
            <span class="updated" :title="formatTime(row.updated_at)">
              更新于 {{ formatTime(row.updated_at) }}
            </span>
            <div class="app-actions">
              <el-tooltip content="知识库" placement="top">
                <button
                  type="button"
                  class="icon-action"
                  @click.stop="router.push({ name: 'app-knowledge', params: { appId: row.id } })"
                >
                  <el-icon><Collection /></el-icon>
                </button>
              </el-tooltip>
              <el-tooltip v-if="auth.can('chat.debug')" content="调试对话" placement="top">
                <button
                  type="button"
                  class="icon-action"
                  @click.stop="router.push({ name: 'app-debug', params: { appId: row.id } })"
                >
                  <el-icon><ChatDotRound /></el-icon>
                </button>
              </el-tooltip>
              <el-button
                type="primary"
                size="small"
                :icon="Setting"
                @click.stop="onCommand(row, 'config')"
              >
                配置
              </el-button>
            </div>
          </footer>
        </article>
      </div>
    </div>

    <el-dialog v-model="createVisible" title="新建智能客服" width="460px">
      <p class="dialog-hint">
        创建后可以继续上传知识文档、调整提示词与接入渠道；每次保存都会生成新的配置版本，可随时回滚。
      </p>
      <el-form label-width="72px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如：售后小助手" maxlength="100" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="一句话说明它负责什么，方便同事分辨"
          />
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
  gap: 24px;
}
.hero-text {
  min-width: 0;
}
.hero-count {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--aics-text-3);
}
.hero-tools {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 0 0 auto;
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
  height: 24px;
  padding: 0 10px;
  border-radius: 4px;
  border: 1px solid var(--aics-border);
  background: #fafafa;
  color: var(--aics-text-2);
  font-size: 12px;
  line-height: 22px;
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    border-color 0.15s ease,
    color 0.15s ease;
}
.filter:hover {
  border-color: #91caff;
  color: var(--aics-primary);
}
.filter.active {
  background: var(--aics-primary-bg);
  border-color: #91caff;
  color: #0958d9;
}

.board {
  min-height: 240px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 20px;
}

.app-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  background: var(--aics-white);
  border: 1px solid var(--aics-split);
  border-radius: 12px;
  cursor: pointer;
  transition:
    box-shadow 0.2s ease,
    transform 0.2s ease,
    border-color 0.2s ease;
}
.app-card:hover {
  border-color: #d6e4ff;
  box-shadow: 0 10px 24px rgb(15 23 42 / 8%);
  transform: translateY(-2px);
}

.app-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.app-avatar {
  width: 44px;
  height: 44px;
  flex: 0 0 44px;
  border-radius: 12px;
  background: linear-gradient(135deg, #e6f4ff 0%, #bae0ff 100%);
  color: var(--aics-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 600;
}
.app-title {
  flex: 1;
  min-width: 0;
}
.app-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.app-name-row h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--aics-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.app-title p {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--aics-text-3);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 36px;
}

.status-tag {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 12px;
  background: #fafafa;
  color: var(--aics-text-2);
}
.status-tag .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentcolor;
}
.status-tag.active {
  background: #f6ffed;
  color: #389e0d;
}
.status-tag.deleting {
  background: #fff7e6;
  color: #d46b08;
}

.more {
  display: inline-flex;
  padding: 4px;
  border-radius: 6px;
  color: var(--aics-text-3);
  cursor: pointer;
}
.more:hover {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  height: 22px;
  padding: 0 8px;
  border-radius: 11px;
  background: #f5f7fa;
  color: var(--aics-text-2);
  font-size: 12px;
  line-height: 22px;
  white-space: nowrap;
}

.stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: #fafcff;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.stat-value {
  font-size: 18px;
  font-weight: 600;
  color: var(--aics-text);
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat-label {
  font-size: 12px;
  color: var(--aics-text-3);
}

.app-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.updated {
  font-size: 12px;
  color: var(--aics-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.app-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}
.icon-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--aics-border);
  border-radius: 6px;
  background: var(--aics-white);
  color: var(--aics-text-2);
  cursor: pointer;
  font-size: 14px;
}
.icon-action:hover {
  color: var(--aics-primary);
  border-color: #91caff;
  background: var(--aics-primary-bg);
}

.empty {
  padding: 64px 24px;
  text-align: center;
  background: var(--aics-white);
  border: 1px dashed var(--aics-border);
  border-radius: 12px;
}
.empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: var(--aics-primary-bg);
  color: var(--aics-primary);
  font-size: 26px;
}
.empty h3 {
  margin: 16px 0 6px;
  font-size: 16px;
  font-weight: 600;
  color: var(--aics-text);
}
.empty p {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--aics-text-3);
}

.dialog-hint {
  margin: 0 0 16px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--aics-primary-bg);
  color: var(--aics-text-2);
  font-size: 12px;
  line-height: 18px;
}
</style>
