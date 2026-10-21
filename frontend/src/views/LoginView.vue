<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Service } from '@element-plus/icons-vue'
import ReadyStatus from '@/components/ReadyStatus.vue'
import { useAuthStore } from '@/stores/auth'

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
    <!-- 设计稿 01 画板：左侧 620px 品牌面、右侧 400×457 登录卡 -->
    <section class="brand-panel">
      <span class="circle circle-1" />
      <span class="circle circle-2" />

      <div class="brand-mark">
        <el-icon><Service /></el-icon>
      </div>
      <h1 class="brand-title">让每个业务场景，<br />都有自己的智能客服</h1>
      <p class="brand-sub">上传知识库，只按资料回答并给出引用；答不了就拒答或转人工。</p>
      <div class="brand-tags">
        <span>混合检索</span>
        <span>引用溯源</span>
        <span>可评测</span>
      </div>
    </section>

    <section class="form-panel">
      <div class="card">
        <div class="card-head">
          <span class="card-mark"
            ><el-icon><Service /></el-icon
          ></span>
          <h2>AICS 管理后台</h2>
        </div>
        <p class="card-sub">知识库 AI 客服平台 · 请登录你的账号</p>

        <form @submit.prevent="onSubmit">
          <label class="field-label" for="login-email">账号</label>
          <el-input
            id="login-email"
            v-model="form.email"
            placeholder="admin@aics.local"
            autocomplete="username"
            data-test="email"
          />

          <label class="field-label" for="login-password">密码</label>
          <el-input
            id="login-password"
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
            data-test="password"
          />

          <div class="card-row">
            <el-tooltip content="令牌只保存在当前会话，关闭浏览器即失效" placement="top">
              <span class="remember"><el-checkbox disabled size="small" />7 天内自动登录</span>
            </el-tooltip>
            <el-tooltip content="请联系管理员重置密码" placement="top">
              <span class="forgot">忘记密码？</span>
            </el-tooltip>
          </div>

          <p v-if="message" class="alert">{{ message }}</p>

          <el-button class="submit" type="primary" native-type="submit" :loading="pending">
            登 录
          </el-button>
        </form>

        <p class="card-foot">© 2026 AICS · 企业内部智能客服平台</p>
        <ReadyStatus />
      </div>
    </section>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  min-height: 100vh;
  background: var(--aics-layout);
}

.brand-panel {
  position: relative;
  flex: 0 0 620px;
  width: 620px;
  padding: 214px 80px 0;
  box-sizing: border-box;
  background: var(--aics-primary);
  color: #fff;
  overflow: hidden;
}
.circle {
  position: absolute;
  border-radius: 50%;
  background: rgb(255 255 255 / 6%);
}
.circle-1 {
  width: 420px;
  height: 420px;
  left: -120px;
  top: -160px;
}
.circle-2 {
  width: 360px;
  height: 360px;
  left: 380px;
  top: 620px;
}

.brand-mark {
  width: 40px;
  height: 40px;
  border-radius: var(--aics-radius);
  background: rgb(255 255 255 / 15%);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
}
.brand-title {
  margin: 36px 0 0;
  font-size: 34px;
  line-height: 1.45;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.brand-sub {
  margin: 26px 0 0;
  max-width: 440px;
  font-size: 16px;
  line-height: 1.6;
  color: rgb(255 255 255 / 80%);
}
.brand-tags {
  display: flex;
  gap: 12px;
  margin-top: 42px;
}
.brand-tags span {
  height: 28px;
  padding: 0 12px;
  border-radius: 14px;
  background: rgb(255 255 255 / 15%);
  font-size: 13px;
  line-height: 28px;
}

.form-panel {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
}

.card {
  width: 400px;
  box-sizing: border-box;
  padding: 40px;
  background: var(--aics-white);
  border-radius: var(--aics-radius);
}

.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.card-mark {
  width: 32px;
  height: 32px;
  border-radius: var(--aics-radius);
  background: var(--aics-primary);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
}
.card-head h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: var(--aics-text);
}
.card-sub {
  margin: 20px 0 0;
  font-size: 14px;
  color: var(--aics-text-3);
}

.field-label {
  display: block;
  margin: 25px 0 8px;
  font-size: 14px;
  color: var(--aics-text-2);
}

.card-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 20px;
  font-size: 14px;
  color: var(--aics-text-2);
}
.remember {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: default;
}
.forgot {
  color: var(--aics-primary);
  cursor: default;
}

.alert {
  margin: 14px 0 0;
  padding: 6px 10px;
  border-radius: 4px;
  background: #fff2f0;
  color: var(--aics-error);
  font-size: 13px;
}

.submit {
  width: 100%;
  height: 40px;
  margin-top: 26px;
  font-size: 16px;
}

.card-foot {
  margin: 34px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--aics-text-3);
}

@media (max-width: 960px) {
  .brand-panel {
    display: none;
  }
}
</style>
