<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  webhookApi,
  type DeliveryRow,
  type WebhookBody,
  type WebhookSecret,
  type WebhookTestResult,
} from '@/api/webhooks'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()
const appId = computed(() => String(route.params.appId ?? auth.currentAppId ?? ''))
const canWrite = computed(() => auth.can('webhook.write'))

const config = ref<WebhookBody | null>(null)
const rows = ref<DeliveryRow[]>([])
const loading = ref(false)
const status = ref('')
const testing = ref(false)
const testResult = ref<WebhookTestResult | null>(null)
const secret = ref<WebhookSecret | null>(null)
const secretVisible = ref(false)

async function load() {
  if (!appId.value) return
  loading.value = true
  try {
    const [body, deliveries] = await Promise.all([
      webhookApi.config(appId.value),
      webhookApi.deliveries(appId.value, { status: status.value, limit: 30 }),
    ])
    config.value = body
    rows.value = deliveries
  } finally {
    loading.value = false
  }
}

async function onTest() {
  testing.value = true
  try {
    testResult.value = await webhookApi.test(appId.value)
    ElMessage[testResult.value.ok ? 'success' : 'error'](
      testResult.value.ok ? '测试事件已送达' : `测试失败：${testResult.value.error ?? '未知错误'}`,
    )
  } finally {
    testing.value = false
  }
}

async function onRotate() {
  await ElMessageBox.confirm('轮换后旧密钥立即失效，你的接收端需要同步更新。继续？', '轮换密钥', {
    type: 'warning',
  })
  secret.value = await webhookApi.rotateSecret(appId.value)
  secretVisible.value = true
  await load()
}

async function onRedeliver(row: DeliveryRow) {
  await webhookApi.redeliver(appId.value, row.id)
  ElMessage.success('已重新排队')
  await load()
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('请手动复制')
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="head">
      <div>
        <div class="aics-page-title">Webhook</div>
        <div class="aics-page-subtitle">
          会话事件投递记录：每次投递带签名，失败按退避重试，可人工重发。
        </div>
      </div>
      <el-button v-if="canWrite" @click="onRotate">轮换签名密钥</el-button>
    </div>

    <el-card v-loading="loading" class="aics-card">
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="回调地址">{{ config?.url ?? '未配置' }}</el-descriptions-item>
        <el-descriptions-item label="签名密钥">{{
          config?.secret_prefix ?? '未生成'
        }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="config?.ready ? 'success' : 'info'" size="small">
            {{ config?.ready ? '已就绪' : '未配置' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <div class="actions">
        <el-button v-if="canWrite" :loading="testing" @click="onTest">发送测试事件</el-button>
        <span v-if="testResult" class="result">
          最近一次：{{ testResult.ok ? '成功' : '失败' }}（{{ testResult.status_code ?? '—' }}，{{
            testResult.elapsed_ms
          }}
          ms）
        </span>
      </div>
    </el-card>

    <el-card class="aics-card">
      <template #header>
        <div class="table-head">
          <span>投递记录</span>
          <el-select
            v-model="status"
            placeholder="全部状态"
            clearable
            style="width: 150px"
            @change="load"
          >
            <el-option label="排队中" value="pending" />
            <el-option label="已送达" value="delivered" />
            <el-option label="已失败" value="failed" />
          </el-select>
        </div>
      </template>
      <el-table :data="rows" empty-text="还没有投递记录">
        <el-table-column prop="event" label="事件" width="190" />
        <el-table-column prop="seq" label="序号" width="80" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              :type="
                row.status === 'delivered'
                  ? 'success'
                  : row.status === 'failed'
                    ? 'danger'
                    : 'warning'
              "
              size="small"
            >
              {{
                row.status === 'delivered'
                  ? '已送达'
                  : row.status === 'failed'
                    ? '已失败'
                    : '排队中'
              }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="attempts" label="尝试" width="80" />
        <el-table-column prop="status_code" label="HTTP" width="90" />
        <el-table-column prop="error" label="错误" min-width="180" />
        <el-table-column prop="next_attempt_at" label="下次重试" width="180" />
        <el-table-column prop="created_at" label="创建时间" width="180" />
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canWrite && row.status !== 'pending'"
              link
              type="primary"
              @click="onRedeliver(row)"
            >
              重发
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      :model-value="secretVisible"
      title="新签名密钥（只显示这一次）"
      width="560"
      @close="secretVisible = false"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="接收端必须立刻换成新密钥，旧密钥已失效。"
      />
      <div class="secret">{{ secret?.secret }}</div>
      <template #footer>
        <el-button type="primary" @click="secret && copy(secret.secret)">复制</el-button>
        <el-button @click="secretVisible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.aics-card {
  margin-bottom: 16px;
}
.actions {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.result {
  color: var(--aics-text-3, #8c8c8c);
  font-size: 13px;
}
.table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.secret {
  margin-top: 12px;
  padding: 12px;
  background: #141414;
  color: #e6f4ff;
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
</style>
