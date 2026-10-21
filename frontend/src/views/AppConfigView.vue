<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { appApi, type AppDetail, type ChunkPreset } from '@/api/admin'
import AssistantAvatar from '@/components/AssistantAvatar.vue'
import {
  AVATAR_STYLES,
  STYLE_LABELS,
  normalizeAvatar,
  randomSeed,
  type AvatarInput,
  type AvatarStyle,
} from '@/utils/avatar'
import { changedFields } from '@/utils/diff'
import { useAuthStore } from '@/stores/auth'

interface Field {
  path: string
  label: string
  kind: 'text' | 'textarea' | 'number' | 'switch' | 'list'
}

interface GroupSpec {
  key: string
  title: string
  /** 取哪个配置对象里的字段。 */
  source: string
  /** `intent` 只取 `intent.*`，`plain` 只取其余顶层字段。 */
  only?: 'intent' | 'plain'
}

const BASIC_FIELDS: Field[] = [
  { path: 'name', label: '名称', kind: 'text' },
  { path: 'description', label: '描述', kind: 'textarea' },
  { path: 'greeting', label: '欢迎语', kind: 'textarea' },
  { path: 'system_prompt', label: '系统提示词', kind: 'textarea' },
  { path: 'answer_style', label: '回答风格', kind: 'text' },
  { path: 'fallback_text', label: '兜底话术', kind: 'textarea' },
]

const CONFIG_GROUPS: GroupSpec[] = [
  { key: 'rag_config', title: '检索参数', source: 'rag_config', only: 'plain' },
  { key: 'rag_intent', title: '意图识别', source: 'rag_config', only: 'intent' },
  { key: 'chunk_config', title: '切块参数', source: 'chunk_config' },
  { key: 'handoff_config', title: '转人工规则', source: 'handoff_config' },
  { key: 'session_config', title: '会话', source: 'session_config' },
]

/** 设计稿与配置文档里的中文名；未登记的字段回落到键路径。 */
const LABELS: Record<string, string> = {
  llm_model: '对话模型',
  summary_model: '摘要模型',
  temperature: '温度',
  max_tokens: '最大输出 tokens',
  retrieval_mode: '检索模式',
  fusion: '融合方式',
  vector_weight: '向量权重',
  vector_top_k: '向量召回数',
  keyword_top_k: '关键词召回数',
  fuse_top_k: '融合候选数',
  enable_rerank: '启用重排',
  rerank_top_n: '重排保留数',
  rerank_threshold: '重排阈值',
  vector_score_threshold: '向量分数下限',
  judge_with_llm: 'LLM 判定可答性',
  judge_gray_upper: '灰区上界',
  default_tags: '默认标签',
  enable_facts: '启用事实记忆',
  llm_thinking: '深度思考',
  keep_recent_turns: '保留最近轮数',
  history_token_budget: '历史 token 预算',
  compact_trigger_tokens: '压缩触发 tokens',
  enable_history_recall: '历史回查',
  openai_temp_summary: '临时摘要',
  'intent.enabled': '启用意图识别',
  'intent.min_confidence': '最低置信度',
  'intent.classify_timeout_ms': '分类超时（毫秒）',
  'intent.vector_fast_path': '向量快速通道',
  'intent.vector_threshold': '向量快速阈值',
  'intent.out_of_scope_check': '越界检查',
  'intent.out_of_scope_override_score': '越界覆盖分',
  'intent.clarify_threshold': '澄清阈值',
  'intent.chitchat_max_chars': '闲聊最大字数',
  'intent.enabled_intents': '启用意图',
  'intent.sensitive_keywords': '敏感词',
  'intent.chitchat_keywords': '闲聊关键词',
  'intent.chitchat_replies': '闲聊话术',
  'intent.sensitive_reply': '敏感话术',
  'intent.out_of_scope_reply': '越界话术',
  'intent.action_reply': '动作话术',
  mode: '切块模式',
  preset: '预设',
  strategy: '切分策略',
  chunk_size: '父块大小',
  chunk_overlap: '父块重叠',
  child_size: '子块大小',
  child_overlap: '子块重叠',
  parent_mode: '父块模式',
  parent_size: '父块上限',
  split_by_heading: '按标题切分',
  contextual_prefix: '上下文前缀',
  keep_tables: '保留表格',
  normalize_whitespace: '规范化空白',
  remove_urls_emails: '移除链接与邮箱',
  full_doc_max_tokens: '全文最大 tokens',
  separators: '父块分隔符',
  child_separators: '子块分隔符',
  enabled: '启用转人工',
  keywords: '触发关键词',
  max_refusals: '连续拒答次数',
  on_unanswerable: '答不了即转人工',
  handoff_ai_mode: '转接后 AI 模式',
  text: '转接提示',
  wait_text: '排队提示',
  accepted_text: '坐席接入提示',
  released_text: '交还提示',
  offline_text: '非服务时间提示',
  queue_timeout_text: '排队超时提示',
  service_hours: '服务时间',
  handoff_queue_timeout_minutes: '排队超时（分钟）',
  handoff_accepted_idle_minutes: '坐席静默超时（分钟）',
  idle_close_minutes: '空闲关闭（分钟）',
  idle_notice_before_minutes: '关闭前提醒（分钟）',
  idle_close_notice: '空闲关闭提示',
}

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
    fields.push({ path, label: LABELS[path] ?? path, kind: kindOf(child) })
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

/** 头像不是配置字典的一部分：整体对象提交，只有真改了才进 patch。 */
const avatarStyle = computed<AvatarStyle>({
  get: () => normalizeAvatar(form.avatar as AvatarInput, appId).style,
  set: (style) => {
    const current = normalizeAvatar(form.avatar as AvatarInput, appId)
    form.avatar = { ...current, style }
  },
})

function rerollAvatar() {
  const current = normalizeAvatar(form.avatar as AvatarInput, appId)
  form.avatar = { ...current, seed: randomSeed() }
}

const parsedGroups = computed<{ key: string; title: string; fields: Field[] }[]>(() => {
  const out: { key: string; title: string; fields: Field[] }[] = [
    { key: 'basic', title: '基础信息', fields: BASIC_FIELDS },
  ]
  for (const group of CONFIG_GROUPS) {
    let fields = flatten(form[group.source] ?? {})
    if (group.only === 'intent') fields = fields.filter((field) => field.path.startsWith('intent.'))
    if (group.only === 'plain') fields = fields.filter((field) => !field.path.startsWith('intent.'))
    out.push({
      key: group.key,
      title: group.title,
      fields: fields.map((field) => ({ ...field, path: `${group.source}.${field.path}` })),
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
    for (const group of CONFIG_GROUPS) {
      next[group.source] = clone(body[group.source as keyof AppDetail])
    }
    next.webhook_config = { url: (body.webhook_config.url as string | null) ?? '' }
    next.custom_dict = clone(body.custom_dict)
    next.avatar = normalizeAvatar(body.avatar, body.id)
    Object.assign(form, next)
    snapshot.value = clone(next)
  } finally {
    loading.value = false
  }
}

/** 丢弃未保存的改动，回到最近一次加载的状态。 */
function resetForm() {
  for (const key of Object.keys(form)) delete form[key]
  Object.assign(form, clone(snapshot.value))
  errors.value = []
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
    <div class="page-head">
      <div>
        <div class="title-row">
          <h2 class="aics-page-title">智能客服配置</h2>
          <el-tag v-if="detail" size="small" effect="plain">v{{ detail.config_version }}</el-tag>
        </div>
        <p class="aics-page-subtitle">
          保存后立即生效；每次保存 config_version 递增，保存时用版本号做乐观并发校验。
        </p>
      </div>
      <div class="head-actions">
        <el-tooltip content="版本历史将在后续里程碑提供" placement="top">
          <span><el-button disabled>版本历史</el-button></span>
        </el-tooltip>
        <el-button :disabled="!canWrite" @click="resetForm">重置</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canWrite" @click="save">
          保存并发布 v{{ (detail?.config_version ?? 0) + 1 }}
        </el-button>
      </div>
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

    <section class="group avatar-group">
      <header class="group-head">
        <h3>头像</h3>
        <span class="group-hint">只影响后台里的展示；不产生图片文件，换一个就是换一组画法参数</span>
      </header>
      <div class="avatar-body">
        <AssistantAvatar
          :avatar="form.avatar as AvatarInput"
          :name="detail?.name"
          :size="72"
          :radius="18"
        />
        <div class="avatar-controls">
          <el-select v-model="avatarStyle" :disabled="!canWrite" size="default">
            <el-option
              v-for="style in AVATAR_STYLES"
              :key="style"
              :label="STYLE_LABELS[style]"
              :value="style"
            />
          </el-select>
          <el-button :disabled="!canWrite" @click="rerollAvatar">换一个</el-button>
          <span class="avatar-seed">seed：{{ (form.avatar as AvatarInput)?.seed }}</span>
        </div>
      </div>
    </section>

    <div class="groups">
      <section v-for="group in parsedGroups" :key="group.key" class="group">
        <header class="group-head">
          <h3>{{ group.title }}</h3>
          <el-select
            v-if="group.key === 'chunk_config' && canWrite"
            class="preset"
            placeholder="套用预设"
            size="small"
            @change="applyPreset"
          >
            <el-option
              v-for="preset in presets"
              :key="preset.name"
              :label="preset.label"
              :value="preset.name"
            />
          </el-select>
        </header>

        <el-form label-width="132px" label-position="left" :disabled="!canWrite">
          <el-form-item v-for="field in group.fields" :key="field.path" :label="field.label">
            <el-input-number
              v-if="field.kind === 'number'"
              :model-value="asNumber(field.path)"
              controls-position="right"
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
              :rows="2"
              @update:model-value="(value: string) => setValue(field.path, value)"
            />
            <el-input
              v-else
              :model-value="asText(field.path)"
              @update:model-value="(value: string) => setValue(field.path, value)"
            />
          </el-form-item>
        </el-form>
      </section>
    </div>
  </div>
</template>

<style scoped>
.app-config {
  padding-bottom: 24px;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-row .aics-page-title {
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
.el-alert {
  margin-bottom: 12px;
}
.groups {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}
.group {
  background: var(--aics-white);
  border: 1px solid var(--aics-border);
  border-radius: 8px;
  padding: 16px 20px 4px;
}
.group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.group-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--aics-text);
}
.preset {
  width: 180px;
}
.avatar-group {
  padding-bottom: 16px;
}
.group-hint {
  font-size: 12px;
  color: var(--aics-text3);
}
.avatar-body {
  display: flex;
  align-items: center;
  gap: 20px;
  padding-bottom: 8px;
}
.avatar-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}
.avatar-seed {
  font-size: 12px;
  color: var(--aics-text3);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group :deep(.el-select__tags) {
  max-height: 72px;
  overflow-y: auto;
}
@media (max-width: 1200px) {
  .groups {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
