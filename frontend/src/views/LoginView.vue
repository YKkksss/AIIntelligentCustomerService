<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import ReadyStatus from '@/components/ReadyStatus.vue'
import { useAuthStore } from '@/stores/auth'
import { APP_TITLE } from '@/utils'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const form = reactive({ email: '', password: '' })
const pending = ref(false)
const message = ref('')

const redirect = computed(() =>
  typeof route.query.redirect === 'string' ? route.query.redirect : '/',
)

async function onSubmit() {
  if (pending.value) return
  pending.value = true
  message.value = ''
  try {
    await auth.login(form.email, form.password)
    ElMessage.success('登录成功')
    await router.replace(redirect.value)
  } catch (err) {
    const code = (err as { response?: { data?: { error?: { code?: string } } } }).response?.data
      ?.error?.code
    message.value =
      code === 'ACCOUNT_LOCKED'
        ? '连续失败次数过多，账号已锁定，请 15 分钟后再试'
        : '邮箱或密码不正确'
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <div class="login">
    <el-card class="card">
      <h2>{{ APP_TITLE }}</h2>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="邮箱">
          <el-input v-model="form.email" autocomplete="username" data-test="email" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            data-test="password"
          />
        </el-form-item>
        <el-alert v-if="message" :title="message" type="error" :closable="false" class="alert" />
        <el-button type="primary" native-type="submit" :loading="pending" style="width: 100%">
          登录
        </el-button>
      </el-form>
      <ReadyStatus />
    </el-card>
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
}
.card {
  width: 380px;
}
h2 {
  text-align: center;
  margin: 0 0 16px;
}
.alert {
  margin-bottom: 12px;
}
</style>
