<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const form = reactive({ current: '', next: '' })
const pending = ref(false)
const error = ref('')

async function onSubmit() {
  if (pending.value) return
  pending.value = true
  error.value = ''
  try {
    await auth.changePassword(form.current, form.next)
    ElMessage.success('密码已修改，其他设备的登录态已失效')
    await router.replace({ name: 'home' })
  } catch (err) {
    const details = (
      err as { response?: { data?: { error?: { details?: { errors?: unknown[] } } } } }
    ).response?.data?.error?.details
    error.value = details ? '当前密码不正确，或新密码不满足要求' : '修改失败，请稍后再试'
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <el-card class="card">
    <template #header>修改密码</template>
    <el-form label-position="top" @submit.prevent="onSubmit">
      <el-form-item label="当前密码">
        <el-input v-model="form.current" type="password" show-password />
      </el-form-item>
      <el-form-item label="新密码（≥8 位）">
        <el-input v-model="form.next" type="password" show-password />
      </el-form-item>
      <el-alert v-if="error" :title="error" type="error" :closable="false" class="alert" />
      <el-button type="primary" native-type="submit" :loading="pending">保存</el-button>
    </el-form>
  </el-card>
</template>

<style scoped>
.card {
  max-width: 420px;
}
.alert {
  margin-bottom: 12px;
}
</style>
