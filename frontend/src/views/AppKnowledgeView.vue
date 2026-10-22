<script setup lang="ts">
/**
 * 智能客服的「知识库」页（M4.7）：只做**关联/解绑**。
 *
 * 知识库是平台级资源，建库、上传文档、改名这些都在左侧菜单的「知识库」公共页做；
 * 这里只展示当前客服挂了哪些库、哪个是默认库（旧接口 `/apps/{id}/documents` 的落点），
 * 以及多库时的绑定关系本身。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Link, Plus, Refresh, Setting } from '@element-plus/icons-vue'
import { kbApi, type KnowledgeBaseRow } from '@/api/knowledge-bases'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const appId = computed(() => String(route.params.appId ?? ''))
const canWrite = computed(() => auth.can('kb.write'))

const bound = ref<KnowledgeBaseRow[]>([])
const allKbs = ref<KnowledgeBaseRow[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const saving = ref(false)
const picked = ref<string[]>([])
const defaultKbId = ref<string | null>(null)

const boundIds = computed(() => new Set(bound.value.map((kb) => kb.id)))
const selectable = computed(() => allKbs.value.filter((kb) => kb.status !== 'deleting'))
const defaultName = computed(
  () => bound.value.find((kb) => kb.is_default)?.name ?? bound.value[0]?.name ?? '—',
)

async function load() {
  if (!appId.value) return
  loading.value = true
  try {
    bound.value = await kbApi.appLinks(appId.value)
    picked.value = bound.value.map((kb) => kb.id)
    defaultKbId.value = bound.value.find((kb) => kb.is_default)?.id ?? null
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

async function openDialog() {
  try {
    const page = await kbApi.list({ page: 1, pageSize: 100 })
    allKbs.value = page.items
  } catch {
    return
  }
  picked.value = bound.value.map((kb) => kb.id)
  defaultKbId.value = bound.value.find((kb) => kb.is_default)?.id ?? null
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  try {
    bound.value = await kbApi.setAppLinks(appId.value, {
      kb_ids: picked.value,
      default_kb_id: defaultKbId.value,
    })
    ElMessage.success('已更新关联的知识库')
    dialogVisible.value = false
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

async function unbind(kb: KnowledgeBaseRow) {
  await ElMessageBox.confirm(
    `解绑后「${kb.name}」里的文档不再参与这个智能客服的检索（库本身与其他客服不受影响）。`,
    '解绑知识库',
    { type: 'warning', confirmButtonText: '解绑', cancelButtonText: '取消' },
  )
  const rest = bound.value.filter((item) => item.id !== kb.id)
  bound.value = await kbApi.setAppLinks(appId.value, {
    kb_ids: rest.map((item) => item.id),
    default_kb_id: defaultKbId.value === kb.id ? (rest[0]?.id ?? null) : defaultKbId.value,
  })
  ElMessage.success('已解绑')
}

async function setDefault(kb: KnowledgeBaseRow) {
  bound.value = await kbApi.setAppLinks(appId.value, {
    kb_ids: bound.value.map((item) => item.id),
    default_kb_id: kb.id,
  })
  defaultKbId.value = kb.id
  ElMessage.success(`默认库已设为「${kb.name}」`)
}

function openKb(kb: KnowledgeBaseRow) {
  void router.push({ name: 'knowledge-bases', query: { kb: kb.id } })
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="binding">
    <header class="head">
      <div>
        <h2>关联知识库</h2>
        <p class="sub">
          知识库是平台级资源，在一个地方建库、传文档，多个智能客服可以共用；这里只决定
          <b>这个智能客服能用哪些库</b>。当前默认库：<b>{{ defaultName }}</b>
        </p>
      </div>
      <div class="actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button :icon="Link" @click="router.push({ name: 'knowledge-bases' })">
          去知识库管理
        </el-button>
        <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openDialog">
          关联知识库
        </el-button>
      </div>
    </header>

    <el-empty v-if="!loading && bound.length === 0" description="还没有关联任何知识库">
      <el-button v-if="canWrite" type="primary" @click="openDialog">关联第一个知识库</el-button>
    </el-empty>

    <div v-else class="grid">
      <article v-for="kb in bound" :key="kb.id" class="card">
        <div class="card-head">
          <div class="title">
            <span class="name">{{ kb.name }}</span>
            <el-tag v-if="kb.is_default" size="small" type="success" effect="plain">默认库</el-tag>
            <el-tag v-if="kb.status !== 'active'" size="small" type="info">
              {{ kb.status === 'disabled' ? '已停用' : '清理中' }}
            </el-tag>
          </div>
          <el-tag size="small" effect="plain">{{ kb.document_count }} 篇文档</el-tag>
        </div>
        <p class="desc">{{ kb.description || '暂无描述' }}</p>
        <div class="meta">
          <span>{{ kb.chunk_count }} 个切块</span>
          <span>配置 v{{ kb.config_version }}</span>
        </div>
        <div class="card-actions">
          <el-button link type="primary" @click="openKb(kb)">查看文档</el-button>
          <el-button
            v-if="canWrite && !kb.is_default"
            link
            type="primary"
            :icon="Setting"
            @click="setDefault(kb)"
          >
            设为默认
          </el-button>
          <el-button v-if="canWrite" link type="danger" @click="unbind(kb)">解绑</el-button>
        </div>
      </article>
    </div>

    <el-alert
      class="note"
      type="info"
      :closable="false"
      show-icon
      title="上传文档、切块编辑、改名都在「知识库」公共页完成；这里只管理绑定关系。"
    />

    <el-dialog v-model="dialogVisible" title="关联知识库" width="620px">
      <p class="dialog-hint">
        勾选这个智能客服要使用的知识库；默认库用于旧接口
        <code>/apps/{app_id}/documents</code> 的落点。
      </p>
      <el-checkbox-group v-model="picked" class="pick-list">
        <el-checkbox
          v-for="kb in selectable"
          :key="kb.id"
          :value="kb.id"
          :disabled="boundIds.has(kb.id) && bound.length > 0"
        >
          {{ kb.name }}
          <span class="pick-meta">{{ kb.document_count }} 篇 · {{ kb.chunk_count }} 切块</span>
        </el-checkbox>
      </el-checkbox-group>
      <div class="default-row">
        <span>默认库</span>
        <el-select v-model="defaultKbId" clearable placeholder="不设默认" style="width: 260px">
          <el-option
            v-for="kb in selectable.filter((item) => picked.includes(item.id))"
            :key="kb.id"
            :label="kb.name"
            :value="kb.id"
          />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存关联</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.binding {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.head h2 {
  margin: 0 0 6px;
  font-size: 20px;
}
.sub {
  margin: 0;
  color: var(--aics-text2);
  font-size: 13px;
  line-height: 1.7;
  max-width: 720px;
}
.actions {
  display: flex;
  gap: 8px;
  flex: 0 0 auto;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.card {
  background: #fff;
  border: 1px solid var(--aics-border);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: box-shadow 0.2s;
}
.card:hover {
  box-shadow: 0 6px 16px rgb(0 0 0 / 8%);
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.name {
  font-size: 15px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desc {
  margin: 0;
  color: var(--aics-text2);
  font-size: 13px;
  line-height: 1.6;
  min-height: 21px;
}
.meta {
  display: flex;
  gap: 14px;
  color: var(--aics-text3);
  font-size: 12px;
}
.card-actions {
  display: flex;
  gap: 4px;
  border-top: 1px solid var(--aics-split);
  padding-top: 8px;
}
.note {
  margin-top: 4px;
}
.dialog-hint {
  margin: 0 0 12px;
  color: var(--aics-text2);
  font-size: 13px;
}
.pick-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 320px;
  overflow: auto;
}
.pick-meta {
  margin-left: 8px;
  color: var(--aics-text3);
  font-size: 12px;
}
.default-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
  font-size: 13px;
  color: var(--aics-text2);
}
</style>
