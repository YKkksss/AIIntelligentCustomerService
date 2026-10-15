<script setup lang="ts">
import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import ReadyStatus from '@/components/ReadyStatus.vue'
import { APP_TITLE } from '@/utils'

// 占位：M4 接入 POST /api/v1/admin/auth/login
const form = reactive({ email: '', password: '' })

function onSubmit() {
  ElMessage.info('登录功能将在 M4 实现')
}
</script>

<template>
  <div class="login">
    <el-card class="card">
      <h2>{{ APP_TITLE }}</h2>
      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="邮箱">
          <el-input v-model="form.email" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" style="width: 100%">登录</el-button>
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
</style>
