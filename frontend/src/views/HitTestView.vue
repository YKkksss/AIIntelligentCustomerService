<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  knowledgeApi,
  type HitTestRanked,
  type HitTestResult,
  type HitTestUnit,
} from '@/api/knowledge'

const route = useRoute()
const router = useRouter()
const appId = computed(() => String(route.params.appId))

const form = reactive({ query: '', tags: '', overrides: '' })
const loading = ref(false)
const result = ref<HitTestResult | null>(null)

type GroupRow = HitTestUnit | HitTestRanked

interface Group {
  key: string
  label: string
  rows: GroupRow[]
}

const groups = computed<Group[]>(() => {
  const body = result.value
  if (!body) return []
  return [
    { key: 'fused', label: '融合结果', rows: body.fused },
    { key: 'reranked', label: '重排结果', rows: body.reranked },
    { key: 'vector', label: '向量召回', rows: body.vector },
    { key: 'keyword', label: '关键词召回', rows: body.keyword },
  ]
})

function keyOf(row: GroupRow): string {
  return `${row.chunk_id}-${row.rank}`
}

function parseOverrides(): Record<string, unknown> | undefined {
  const raw = form.overrides.trim()
  if (!raw) return undefined
  const parsed = JSON.parse(raw) as Record<string, unknown>
  return { rag_config: parsed }
}

async function run() {
  const query = form.query.trim()
  if (!query) {
    ElMessage.warning('请输入查询语句')
    return
  }
  let overrides: Record<string, unknown> | undefined
  try {
    overrides = parseOverrides()
  } catch {
    ElMessage.error('覆盖参数不是合法 JSON')
    return
  }
  const tags = form.tags
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)
  loading.value = true
  try {
    result.value = await knowledgeApi.hitTest(appId.value, {
      query,
      overrides,
      filters: tags.length > 0 ? { tags } : undefined,
    })
    ElMessage.success(
      `向量 ${result.value.vector.length} · 关键词 ${result.value.keyword.length} · 融合 ${result.value.fused.length} · 重排 ${result.value.reranked.length}`,
    )
  } finally {
    loading.value = false
  }
}

function isUnit(row: GroupRow): row is HitTestUnit {
  return typeof row === 'object' && row !== null && 'text' in row
}
</script>

<template>
  <div class="hit-test">
    <div class="toolbar">
      <el-button @click="router.push({ name: 'app-knowledge', params: { appId } })"
        >返回知识库</el-button
      >
      <h3 class="title">命中测试</h3>
      <span class="hint">只跑检索 → 融合 → 重排，不调模型、不写库</span>
    </div>

    <el-form label-width="88px" class="form">
      <el-form-item label="查询">
        <el-input v-model="form.query" placeholder="例如：退款多久到账？" @keyup.enter="run" />
      </el-form-item>
      <el-form-item label="标签过滤">
        <el-input v-model="form.tags" placeholder="逗号分隔，可留空" />
      </el-form-item>
      <el-form-item label="覆盖参数">
        <el-input
          v-model="form.overrides"
          type="textarea"
          :rows="3"
          placeholder='可选，JSON：{"vector_top_k": 5, "enable_rerank": true}'
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="loading" @click="run">运行</el-button>
      </el-form-item>
    </el-form>

    <template v-if="result">
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="重排启用">
          {{ result.rerank_applied ? '是' : '否' }}
        </el-descriptions-item>
        <el-descriptions-item label="检索模式">
          {{ result.rag_config.retrieval_mode ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="融合方式">
          {{ result.rag_config.fusion ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="标签">
          {{ result.tags.length > 0 ? result.tags.join('、') : '-' }}
        </el-descriptions-item>
      </el-descriptions>

      <el-table
        v-for="group in groups"
        :key="group.key"
        :data="group.rows"
        :row-key="keyOf"
        size="small"
      >
        <template #header>
          <span class="group-title">{{ group.label }}（{{ group.rows.length }}）</span>
        </template>
        <el-table-column label="#" width="60">
          <template #default="{ row }">{{ row.rank }}</template>
        </el-table-column>
        <el-table-column label="命中" min-width="420">
          <template #default="{ row }">
            <template v-if="isUnit(row)">
              <div class="unit-title">
                {{ row.document_title }}
                <span v-if="row.heading_path" class="path">/ {{ row.heading_path }}</span>
              </div>
              <div class="unit-text">{{ row.text }}</div>
            </template>
            <span v-else>{{ row.chunk_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="分数" width="120">
          <template #default="{ row }">
            {{
              typeof row.score === 'number'
                ? row.score.toFixed(4)
                : typeof row.vector_score === 'number'
                  ? row.vector_score.toFixed(4)
                  : '-'
            }}
          </template>
        </el-table-column>
        <el-table-column label="分路" width="220">
          <template #default="{ row }">
            <template v-if="isUnit(row)">
              <el-tag size="small" type="info">v {{ row.vector_score?.toFixed(3) ?? '-' }}</el-tag>
              <el-tag size="small" type="info">k {{ row.keyword_score?.toFixed(3) ?? '-' }}</el-tag>
              <el-tag size="small" type="success"
                >f {{ row.fused_score?.toFixed(3) ?? '-' }}</el-tag
              >
            </template>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.hit-test {
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
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.form {
  max-width: 880px;
}
.group-title {
  font-weight: 600;
}
.unit-title {
  font-weight: 500;
}
.path {
  color: var(--el-text-color-secondary);
  font-weight: 400;
  font-size: 12px;
}
.unit-text {
  font-size: 12px;
  color: var(--el-text-color-regular);
  margin-top: 2px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
