<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const apps = computed(() => auth.apps)
const user = computed(() => auth.user)
</script>

<template>
  <div class="home">
    <el-card>
      <template #header>当前会话</template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="账号">{{ user?.email }}</el-descriptions-item>
        <el-descriptions-item label="姓名">{{ user?.display_name }}</el-descriptions-item>
        <el-descriptions-item label="超级管理员">
          {{ auth.isSuperAdmin ? '是' : '否' }}
        </el-descriptions-item>
        <el-descriptions-item label="可见应用">{{ apps.length }} 个</el-descriptions-item>
        <el-descriptions-item label="当前应用">
          {{ auth.currentApp?.app_name ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="角色">
          {{ auth.currentApp?.role_id ?? '—' }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
    <el-card class="next">
      <p>登录、权限与审计已可用。应用管理、知识库、调试对话等页面按 M4 后续子步骤交付。</p>
    </el-card>
  </div>
</template>

<style scoped>
.home {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.next {
  color: #606266;
}
</style>
