<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { userApi, type AdminUser } from '@/api/admin'

const rows = ref<AdminUser[]>([])
const loading = ref(false)
const keyword = ref('')
const createVisible = ref(false)
const creating = ref(false)
const form = reactive({ email: '', display_name: '', password: '', is_super_admin: false })
const temporary = ref('')

async function load() {
  loading.value = true
  try {
    rows.value = (await userApi.list(keyword.value)).items
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  creating.value = true
  try {
    await userApi.create({ ...form })
    ElMessage.success('已创建，该用户首次登录须改密')
    createVisible.value = false
    Object.assign(form, { email: '', display_name: '', password: '', is_super_admin: false })
    await load()
  } finally {
    creating.value = false
  }
}

async function toggleStatus(row: AdminUser) {
  const next = row.status === 'disabled' ? 'active' : 'disabled'
  await ElMessageBox.confirm(
    next === 'disabled' ? '禁用后该用户的所有登录态立即失效，确定？' : '确定启用该用户？',
    '确认',
  )
  await userApi.patch(row.id, { status: next })
  ElMessage.success('已更新')
  await load()
}

async function onReset(row: AdminUser) {
  await ElMessageBox.confirm('将生成一次性临时密码，用户的旧登录态立即失效。继续？', '重置密码')
  const result = await userApi.resetPassword(row.id)
  temporary.value = result.password
}

async function onDelete(row: AdminUser) {
  await ElMessageBox.confirm(`确定删除 ${row.email}？`, '删除用户', { type: 'warning' })
  await userApi.remove(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <el-card>
    <template #header>
      <div class="header">
        <span>用户</span>
        <div class="actions">
          <el-input
            v-model="keyword"
            placeholder="邮箱或姓名"
            clearable
            style="width: 200px"
            @change="load"
          />
          <el-button type="primary" @click="createVisible = true">新建用户</el-button>
        </div>
      </div>
    </template>

    <el-table v-loading="loading" :data="rows" stripe>
      <el-table-column prop="email" label="邮箱" min-width="200" />
      <el-table-column prop="display_name" label="姓名" width="140" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'disabled' ? 'info' : 'success'" size="small">
            {{ row.status === 'disabled' ? '已禁用' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="超管" width="80">
        <template #default="{ row }">{{ row.is_super_admin ? '是' : '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="240">
        <template #default="{ row }">
          <el-button link type="primary" @click="toggleStatus(row)">
            {{ row.status === 'disabled' ? '启用' : '禁用' }}
          </el-button>
          <el-button link type="primary" @click="onReset(row)">重置密码</el-button>
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="createVisible" title="新建用户" width="420">
      <el-form label-position="top">
        <el-form-item label="邮箱"><el-input v-model="form.email" /></el-form-item>
        <el-form-item label="姓名"><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item label="初始密码（≥8 位）">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="超级管理员">
          <el-switch v-model="form.is_super_admin" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="!!temporary"
      title="一次性临时密码"
      width="420"
      @close="temporary = ''"
    >
      <p>只显示这一次，请通过安全渠道交给用户；用户登录后必须改密。</p>
      <el-input :model-value="temporary" readonly />
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.actions {
  display: flex;
  gap: 8px;
}
</style>
