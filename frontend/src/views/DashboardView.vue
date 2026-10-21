<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import ChartPanel from '@/components/ChartPanel.vue'
import { statsApi, type StatsOverview } from '@/api/stats'
import {
  conversationsOf,
  dateRange,
  datesOf,
  formatCount,
  intentLabel,
  intentRows,
  kpis,
  refusalRatesOf,
  type DayTrend,
} from '@/utils/stats'

/** 全局仪表盘（设计稿 07 画板）：KPI 四卡 + 对话量/拒答率趋势 + 应用对比与意图分布。 */
const days = ref(7)
const loading = ref(false)
const overview = ref<StatsOverview | null>(null)

/** 后端 `overview.series` 已按日把各应用相加，直接当趋势数据用。 */
const daily = computed<DayTrend[]>(() =>
  [...(overview.value?.series ?? [])]
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((row) => ({
      date: row.date,
      conversations: row.conversations,
      answered: row.answered,
      refused: row.refused,
    })),
)

const EMPTY_TOTALS = {
  conversations: 0,
  messages: 0,
  answered: 0,
  refused: 0,
  handoff: 0,
  errors: 0,
  handoff_requested: 0,
  handoff_accepted: 0,
  handoff_released: 0,
  handoff_queue_timeout: 0,
  handoff_offline: 0,
  handoff_accepted_idle_closed: 0,
  human_agent_messages: 0,
  idle_closed: 0,
  negative_feedback: 0,
  tokens_in: 0,
  tokens_out: 0,
}

const cards = computed(() => kpis(overview.value?.totals ?? EMPTY_TOTALS))

const spark = computed(() => conversationsOf(daily.value))
const sparkMax = computed(() => Math.max(1, ...spark.value))

const rangeText = computed(() =>
  overview.value ? dateRange(overview.value.since, overview.value.until) : '',
)

const axis = computed(() => datesOf(daily.value))

const conversationOption = computed(() => ({
  grid: { left: 8, right: 12, top: 16, bottom: 8, containLabel: true },
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: axis.value, axisTick: { show: false } },
  yAxis: { type: 'value', splitLine: { lineStyle: { color: '#F0F0F0' } } },
  series: [
    {
      type: 'bar',
      name: '对话量',
      barMaxWidth: 28,
      itemStyle: { color: '#1677FF', borderRadius: [4, 4, 0, 0] },
      data: conversationsOf(daily.value),
    },
  ],
}))

const refusalOption = computed(() => ({
  grid: { left: 8, right: 12, top: 16, bottom: 8, containLabel: true },
  tooltip: { trigger: 'axis', valueFormatter: (value: number) => `${value}%` },
  xAxis: { type: 'category', data: axis.value, axisTick: { show: false } },
  yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' } },
  series: [
    {
      type: 'bar',
      name: '拒答率',
      barMaxWidth: 28,
      itemStyle: { color: '#FAAD14', borderRadius: [4, 4, 0, 0] },
      data: refusalRatesOf(daily.value),
    },
  ],
}))

/** 应用对比：区间内各应用的对话量（横向柱，名字在左侧）。 */
const appsOption = computed(() => {
  const apps = [...(overview.value?.apps ?? [])].sort((a, b) => b.conversations - a.conversations)
  return {
    grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#F0F0F0' } } },
    yAxis: { type: 'category', data: apps.map((app) => app.name), axisTick: { show: false } },
    series: [
      {
        type: 'bar',
        name: '对话量',
        barMaxWidth: 18,
        itemStyle: { color: '#1677FF', borderRadius: [0, 4, 4, 0] },
        data: apps.map((app) => app.conversations),
      },
    ],
  }
})

const intents = computed(() => intentRows(overview.value?.intent_distribution ?? {}))
const intentMax = computed(() => Math.max(1, ...intents.value.map((item) => item.count)))

async function load() {
  loading.value = true
  try {
    overview.value = await statsApi.overview(days.value)
  } catch {
    // 拦截器已提示，这里只保留上一次成功的数据
  } finally {
    loading.value = false
  }
}

function exportCsv() {
  const header = ['date', 'conversations', 'answered', 'refused']
  const lines = [
    header.join(','),
    ...daily.value.map((row) => [row.date, row.conversations, row.answered, row.refused].join(',')),
  ]
  const blob = new Blob([`\uFEFF${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `aics-stats-${overview.value?.until ?? 'all'}.csv`
  link.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出当前区间的逐日数据')
}

onMounted(load)
watch(days, load)
</script>

<template>
  <div v-loading="loading" class="dashboard">
    <header class="page-head">
      <div>
        <h1 class="aics-page-title">仪表盘</h1>
        <p class="aics-page-subtitle">
          全部应用 · 近 {{ days }} 天 {{ rangeText ? `(${rangeText})` : '' }}
        </p>
      </div>
      <div class="head-actions">
        <el-select v-model="days" class="range-select" aria-label="时间范围">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="14" label="近 14 天" />
          <el-option :value="30" label="近 30 天" />
        </el-select>
        <el-button :disabled="!overview" @click="exportCsv">导出报表</el-button>
      </div>
    </header>

    <section class="kpi-row">
      <article v-for="card in cards" :key="card.key" class="kpi-card">
        <div class="kpi-label">{{ card.label }}</div>
        <div class="kpi-value">{{ card.value }}</div>
        <div class="kpi-hint">{{ card.hint }}</div>
        <div class="spark" aria-hidden="true">
          <span
            v-for="(value, index) in spark"
            :key="index"
            :style="{ height: `${Math.max(3, (value / sparkMax) * 28)}px` }"
          />
        </div>
      </article>
    </section>

    <section class="chart-row">
      <article class="aics-card chart-card">
        <header class="card-head">
          <h2>对话量趋势</h2>
          <span class="tech-tag">ECharts</span>
        </header>
        <ChartPanel :option="conversationOption" :height="240" />
      </article>
      <article class="aics-card chart-card">
        <header class="card-head">
          <h2>拒答率趋势</h2>
          <span class="tech-tag">ECharts</span>
        </header>
        <ChartPanel :option="refusalOption" :height="240" />
      </article>
    </section>

    <section class="chart-row">
      <article class="aics-card chart-card">
        <header class="card-head">
          <h2>应用对比</h2>
          <span class="hint">区间内对话量</span>
        </header>
        <ChartPanel :option="appsOption" :height="220" />
      </article>
      <article class="aics-card">
        <header class="card-head">
          <h2>意图分布</h2>
          <span class="hint">近 {{ days }} 天</span>
        </header>
        <ul v-if="intents.length" class="intent-list">
          <li v-for="item in intents" :key="item.name">
            <span class="intent-name">{{ intentLabel(item.name) }}</span>
            <span class="intent-bar">
              <span :style="{ width: `${(item.count / intentMax) * 100}%` }" />
            </span>
            <span class="intent-count">{{ formatCount(item.count) }}</span>
          </li>
        </ul>
        <p v-else class="empty">区间内还没有意图数据</p>
      </article>
    </section>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.range-select {
  width: 112px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}
.kpi-card {
  position: relative;
  background: var(--aics-white);
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  min-height: 126px;
}
.kpi-label {
  font-size: 13px;
  color: var(--aics-text3);
}
.kpi-value {
  margin-top: 8px;
  font-size: 28px;
  font-weight: 600;
  color: var(--aics-text);
  letter-spacing: -0.5px;
}
.kpi-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--aics-text3);
}
.spark {
  position: absolute;
  right: 16px;
  bottom: 14px;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 28px;
}
.spark span {
  width: 4px;
  border-radius: 1px;
  background: var(--aics-primary);
  opacity: 0.7;
}
.chart-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.chart-card {
  min-width: 0;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.card-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--aics-text);
}
.tech-tag {
  font-size: 11px;
  color: var(--aics-text3);
  border: 1px solid var(--aics-border);
  border-radius: 4px;
  padding: 1px 6px;
}
.hint {
  font-size: 12px;
  color: var(--aics-text3);
}
.intent-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.intent-list li {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr) 56px;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.intent-name {
  color: var(--aics-text2);
}
.intent-bar {
  height: 8px;
  border-radius: 4px;
  background: var(--aics-split);
  overflow: hidden;
}
.intent-bar span {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--aics-primary);
}
.intent-count {
  text-align: right;
  color: var(--aics-text2);
}
.empty {
  margin: 24px 0;
  text-align: center;
  font-size: 13px;
  color: var(--aics-text3);
}
@media (max-width: 1180px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .chart-row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
