<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { KEY_SCOPES, keyApi, type CreatedKey, type KeyExample, type KeyRow } from '@/api/keys'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()
const appId = computed(() => String(route.params.appId ?? auth.currentAppId ?? ''))

const rows = ref<KeyRow[]>([])
const loading = ref(false)
const canWrite = computed(() => auth.can('key.write'))

const createVisible = ref(false)
const creating = ref(false)
const form = reactive({
  name: '',
  scopes: ['chat'] as string[],
  rpm_limit: 60,
  daily_quota: null as number | null,
  expires_at: '',
  ip_allowlist: '',
})

const created = ref<CreatedKey | null>(null)
const editVisible = ref(false)
const editing = ref<KeyRow | null>(null)
const editForm = reactive({ rpm_limit: 60, daily_quota: null as number | null, ip_allowlist: '' })

const exampleVisible = ref(false)
const example = ref<KeyExample | null>(null)

async function load() {
  if (!appId.value) return
  loading.value = true
  try {
    rows.value = await keyApi.list(appId.value)
  } finally {
    loading.value = false
  }
}

function splitList(raw: string): string[] {
  return raw
    .split(/[,\s\n]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function onCreate() {
  creating.value = true
  try {
    const body = {
      name: form.name.trim(),
      scopes: form.scopes,
      rpm_limit: form.rpm_limit,
      daily_quota: form.daily_quota ?? null,
      expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null,
      ip_allowlist: splitList(form.ip_allowlist),
    }
    created.value = await keyApi.create(appId.value, body)
    createVisible.value = false
    ElMessage.success('已创建，明文只在本次显示')
    Object.assign(form, {
      name: '',
      scopes: ['chat'],
      rpm_limit: 60,
      daily_quota: null,
      expires_at: '',
      ip_allowlist: '',
    })
    await load()
  } finally {
    creating.value = false
  }
}

function openEdit(row: KeyRow) {
  editing.value = row
  editForm.rpm_limit = row.rpm_limit
  editForm.daily_quota = row.daily_quota
  editForm.ip_allowlist = row.ip_allowlist.join(', ')
  editVisible.value = true
}

async function onSaveEdit() {
  if (!editing.value) return
  await keyApi.update(appId.value, editing.value.id, {
    rpm_limit: editForm.rpm_limit,
    daily_quota: editForm.daily_quota,
    ip_allowlist: splitList(editForm.ip_allowlist),
  })
  ElMessage.success('已保存，鉴权缓存立即失效')
  editVisible.value = false
  await load()
}

async function onRevoke(row: KeyRow) {
  await ElMessageBox.confirm(`吊销后 ${row.prefix}… 立即失效且不可恢复，确定？`, '吊销 Key', {
    type: 'warning',
  })
  await keyApi.remove(appId.value, row.id)
  ElMessage.success('已吊销')
  await load()
}

async function onExample(row: KeyRow) {
  example.value = await keyApi.example(appId.value, row.id)
  exampleVisible.value = true
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许访问剪贴板，请手动复制')
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="head">
      <div>
        <div class="aics-page-title">API Key</div>
        <div class="aics-page-subtitle">
          给外部系统调用的凭证：明文只显示一次，可随时吊销或改限额。
        </div>
      </div>
      <el-button v-if="canWrite" type="primary" @click="createVisible = true"
        >创建 API Key</el-button
      >
    </div>

    <el-card class="aics-card">
      <el-table v-loading="loading" :data="rows" empty-text="还没有 Key">
        <el-table-column prop="name" label="名称" min-width="160" />
        <el-table-column label="前缀" width="150">
          <template #default="{ row }">{{ row.prefix }}…{{ row.hint }}</template>
        </el-table-column>
        <el-table-column label="范围" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="scope in row.scopes" :key="scope" size="small" class="tag">{{
              scope
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="限额" width="150">
          <template #default="{ row }">
            {{ row.rpm_limit }}/分钟<template v-if="row.daily_quota">
              · {{ row.daily_quota }}/天</template
            >
          </template>
        </el-table-column>
        <el-table-column label="最近使用" width="170">
          <template #default="{ row }">{{ row.last_used_at ?? '从未' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag
              :type="
                row.status === 'active' ? 'success' : row.status === 'revoked' ? 'info' : 'warning'
              "
              size="small"
            >
              {{
                row.status === 'active' ? '生效中' : row.status === 'revoked' ? '已吊销' : '已过期'
              }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onExample(row)">示例</el-button>
            <el-button v-if="canWrite" link type="primary" @click="openEdit(row)">改限额</el-button>
            <el-button
              v-if="canWrite && row.status !== 'revoked'"
              link
              type="danger"
              @click="onRevoke(row)"
            >
              吊销
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createVisible" title="创建 API Key" width="520">
      <el-form label-width="96">
        <el-form-item label="名称">
          <el-input v-model="form.name" maxlength="100" placeholder="例如 官网客服" />
        </el-form-item>
        <el-form-item label="权限范围">
          <el-checkbox-group v-model="form.scopes">
            <el-checkbox v-for="scope in KEY_SCOPES" :key="scope" :value="scope">{{
              scope
            }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="每分钟上限">
          <el-input-number v-model="form.rpm_limit" :min="1" />
        </el-form-item>
        <el-form-item label="每日配额">
          <el-input-number v-model="form.daily_quota" :min="1" placeholder="不限" />
        </el-form-item>
        <el-form-item label="过期时间">
          <el-date-picker
            v-model="form.expires_at"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
          />
        </el-form-item>
        <el-form-item label="IP 白名单">
          <el-input
            v-model="form.ip_allowlist"
            type="textarea"
            :rows="2"
            placeholder="逗号或换行分隔，留空表示不限"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="created !== null"
      title="Key 已创建（只显示这一次）"
      width="560"
      @close="created = null"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="离开后无法再次查看，请立即保存。"
      />
      <div class="secret">{{ created?.key }}</div>
      <template #footer>
        <el-button type="primary" @click="created && copy(created.key)">复制</el-button>
        <el-button @click="created = null">我已保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="调整限额与白名单" width="480">
      <el-form label-width="96">
        <el-form-item label="每分钟上限">
          <el-input-number v-model="editForm.rpm_limit" :min="1" />
        </el-form-item>
        <el-form-item label="每日配额">
          <el-input-number v-model="editForm.daily_quota" :min="1" placeholder="不限" />
        </el-form-item>
        <el-form-item label="IP 白名单">
          <el-input
            v-model="editForm.ip_allowlist"
            type="textarea"
            :rows="2"
            placeholder="留空表示不限"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="onSaveEdit">保存</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="exampleVisible" title="接入示例" size="620px">
      <template v-if="example">
        <div class="sub">占位符 <code>&lt;YOUR_API_KEY&gt;</code> 换成你的 Key 明文。</div>
        <h4>curl</h4>
        <pre>{{ example.curl }}</pre>
        <h4>Python</h4>
        <pre>{{ example.python }}</pre>
        <h4>JavaScript</h4>
        <pre>{{ example.javascript }}</pre>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.tag {
  margin-right: 4px;
}
.secret {
  margin-top: 12px;
  padding: 12px;
  background: var(--aics-code-bg, #141414);
  color: #e6f4ff;
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
.sub {
  color: var(--aics-text-3, #8c8c8c);
  font-size: 13px;
}
pre {
  padding: 12px;
  background: #141414;
  color: #e6f4ff;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
}
</style>
