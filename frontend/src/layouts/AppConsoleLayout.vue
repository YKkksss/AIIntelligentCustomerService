<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  ChatLineSquare,
  Collection,
  Connection,
  DataAnalysis,
  DataBoard,
  Key,
  Search,
  Setting,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { appApi, type AppAvatar } from '@/api/admin'
import AssistantAvatar from '@/components/AssistantAvatar.vue'
import AppsView from '@/views/AppsView.vue'

interface NavItem {
  name: string
  title: string
  icon: Component
  /** 需要的权限名；没有则对所有能进这个应用的人可见。 */
  permission?: string
  /** 未实现的页面只占位，等里程碑补齐。 */
  pending?: string
  group: string
}

/** 弹窗内的页内菜单：替代原先塞在侧栏 / 工作台条里的应用级入口。 */
const NAV: NavItem[] = [
  { name: 'app-overview', title: '概览', icon: DataBoard, permission: 'app.read', group: '看板' },
  { name: 'app-config', title: '配置', icon: Setting, permission: 'app.read', group: '看板' },
  {
    name: 'app-knowledge',
    title: '关联知识库',
    icon: Collection,
    permission: 'kb.read',
    group: '知识与调试',
  },
  {
    name: 'app-hit-test',
    title: '命中测试',
    icon: Search,
    permission: 'kb.read',
    group: '知识与调试',
  },
  {
    name: 'app-debug',
    title: '调试对话',
    icon: ChatDotRound,
    permission: 'chat.debug',
    group: '知识与调试',
  },
  { name: 'app-eval', title: '评测', icon: DataAnalysis, pending: 'M5', group: '知识与调试' },
  { name: 'app-keys', title: 'API Key', icon: Key, permission: 'key.read', group: '接入与运营' },
  {
    name: 'app-webhooks',
    title: 'Webhook',
    icon: Connection,
    permission: 'webhook.write',
    group: '接入与运营',
  },
  {
    name: 'app-logs',
    title: '会话',
    icon: ChatLineSquare,
    permission: 'log.read',
    group: '接入与运营',
  },
]

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const open = ref(true)

const appId = computed(() => String(route.params.appId ?? ''))
const appName = computed(() => auth.currentApp?.app_name ?? '智能客服')
const avatar = ref<AppAvatar | null>(null)

/** 头像只在详情接口里，成员关系里没有；控制台打开时取一次，切换客服再取。 */
async function loadAvatar() {
  if (!appId.value) return
  try {
    avatar.value = (await appApi.detail(appId.value)).avatar
  } catch {
    avatar.value = null
  }
}

onMounted(loadAvatar)
watch(appId, loadAvatar)

const groups = computed(() => {
  const out: { title: string; items: NavItem[] }[] = []
  for (const item of NAV) {
    if (item.permission && !auth.can(item.permission, appId.value)) continue
    const last = out[out.length - 1]
    if (last && last.title === item.group) last.items.push(item)
    else out.push({ title: item.group, items: [item] })
  }
  return out
})

/** 文档切块挂在知识库下面，导航高亮跟着归并。 */
const activeName = computed(() => {
  const name = String(route.name ?? '')
  return name === 'app-knowledge-document' ? 'app-knowledge' : name
})

/** 弹窗收起后回到列表页：URL 与界面保持一致，刷新也不会停在半开状态。 */
function onClosed() {
  if (String(route.name ?? '') !== 'apps') void router.push({ name: 'apps' })
}
</script>

<template>
  <div class="console-shell">
    <!-- 背景保留智能客服列表：弹窗只是盖在它上面，关掉就回到原处 -->
    <AppsView />

    <el-dialog
      v-model="open"
      class="app-console-dialog"
      width="calc(100vw - 100px)"
      top="50px"
      :show-close="true"
      :close-on-click-modal="false"
      header-class="console-header"
      body-class="console-body-wrap"
      style="--el-dialog-padding-primary: 0px"
      @closed="onClosed"
    >
      <template #header>
        <div class="console-head">
          <AssistantAvatar :avatar="avatar" :stable-seed="appId" :name="appName" :size="40" />
          <div class="console-head-text">
            <div class="console-name-row">
              <span class="console-name">{{ appName }}</span>
              <span class="console-tag">智能客服</span>
            </div>
            <span class="console-sub">这里的设置只对这个智能客服生效，不影响其他客服</span>
          </div>
        </div>
      </template>

      <div class="console-body">
        <aside class="console-nav">
          <template v-for="group in groups" :key="group.title">
            <p class="nav-group">{{ group.title }}</p>
            <template v-for="item in group.items" :key="item.name">
              <span v-if="item.pending" class="nav-item pending">
                <el-icon><component :is="item.icon" /></el-icon>
                <span class="nav-text">{{ item.title }}</span>
                <span class="nav-pill">{{ item.pending }}</span>
              </span>
              <router-link
                v-else
                class="nav-item"
                :class="{ active: activeName === item.name }"
                :to="{ name: item.name, params: { appId } }"
              >
                <el-icon><component :is="item.icon" /></el-icon>
                <span class="nav-text">{{ item.title }}</span>
              </router-link>
            </template>
          </template>
        </aside>

        <section class="console-main">
          <router-view />
        </section>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.console-shell {
  min-height: 100%;
}

.console-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 44px 4px 4px;
}
.console-avatar {
  width: 40px;
  height: 40px;
  flex: 0 0 40px;
  border-radius: 12px;
  background: linear-gradient(135deg, #e6f4ff 0%, #bae0ff 100%);
  color: var(--aics-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  font-weight: 600;
}
.console-head-text {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.console-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.console-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--aics-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 460px;
}
.console-tag {
  height: 20px;
  padding: 0 6px;
  border-radius: 4px;
  background: var(--aics-primary-bg);
  color: var(--aics-primary);
  font-size: 12px;
  line-height: 20px;
  white-space: nowrap;
}
.console-sub {
  font-size: 12px;
  color: var(--aics-text-3);
}

.console-body {
  display: flex;
  flex: 1;
  min-height: 0;
  background: #f7f9fc;
  border-top: 1px solid var(--aics-split);
}

.console-nav {
  width: 196px;
  flex: 0 0 196px;
  padding: 12px 10px 16px;
  background: var(--aics-white);
  border-right: 1px solid var(--aics-split);
  overflow-y: auto;
}
.nav-group {
  margin: 12px 0 6px;
  padding: 0 10px;
  font-size: 12px;
  color: var(--aics-text-3);
  letter-spacing: 0.4px;
}
.nav-group:first-child {
  margin-top: 0;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 12px;
  border-radius: 8px;
  color: var(--aics-text-2);
  text-decoration: none;
  font-size: 14px;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;
}
.nav-item:hover {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
}
.nav-item.active {
  color: var(--aics-primary);
  background: var(--aics-primary-bg);
  font-weight: 600;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--aics-primary);
}
.nav-item.pending {
  color: var(--aics-text-3);
  cursor: not-allowed;
}
.nav-item.pending:hover {
  color: var(--aics-text-3);
  background: transparent;
}
.nav-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nav-pill {
  flex: 0 0 auto;
  height: 18px;
  padding: 0 5px;
  border-radius: 4px;
  background: #f0f2f5;
  font-size: 11px;
  line-height: 18px;
  font-weight: 400;
}

.console-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: 20px 24px 24px;
}

/* 窄屏：左侧导航收窄，主体仍然弹性铺满 */
@media (max-width: 860px) {
  .console-nav {
    width: 164px;
    flex-basis: 164px;
  }
  .console-main {
    padding: 16px;
  }
}
</style>

<style>
/* 弹窗本体：四周各留 50px 的对话框形态，头部固定、主体弹性撑满 */
.app-console-dialog.el-dialog {
  --el-dialog-margin-top: 50px;
  display: flex;
  flex-direction: column;
  width: calc(100vw - 100px);
  max-width: calc(100vw - 100px);
  height: calc(100vh - 100px);
  margin: 50px auto;
  padding: 0;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 24px 64px rgb(15 23 42 / 24%);
}
.app-console-dialog .el-dialog__header {
  flex: 0 0 auto;
  margin: 0;
  padding: 14px 20px;
  border-bottom: 1px solid var(--aics-split);
}
.app-console-dialog .el-dialog__headerbtn {
  top: 12px;
  right: 14px;
  width: 32px;
  height: 32px;
  z-index: 2;
}
.app-console-dialog .el-dialog__headerbtn .el-dialog__close {
  font-size: 18px;
}
.app-console-dialog .el-dialog__body {
  display: flex;
  flex: 1;
  min-height: 0;
  padding: 0;
}
/* 遮罩层不让页面跟着滚，弹窗四周的留白始终是 50px */
.el-overlay-dialog:has(> .app-console-dialog) {
  padding: 0;
  overflow: hidden;
}
/* 小屏按 16px 留白，避免可视区域被吃掉太多 */
@media (max-width: 900px) {
  .app-console-dialog.el-dialog {
    --el-dialog-margin-top: 16px;
    width: calc(100vw - 32px);
    max-width: calc(100vw - 32px);
    height: calc(100vh - 32px);
    margin: 16px auto;
  }
}
</style>
