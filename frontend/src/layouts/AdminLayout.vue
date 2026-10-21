<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDown,
  ArrowLeft,
  Bell,
  ChatDotRound,
  ChatLineSquare,
  Collection,
  Connection,
  DataAnalysis,
  DataBoard,
  Document,
  Key,
  Odometer,
  Service,
  Setting,
  User,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

interface MenuItem {
  name: string
  title: string
  icon: unknown
  permission?: string
  /** 未实现的页面只占位（设计稿 6 个应用菜单项，逐里程碑补齐）。 */
  pending?: string
}

/** 全局菜单：仪表盘 / 智能客服 / 用户 / 审计（应用级入口收进工作台，见下）。 */
const globalMenu = computed<MenuItem[]>(() =>
  [
    { name: 'home', title: '仪表盘', icon: Odometer },
    { name: 'apps', title: '智能客服', icon: Service, permission: 'app.read' },
    { name: 'users', title: '用户', icon: User, permission: 'user.admin' },
    { name: 'audit', title: '审计', icon: Document, permission: 'audit.read' },
  ].filter((item) => !item.permission || auth.can(item.permission)),
)

/** 工作台导航：进入某个智能客服后才出现的一组页签（替代原先塞在侧栏里的 8 项）。 */
const workspaceMenu = computed<MenuItem[]>(() =>
  [
    { name: 'app-overview', title: '概览', icon: DataBoard },
    { name: 'app-config', title: '配置', icon: Setting },
    { name: 'app-knowledge', title: '知识库', icon: Collection, permission: 'kb.read' },
    { name: 'app-debug', title: '调试对话', icon: ChatDotRound, permission: 'chat.debug' },
    { name: 'app-eval', title: '评测', icon: DataAnalysis, pending: 'M5' },
    { name: 'app-keys', title: 'API Key', icon: Key, permission: 'key.read' },
    { name: 'app-logs', title: '会话与反馈', icon: ChatLineSquare, permission: 'log.read' },
    { name: 'app-webhooks', title: 'Webhook', icon: Connection, permission: 'webhook.write' },
  ].filter((item) => !item.permission || auth.can(item.permission)),
)

const active = computed(() => String(route.name ?? 'home'))

const inWorkspace = computed(
  () => typeof route.params.appId === 'string' && route.params.appId !== '',
)

const appName = computed(() => auth.currentApp?.app_name ?? '智能客服')

/** 进入某个应用的页面时，侧栏与标题跟着路由切换当前应用（刷新后也能恢复）。 */
watch(
  () => route.params.appId,
  (appId) => {
    if (typeof appId === 'string' && appId && appId !== auth.currentAppId) auth.switchApp(appId)
  },
  { immediate: true },
)

const appPath = computed(() => {
  if (!auth.currentAppId) return { name: 'apps' }
  return { name: 'app-config', params: { appId: auth.currentAppId } }
})

/** 面包屑：与设计稿 Top Bar 的「智能客服 / 全部智能客服」一致。 */
const breadcrumb = computed<string[]>(() => {
  switch (String(route.name ?? '')) {
    case 'home':
      return ['仪表盘']
    case 'apps':
      return ['智能客服', '全部智能客服']
    case 'app-config':
      return ['智能客服', appName.value, '配置']
    case 'app-overview':
      return ['智能客服', appName.value, '概览']
    case 'app-knowledge':
      return ['智能客服', appName.value, '知识库']
    case 'app-knowledge-document':
      return ['智能客服', appName.value, '知识库', '文档切块']
    case 'app-hit-test':
      return ['智能客服', appName.value, '知识库', '命中测试']
    case 'app-debug':
      return ['智能客服', appName.value, '调试对话']
    case 'app-keys':
      return ['智能客服', appName.value, 'API Key']
    case 'app-logs':
      return ['智能客服', appName.value, '会话与反馈']
    case 'app-webhooks':
      return ['智能客服', appName.value, 'Webhook']
    case 'users':
      return ['用户']
    case 'audit':
      return ['审计']
    case 'settings':
      return ['系统设置']
    default:
      return ['概览']
  }
})

const initial = computed(() => {
  const name = auth.user?.display_name || auth.user?.email || '?'
  return name.slice(0, 1).toUpperCase()
})

async function onSwitch(target: string) {
  if (target === 'all') {
    auth.switchApp(null)
    await router.push({ name: 'apps' })
    return
  }
  auth.switchApp(target)
  const meta = route.meta as { permission?: string }
  if (meta.permission && !auth.can(meta.permission)) {
    await router.replace(appPath.value)
    return
  }
  if (!auth.can('app.read')) await router.replace({ name: 'home' })
}

async function onWorkspace(item: MenuItem) {
  if (item.pending) return
  await router.push({ name: item.name, params: { appId: auth.currentAppId } })
}

async function onCommand(command: string) {
  if (command === 'logout') {
    await auth.logout()
    await router.replace({ name: 'login' })
    return
  }
  if (command === 'password') await router.push({ name: 'password' })
  if (command === 'settings') await router.push({ name: 'settings' })
}
</script>

<template>
  <div class="shell">
    <aside class="sider">
      <div class="brand">
        <span class="brand-logo"
          ><el-icon><Service /></el-icon
        ></span>
        <span class="brand-name">AICS</span>
        <span class="brand-tag">智能客服</span>
      </div>

      <nav class="menu">
        <router-link
          v-for="item in globalMenu"
          :key="item.name"
          class="menu-item"
          :class="{ active: active === item.name || (item.name === 'apps' && inWorkspace) }"
          :to="{ name: item.name }"
        >
          <el-icon class="menu-icon"><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </router-link>
      </nav>

      <div class="sider-foot">
        <span class="sider-hint">AICS · 企业内部智能客服平台</span>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <el-dropdown trigger="click" @command="onSwitch">
          <button class="switcher" type="button">
            <span class="switcher-tile"
              ><el-icon><Service /></el-icon
            ></span>
            <span class="switcher-label">{{ inWorkspace ? appName : '全部智能客服' }}</span>
            <el-icon class="switcher-caret"><ArrowDown /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="all">全部智能客服</el-dropdown-item>
              <el-dropdown-item v-for="app in auth.apps" :key="app.app_id" :command="app.app_id">
                {{ app.app_name }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <div class="breadcrumb">
          <template v-for="(crumb, index) in breadcrumb" :key="crumb">
            <span v-if="index > 0" class="breadcrumb-sep">/</span>
            <span :class="{ last: index === breadcrumb.length - 1 }">{{ crumb }}</span>
          </template>
        </div>

        <div class="spacer" />

        <a class="topbar-link" href="/docs" target="_blank" rel="noreferrer">文档</a>
        <el-icon class="topbar-bell"><Bell /></el-icon>

        <el-dropdown trigger="click" @command="onCommand">
          <span class="user">
            <span class="avatar">{{ initial }}</span>
            <span class="user-name">{{ auth.user?.display_name || auth.user?.email }}</span>
            <el-icon class="user-caret"><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="auth.can('system.read')" command="settings">
                系统设置
              </el-dropdown-item>
              <el-dropdown-item command="password">修改密码</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </header>

      <!-- 工作台：进入某个智能客服后出现，承载原先「应用内菜单」的 8 个入口，侧栏保持干净 -->
      <div v-if="inWorkspace" class="workspace">
        <button class="workspace-back" type="button" @click="router.push({ name: 'apps' })">
          <el-icon><ArrowLeft /></el-icon>
          <span>全部智能客服</span>
        </button>
        <span class="workspace-sep" />
        <div class="workspace-title">
          <span class="workspace-name">{{ appName }}</span>
          <span class="workspace-tag">智能客服</span>
        </div>
        <nav class="workspace-tabs">
          <template v-for="item in workspaceMenu" :key="item.name">
            <el-tooltip
              v-if="item.pending"
              :content="`${item.pending} 完成后开放`"
              placement="bottom"
            >
              <span class="ws-tab pending">
                <el-icon><component :is="item.icon" /></el-icon>
                <span>{{ item.title }}</span>
              </span>
            </el-tooltip>
            <button
              v-else
              type="button"
              class="ws-tab"
              :class="{ active: active === item.name }"
              @click="onWorkspace(item)"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.title }}</span>
            </button>
          </template>
        </nav>
      </div>

      <main class="content" :class="{ 'with-workspace': inWorkspace }">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100vh;
  background: var(--aics-layout);
}

.sider {
  width: var(--aics-sider-width);
  flex: 0 0 var(--aics-sider-width);
  background: var(--aics-sider);
  display: flex;
  flex-direction: column;
  padding-top: 16px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px 12px;
}
.brand-logo {
  width: 28px;
  height: 28px;
  border-radius: var(--aics-radius-sm);
  background: var(--aics-primary);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
}
.brand-name {
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-tag {
  color: rgb(255 255 255 / 45%);
  font-size: 12px;
}

.menu {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--aics-menu-height);
  padding: 0 16px;
  border-radius: var(--aics-radius-sm);
  color: rgb(255 255 255 / 65%);
  text-decoration: none;
  font-size: 14px;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;
}
.menu-item:hover {
  color: #fff;
  background: rgb(255 255 255 / 8%);
}
.menu-item.active {
  color: #fff;
  background: var(--aics-primary);
}
.menu-icon {
  font-size: 16px;
}

.sider-foot {
  margin-top: auto;
  padding: 16px 20px 20px;
}
.sider-hint {
  color: rgb(255 255 255 / 30%);
  font-size: 12px;
}

.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.topbar {
  height: var(--aics-topbar-height);
  flex: 0 0 var(--aics-topbar-height);
  background: var(--aics-white);
  border-bottom: 1px solid var(--aics-split);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 24px;
}

.switcher {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 128px;
  max-width: 224px;
  height: var(--aics-control-height);
  padding: 0 10px 0 8px;
  background: var(--aics-white);
  border: 1px solid var(--aics-border);
  border-radius: var(--aics-radius-sm);
  cursor: pointer;
  font: inherit;
  color: var(--aics-text);
}
.switcher-tile {
  width: 16px;
  height: 16px;
  border-radius: 4px;
  background: var(--aics-primary);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
}
.switcher-label {
  flex: 1;
  text-align: left;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.switcher-caret {
  color: var(--aics-text-3);
  font-size: 12px;
}

.breadcrumb {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--aics-text-3);
  font-size: 14px;
}
.breadcrumb-sep {
  color: var(--aics-text-3);
}
.breadcrumb .last {
  color: var(--aics-text);
}

.spacer {
  flex: 1;
}

.topbar-link {
  color: var(--aics-text-2);
  text-decoration: none;
  font-size: 14px;
}
.topbar-link:hover {
  color: var(--aics-primary);
}
.topbar-bell {
  color: var(--aics-text-2);
  font-size: 16px;
  margin-left: 8px;
}

.user {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: var(--aics-text);
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--aics-primary);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.user-name {
  font-size: 14px;
}
.user-caret {
  color: var(--aics-text-3);
  font-size: 12px;
}

.workspace {
  flex: 0 0 48px;
  height: 48px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 24px;
  background: var(--aics-white);
  border-bottom: 1px solid var(--aics-split);
  min-width: 0;
}
.workspace-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 0;
  background: transparent;
  color: var(--aics-text-3);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 6px;
  white-space: nowrap;
}
.workspace-back:hover {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
}
.workspace-sep {
  width: 1px;
  height: 16px;
  background: var(--aics-border);
  flex: 0 0 1px;
}
.workspace-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.workspace-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--aics-text);
  max-width: 200px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.workspace-tag {
  height: 20px;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--aics-primary-bg);
  color: var(--aics-primary);
  font-size: 12px;
  line-height: 20px;
  white-space: nowrap;
}
.workspace-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: 12px;
  overflow-x: auto;
  scrollbar-width: none;
}
.workspace-tabs::-webkit-scrollbar {
  display: none;
}
.ws-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--aics-text-2);
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;
}
.ws-tab:hover {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
}
.ws-tab.active {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
  font-weight: 600;
}
.ws-tab.pending {
  color: var(--aics-text-3);
  cursor: not-allowed;
}
.ws-tab.pending:hover {
  color: var(--aics-text-3);
  background: transparent;
}

.content {
  flex: 1;
  min-height: 0;
  padding: 24px;
  box-sizing: border-box;
}
.content.with-workspace {
  padding-top: 20px;
}
</style>
