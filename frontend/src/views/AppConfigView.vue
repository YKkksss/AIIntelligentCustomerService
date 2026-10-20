<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { appApi, type AppDetail, type ChunkPreset } from '@/api/admin'
import { changedFields } from '@/utils/diff'
import { useAuthStore } from '@/stores/auth'

interface Field {
  path: string
  label: string
  kind: 'text' | 'textarea' | 'number' | 'switch' | 'list'
}

interface Group {
  key: string
  title: string
  fields: Field[]
}

const BASIC_FIELDS: Field[] = [
  { path: 'name', label: '名称', kind: 'text' },
  { path: 'description', label: '描述', kind: 'textarea' },
  { path: 'greeting', label: '欢迎语', kind: 'textarea' },
  { path: 'system_prompt', label: '系统提示词', kind: 'textarea' },
  { path: 'answer_style', label: '回答风格', kind: 'text' },
  { path: 'fallback_text', label: '兜底话术', kind: 'textarea' },
]

const CONFIG_GROUPS: { key: string; title: string }[] = [
  { key: 'rag_config', title: '检索与意图' },
  { key: 'session_config', title: '会话' },
  { key: 'handoff_config', title: '转人工' },
  { key: 'chunk_config', title: '切块' },
]

/** 把配置对象摊平成一层的字段列表（数组当叶子处理）。 */
function flatten(value: unknown, prefix = ''): Field[] {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return []
  const fields: Field[] = []
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof child === 'object' && child !== null && !Array.isArray(child)) {
      fields.push(...flatten(child, path))
      continue
    }
    fields.push({ path, label: path, kind: kindOf(child) })
  }
  return fields
}

function kindOf(value: unknown): Field['kind'] {
  if (Array.isArray(value)) return 'list'
  if (typeof value === 'boolean') return 'switch'
  if (typeof value === 'number') return 'number'
  if (typeof value === 'string' && (value.length > 60 || value.includes('\n'))) return 'textarea'
  return 'text'
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function readPath(source: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (typeof acc !== 'object' || acc === null) return undefined
    return (acc as Record<string, unknown>)[part]
  }, source)
}

function writePath(target: Record<string, unknown>, path: string, value: unknown) {
  const parts = path.split('.')
  let cursor: Record<string, unknown> = target
  for (const part of parts.slice(0, -1)) {
    const next = cursor[part]
    if (typeof next !== 'object' || next === null) cursor[part] = {}
    cursor = cursor[part] as Record<string, unknown>
  }
  cursor[parts[parts.length - 1]] = value
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const appId = String(route.params.appId)
const detail = ref<AppDetail | null>(null)
const form = reactive<Record<string, unknown>>({})
const snapshot = ref<Record<string, unknown>>({})
const presets = ref<ChunkPreset[]>([])
const loading = ref(false)
const saving = ref(false)
const errors = ref<string[]>([])
const canWrite = computed(() => auth.can('app.write'))

const groups = computed<Group[]>(() => {
  const out: Group[] = [{ key: 'basic', title: '基础信息', fields: BASIC_FIELDS }]
  for (const group of CONFIG_GROUPS) {
    out.push({
      key: group.key,
      title: group.title,
      fields: flatten(form[group.key] ?? {}).map((field) => ({
        ...field,
        path: `${group.key}.${field.path}`,
      })),
    })
  }
  out.push({
    key: 'webhook_config',
    title: 'Webhook',
    fields: [{ path: 'webhook_config.url', label: '接收地址', kind: 'text' }],
  })
  out.push({
    key: 'custom_dict',
    title: '自定义词典',
    fields: [{ path: 'custom_dict', label: '词条', kind: 'list' }],
  })
  return out
})

function valueOf(path: string): unknown {
  return readPath(form, path)
}

function setValue(path: string, value: unknown) {
  writePath(form, path, value)
}

function asList(path: string): string[] {
  const value = valueOf(path)
  return Array.isArray(value) ? (value as string[]) : []
}

function asNumber(path: string): number {
  const value = valueOf(path)
  return typeof value === 'number' ? value : 0
}

function asText(path: string): string {
  const value = valueOf(path)
  return value === null || value === undefined ? '' : String(value)
}

function asBool(path: string): boolean {
  return valueOf(path) === true
}

async function load() {
  loading.value = true
  try {
    const [body, chunkPresets] = await Promise.all([appApi.detail(appId), appApi.chunkPresets()])
    detail.value = body
    presets.value = chunkPresets
    for (const key of Object.keys(form)) delete form[key]
    const next: Record<string, unknown> = {}
    for (const field of BASIC_FIELDS) next[field.path] = clone(body[field.path as keyof AppDetail])
    for (const group of CONFIG_GROUPS) next[group.key] = clone(body[group.key as keyof AppDetail])
    next.webhook_config = { url: (body.webhook_config.url as string | null) ?? '' }
    next.custom_dict = clone(body.custom_dict)
    Object.assign(form, next)
    snapshot.value = clone(next)
  } finally {
    loading.value = false
  }
}

function applyPreset(name: string) {
  const preset = presets.value.find((item) => item.name === name)
  if (!preset) return
  writePath(form, 'chunk_config', clone(preset.config))
  ElMessage.info(`已套用预设「${preset.label}」，保存后生效`)
}

async function save() {
  const patch = changedFields(snapshot.value, form)
  if (Object.keys(patch).length === 0) {
    ElMessage.info('没有改动')
    return
  }
  errors.value = []
  saving.value = true
  try {
    await appApi.patch(appId, patch, detail.value?.config_version)
    ElMessage.success('已保存')
    await load()
  } catch (error) {
    const body = (
      error as { response?: { data?: { error?: { details?: { errors?: unknown[] } } } } }
    ).response?.data?.error
    const list = body?.details?.errors
    if (Array.isArray(list)) {
      errors.value = list.map((item) => {
        const err = item as { loc?: unknown[]; msg?: string }
        return `${(err.loc ?? []).join('.')}：${err.msg ?? '校验失败'}`
      })
    }
    throw error
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-config">
    <div class="toolbar">
      <el-button @click="router.push({ name: 'apps' })">返回</el-button>
      <span class="title">{{ detail?.name ?? appId }}</span>
      <el-tag v-if="detail" size="small">v{{ detail.config_version }}</el-tag>
      <el-button v-if="canWrite" type="primary" :loading="saving" @click="save">保存</el-button>
    </div>

    <el-alert
      v-if="!canWrite"
      type="info"
      :closable="false"
      title="只读：当前账号没有 app.write 权限"
    />
    <el-alert v-if="errors.length > 0" type="error" :closable="false" title="保存被拒绝">
      <p v-for="item in errors" :key="item">{{ item }}</p>
    </el-alert>

    <el-tabs>
      <el-tab-pane v-for="group in groups" :key="group.key" :label="group.title">
        <el-form label-width="180px" :disabled="!canWrite">
          <el-form-item v-if="group.key === 'chunk_config'" label="套用预设">
            <el-select placeholder="选择预设后整组替换" @change="applyPreset">
              <el-option
                v-for="preset in presets"
                :key="preset.name"
                :label="preset.label"
                :value="preset.name"
              />
            </el-select>
          </el-form-item>

          <el-form-item v-for="field in group.fields" :key="field.path" :label="field.label">
            <el-input-number
              v-if="field.kind === 'number'"
              :model-value="asNumber(field.path)"
              @update:model-value="(value: number | undefined) => setValue(field.path, value ?? 0)"
            />
            <el-switch
              v-else-if="field.kind === 'switch'"
              :model-value="asBool(field.path)"
              @update:model-value="
                (value: boolean | string | number) => setValue(field.path, Boolean(value))
              "
            />
            <el-select
              v-else-if="field.kind === 'list'"
              :model-value="asList(field.path)"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入后回车新增"
              @update:model-value="(value: string[]) => setValue(field.path, value)"
            />
            <el-input
              v-else-if="field.kind === 'textarea'"
              :model-value="asText(field.path)"
              type="textarea"
              :rows="3"
              @update:model-value="(value: string) => setValue(field.path, value)"
            />
            <el-input
              v-else
              :model-value="asText(field.path)"
              @update:model-value="(value: string) => setValue(field.path, value)"
            />
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.title {
  font-weight: 600;
}
.el-alert {
  margin-bottom: 12px;
}
</style>
