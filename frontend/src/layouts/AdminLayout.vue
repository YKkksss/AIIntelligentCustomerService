<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { APP_TITLE } from '@/utils'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

interface MenuItem {
  name: string
  title: string
  icon: string
  permission?: string
}

/** 菜单从路由表派生：`meta.menu` 为真且权限满足的才出现（管理后台设计 §2）。 */
const menu = computed<MenuItem[]>(() => {
  const items: MenuItem[] = []
  for (const record of router.getRoutes()) {
    const meta = record.meta as {
      menu?: boolean
      title?: string
      permission?: string
      icon?: string
    }
    if (!meta.menu || !meta.title) continue
    if (meta.permission && !auth.can(meta.permission)) continue
    items.push({
      name: String(record.name),
      title: meta.title,
      icon: meta.icon ?? 'Menu',
      permission: meta.permission,
    })
  }
  return items
})

const active = computed(() => String(route.name ?? 'home'))

async function onSwitch(appId: string) {
  auth.switchApp(appId)
  const meta = route.meta as { permission?: string }
  if (meta.permission && !auth.can(meta.permission)) await router.replace({ name: 'home' })
}

async function onLogout() {
  await auth.logout()
  await router.replace({ name: 'login' })
}

function onCommand(command: string) {
  if (command === 'logout') void onLogout()
  if (command === 'password') void router.push({ name: 'password' })
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="brand">{{ APP_TITLE }}</div>
      <el-menu :default-active="active" router>
        <el-menu-item
          v-for="item in menu"
          :key="item.name"
          :index="item.name"
          :route="{ name: item.name }"
        >
          {{ item.title }}
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-select
          v-if="auth.apps.length > 1"
          :model-value="auth.currentAppId ?? ''"
          style="width: 220px"
          @update:model-value="onSwitch"
        >
          <el-option
            v-for="app in auth.apps"
            :key="app.app_id"
            :label="`${app.app_name}（${app.role_id}）`"
            :value="app.app_id"
          />
        </el-select>
        <div class="spacer" />
        <el-dropdown @command="onCommand">
          <span class="user">
            {{ auth.user?.display_name || auth.user?.email || '未登录' }}
            <el-tag v-if="auth.isSuperAdmin" size="small" type="warning">超管</el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="password">修改密码</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
}
.aside {
  background: #fff;
  border-right: 1px solid #ebeef5;
}
.brand {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 20px;
  font-weight: 600;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
}
.spacer {
  flex: 1;
}
.user {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
</style>
