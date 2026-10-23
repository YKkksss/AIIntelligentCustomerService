<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

interface SessionDefaults {
  idle_close_minutes: number
  idle_notice_before_minutes: number
  idle_close_notice: string
}

const auth = useAuthStore()
const form = reactive<SessionDefaults>({
  idle_close_minutes: 10,
  idle_notice_before_minutes: 1,
  idle_close_notice: '',
})
const loading = ref(false)
const saving = ref(false)
//: 平台默认设置的版本号（ISSUE-77）：保存时回传，别人先改过就 409，避免静默覆盖。
const version = ref(0)
const canWrite = computed(() => auth.can('system.write'))

async function load() {
  loading.value = true
  try {
    const body = await systemApi.get()
    Object.assign(form, body.default_session_config as Partial<SessionDefaults>)
    version.value = body.version ?? 0
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    await systemApi.patch({ ...form }, version.value)
    ElMessage.success('已保存')
    await load()
  } catch (err: unknown) {
    const code = (err as { response?: { data?: { error?: { code?: string } } } }).response?.data
      ?.error?.code
    if (code === 'CONFIG_VERSION_CONFLICT') {
      ElMessage.error('这条设置刚被别人改过，已刷成最新值，请确认后重试')
      await load()
      return
    }
    throw err
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="system-settings">
    <el-alert
      type="info"
      :closable="false"
      title="平台默认会话配置只对新建智能客服与未单独设置的智能客服生效；智能客服自己的会话配置优先。"
    />
    <el-form label-width="200px" :disabled="!canWrite" class="form">
      <el-form-item label="空闲关闭（分钟）">
        <el-input-number v-model="form.idle_close_minutes" :min="1" :max="1440" />
      </el-form-item>
      <el-form-item label="关闭前提醒（分钟）">
        <el-input-number v-model="form.idle_notice_before_minutes" :min="0" :max="1440" />
      </el-form-item>
      <el-form-item label="关闭提醒话术">
        <el-input v-model="form.idle_close_notice" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item v-if="canWrite">
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.form {
  max-width: 720px;
  margin-top: 16px;
}
</style>
