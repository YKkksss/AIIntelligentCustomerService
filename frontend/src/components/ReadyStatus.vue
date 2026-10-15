<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchReady, type ReadyResponse } from '@/api/http'

const ready = ref<ReadyResponse | null>(null)
const failed = ref(false)

onMounted(async () => {
  try {
    ready.value = await fetchReady()
  } catch {
    failed.value = true
  }
})
</script>

<template>
  <div class="ready">
    <span v-if="failed">后端未连接</span>
    <template v-else-if="ready">
      <el-tag
        v-for="(v, k) in ready.checks"
        :key="k"
        :type="v === 'ok' ? 'success' : 'danger'"
        size="small"
      >
        {{ k }}: {{ v }}
      </el-tag>
    </template>
  </div>
</template>

<style scoped>
.ready {
  display: flex;
  gap: 6px;
  justify-content: center;
  margin-top: 16px;
}
</style>
