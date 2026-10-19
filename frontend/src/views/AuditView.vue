<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { auditApi, type AuditRow } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const rows = ref<AuditRow[]>([])
const actions = ref<string[]>([])
const action = ref('')
const loading = ref(false)
const cursor = ref<string | null>(null)
const detail = ref<AuditRow | null>(null)

async function load(reset = true) {
  loading.value = true
  try {
    const page = await auditApi.page({
      appId: auth.isSuperAdmin ? undefined : (auth.currentAppId ?? undefined),
      action: action.value,
      cursor: reset ? null : cursor.value,
    })
    rows.value = reset ? page.items : [...rows.value, ...page.items]
    cursor.value = page.next_cursor
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  actions.value = await auditApi.actions().catch(() => [])
  await load()
})
</script>

<template>
  <el-card>
    <template #header>
      <div class="header">
        <span>审计日志</span>
        <el-select
          v-model="action"
          placeholder="全部动作"
          clearable
          style="width: 240px"
          @change="load()"
        >
          <el-option v-for="name in actions" :key="name" :label="name" :value="name" />
        </el-select>
      </div>
    </template>

    <el-table v-loading="loading" :data="rows" stripe>
      <el-table-column prop="created_at" label="时间" width="180" />
      <el-table-column prop="action" label="动作" width="200" />
      <el-table-column prop="actor_type" label="主体" width="100" />
      <el-table-column prop="actor_id" label="主体 ID" width="280" show-overflow-tooltip />
      <el-table-column prop="request_id" label="request_id" show-overflow-tooltip />
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-button link type="primary" @click="detail = row">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="more">
      <el-button v-if="cursor" :loading="loading" @click="load(false)">加载更多</el-button>
      <span v-else class="end">没有更多了</span>
    </div>

    <el-drawer :model-value="!!detail" size="520" title="审计详情" @close="detail = null">
      <pre v-if="detail">{{ JSON.stringify(detail, null, 2) }}</pre>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.more {
  margin-top: 16px;
  text-align: center;
}
.end {
  color: #909399;
  font-size: 13px;
}
pre {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
