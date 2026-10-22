import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    requiresAuth?: boolean
    /** 该页需要的权限名（见后端 modules/auth/permissions.py）。 */
    permission?: string
    /** 是否进入左侧菜单。 */
    menu?: boolean
    title?: string
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
  {
    path: '/',
    component: () => import('@/layouts/AdminLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/views/DashboardView.vue'),
        meta: { menu: true, title: '仪表盘', icon: 'Odometer' },
      },
      {
        path: 'apps',
        name: 'apps',
        component: () => import('@/views/AppsView.vue'),
        meta: { menu: true, title: '智能客服', permission: 'app.read', icon: 'Grid' },
      },
      // 单个智能客服的所有设置页都收进「配置弹窗」：AppConsoleLayout 渲染弹窗外壳，
      // 左侧是页内菜单，右侧 <router-view> 渲染下面这些子路由。侧栏始终只有四项全局菜单。
      {
        path: 'apps/:appId',
        component: () => import('@/layouts/AppConsoleLayout.vue'),
        meta: { permission: 'app.read' },
        children: [
          { path: '', redirect: (to) => ({ name: 'app-overview', params: to.params }) },
          {
            path: 'overview',
            name: 'app-overview',
            component: () => import('@/views/AppOverviewView.vue'),
            meta: { permission: 'app.read', title: '概览' },
          },
          {
            path: 'config',
            name: 'app-config',
            component: () => import('@/views/AppConfigView.vue'),
            meta: { permission: 'app.read', title: '配置' },
          },
          {
            path: 'knowledge',
            name: 'app-knowledge',
            component: () => import('@/views/AppKnowledgeView.vue'),
            meta: { permission: 'kb.read', title: '关联知识库' },
          },
          {
            path: 'knowledge/:documentId',
            name: 'app-knowledge-document',
            component: () => import('@/views/KnowledgeDocumentView.vue'),
            meta: { permission: 'kb.read', title: '文档切块' },
          },
          {
            path: 'hit-test',
            name: 'app-hit-test',
            component: () => import('@/views/HitTestView.vue'),
            meta: { permission: 'kb.read', title: '命中测试' },
          },
          {
            path: 'debug',
            name: 'app-debug',
            component: () => import('@/views/DebugView.vue'),
            meta: { permission: 'chat.debug', title: '调试对话' },
          },
          {
            path: 'keys',
            name: 'app-keys',
            component: () => import('@/views/KeysView.vue'),
            meta: { permission: 'key.read', title: 'API Key' },
          },
          {
            path: 'logs',
            name: 'app-logs',
            component: () => import('@/views/LogsView.vue'),
            meta: { permission: 'log.read', title: '会话' },
          },
          {
            path: 'webhooks',
            name: 'app-webhooks',
            component: () => import('@/views/WebhooksView.vue'),
            meta: { permission: 'webhook.write', title: 'Webhook' },
          },
        ],
      },
      {
        path: 'knowledge-bases',
        name: 'knowledge-bases',
        component: () => import('@/views/KnowledgeBasesView.vue'),
        meta: { menu: true, title: '知识库', permission: 'kb.read', icon: 'Collection' },
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('@/views/UsersView.vue'),
        meta: { menu: true, title: '用户', permission: 'user.admin', icon: 'User' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/views/AuditView.vue'),
        meta: { menu: true, title: '审计日志', permission: 'audit.read', icon: 'Document' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SystemSettingsView.vue'),
        meta: { menu: true, title: '系统设置', permission: 'system.read', icon: 'Setting' },
      },
      {
        path: 'password',
        name: 'password',
        component: () => import('@/views/PasswordView.vue'),
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })

// 登录守卫：未登录跳登录页；登录但身份未加载时先补 `GET /auth/me`，再按权限判断
export async function authGuard(to: {
  meta: { requiresAuth?: boolean; permission?: string }
  fullPath: string
}): Promise<true | { name: string; query?: Record<string, string> }> {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.requiresAuth) {
    await auth.ensureLoaded()
    if (!auth.isLoggedIn) return { name: 'login', query: { redirect: to.fullPath } }
    if (to.meta.permission && !auth.can(to.meta.permission)) return { name: 'home' }
  }
  return true
}

router.beforeEach(authGuard)

export default router
