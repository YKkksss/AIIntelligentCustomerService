<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { appApi, type AppCard } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const rows = ref<AppCard[]>([])
const loading = ref(false)
const createVisible = ref(false)
const creating = ref(false)
const form = reactive({ name: '', description: '' })
const canWrite = () => auth.can('app.write')

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

onMounted(load)
</script>

<template>
  <div class="apps">
    <div class="toolbar">
      <el-button v-if="canWrite()" type="primary" @click="createVisible = true">新建应用</el-button>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-empty v-if="!loading && rows.length === 0" description="还没有可见的应用" />

    <el-row :gutter="16">
      <el-col v-for="row in rows" :key="row.id" :span="8" class="card-col">
        <el-card shadow="hover">
          <template #header>
            <div class="card-header">
              <span class="name">{{ row.name }}</span>
              <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
                {{ row.status }}
              </el-tag>
            </div>
          </template>
          <p class="description">{{ row.description || '（无描述）' }}</p>
          <p class="meta">文档 {{ row.stats.documents }} 篇 · 配置版本 v{{ row.config_version }}</p>
          <div class="actions">
            <el-button
              link
              type="primary"
              @click="router.push({ name: 'app-config', params: { appId: row.id } })"
            >
              配置
            </el-button>
            <el-button v-if="canWrite()" link type="primary" @click="onCopy(row)">复制</el-button>
            <el-button v-if="canWrite()" link type="danger" @click="onDelete(row)">删除</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="createVisible" title="新建应用" width="420px">
      <el-form label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如：售后客服" />
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
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.card-col {
  margin-bottom: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.name {
  font-weight: 600;
}
.description {
  min-height: 22px;
  margin: 0 0 8px;
  color: #606266;
}
.meta {
  margin: 0 0 12px;
  color: #909399;
  font-size: 12px;
}
</style>
