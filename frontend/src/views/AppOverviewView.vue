<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ChartPanel from '@/components/ChartPanel.vue'
import { appApi } from '@/api/admin'
import { statsApi, type DailyRow } from '@/api/stats'
import {
  conversationsOf,
  dateRange,
  datesOf,
  formatCount,
  intentLabel,
  intentRows,
  kpis,
  latencyOf,
  mergeIntents,
  refusalRatesOf,
  sumTotals,
  type DayTrend,
} from '@/utils/stats'

/** 单应用概览（应用菜单「概览」）：与全局仪表盘同一套指标，但只含本应用。 */
const route = useRoute()
const appId = computed(() => String(route.params.appId))

const days = ref(7)
const loading = ref(false)
const appName = ref('')
const items = ref<DailyRow[]>([])
const since = ref('')
const until = ref('')

const rows = computed<DayTrend[]>(() =>
  items.value.map((row) => ({
    date: row.date,
    conversations: row.conversations,
    answered: row.answered,
    refused: row.refused,
  })),
)

const cards = computed(() => kpis(sumTotals(items.value)))
const intents = computed(() => intentRows(mergeIntents(items.value)))
const intentMax = computed(() => Math.max(1, ...intents.value.map((item) => item.count)))
const rangeText = computed(() =>
  since.value && until.value ? dateRange(since.value, until.value) : '',
)
const axis = computed(() => datesOf(rows.value))

const conversationOption = computed(() => ({
  grid: { left: 8, right: 12, top: 16, bottom: 8, containLabel: true },
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: axis.value, axisTick: { show: false } },
  yAxis: { type: 'value', splitLine: { lineStyle: { color: '#F0F0F0' } } },
  series: [
    {
      type: 'bar',
      name: '对话量',
      barMaxWidth: 26,
      itemStyle: { color: '#1677FF', borderRadius: [4, 4, 0, 0] },
      data: conversationsOf(rows.value),
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
      barMaxWidth: 26,
      itemStyle: { color: '#FAAD14', borderRadius: [4, 4, 0, 0] },
      data: refusalRatesOf(rows.value),
    },
  ],
}))

const latencyOption = computed(() => {
  const series = latencyOf(items.value)
  return {
    grid: { left: 8, right: 12, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: number | null) => (value === null ? '—' : `${value} ms`),
    },
    legend: { top: 0, right: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10 },
    xAxis: { type: 'category', data: axis.value, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { formatter: '{value} ms' } },
    series: [
      {
        type: 'line',
        name: 'P50',
        smooth: true,
        itemStyle: { color: '#1677FF' },
        data: series.p50,
      },
      {
        type: 'line',
        name: 'P95',
        smooth: true,
        itemStyle: { color: '#FAAD14' },
        data: series.p95,
      },
    ],
  }
})

async function load() {
  loading.value = true
  try {
    const [body, detail] = await Promise.all([
      statsApi.app(appId.value, days.value),
      appApi.detail(appId.value),
    ])
    items.value = body.items
    since.value = body.since
    until.value = body.until
    appName.value = detail.name
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch([appId, days], load)
</script>

<template>
  <div v-loading="loading" class="overview">
    <header class="page-head">
      <div>
        <h1 class="aics-page-title">概览</h1>
        <p class="aics-page-subtitle">
          {{ appName || '本智能客服' }} · 近 {{ days }} 天 {{ rangeText ? `(${rangeText})` : '' }}
        </p>
      </div>
      <el-select v-model="days" class="range-select" aria-label="时间范围">
        <el-option :value="7" label="近 7 天" />
        <el-option :value="14" label="近 14 天" />
        <el-option :value="30" label="近 30 天" />
      </el-select>
    </header>

    <section class="kpi-row">
      <article v-for="card in cards" :key="card.key" class="kpi-card">
        <div class="kpi-label">{{ card.label }}</div>
        <div class="kpi-value">{{ card.value }}</div>
        <div class="kpi-hint">{{ card.hint }}</div>
      </article>
    </section>

    <section class="chart-row">
      <article class="aics-card">
        <header class="card-head"><h2>对话量趋势</h2></header>
        <ChartPanel :option="conversationOption" :height="220" />
      </article>
      <article class="aics-card">
        <header class="card-head"><h2>拒答率趋势</h2></header>
        <ChartPanel :option="refusalOption" :height="220" />
      </article>
    </section>

    <section class="chart-row">
      <article class="aics-card">
        <header class="card-head"><h2>延迟趋势</h2></header>
        <ChartPanel :option="latencyOption" :height="200" />
      </article>
      <article class="aics-card">
        <header class="card-head"><h2>意图分布</h2></header>
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
.overview {
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
.range-select {
  width: 112px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}
.kpi-card {
  background: var(--aics-white);
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  min-height: 108px;
}
.kpi-label {
  font-size: 13px;
  color: var(--aics-text3);
}
.kpi-value {
  margin-top: 8px;
  font-size: 26px;
  font-weight: 600;
  color: var(--aics-text);
}
.kpi-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--aics-text3);
}
.chart-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
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
