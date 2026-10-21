import type { StatsTotals } from '@/api/stats'

/** 仪表盘用到的纯函数：比率、格式化、时间序列与意图分布。
 *
 * 放在这里而不是组件里，是为了让它们能被 Vitest 直接覆盖（设计稿 07 画板的
 * KPI 卡与两张趋势图都只依赖这些换算）。
 */

const COUNT_FORMAT = new Intl.NumberFormat('en-US')

export type KpiKey = 'conversations' | 'refusal' | 'handoff' | 'downvote'

export interface Kpi {
  key: KpiKey
  label: string
  value: string
  hint: string
}

/** 百分比，保留一位小数；分母为 0 时给 0（而不是 NaN）。 */
export function rate(part: number, whole: number): number {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) return 0
  return Math.round((part / whole) * 1000) / 10
}

export function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`
}

export function formatCount(value: number): string {
  return COUNT_FORMAT.format(Math.trunc(value || 0))
}

/** 四张 KPI 卡：对话量、拒答率、转人工率、点踩率（口径见 §2 文档）。 */
export function kpis(totals: StatsTotals): Kpi[] {
  const replies = totals.answered + totals.refused
  return [
    {
      key: 'conversations',
      label: '对话量',
      value: formatCount(totals.conversations),
      hint: '近 7 天新建会话',
    },
    {
      key: 'refusal',
      label: '拒答率',
      value: formatPercent(rate(totals.refused, replies)),
      hint: '拒答 / (作答 + 拒答)',
    },
    {
      key: 'handoff',
      label: '转人工率',
      value: formatPercent(rate(totals.handoff, totals.conversations)),
      hint: '转人工会话 / 全部会话',
    },
    {
      key: 'downvote',
      label: '点踩率',
      value: formatPercent(rate(totals.negative_feedback, totals.answered)),
      hint: '点踩 / 作答应答',
    },
  ]
}

/** `10-08 ~ 10-14`：设计稿副标题里的日期区间（`since` / `until` 为 YYYY-MM-DD）。 */
export function dateRange(since: string, until: string): string {
  return `${shortDay(since)} ~ ${shortDay(until)}`
}

export function shortDay(day: string): string {
  return day.slice(5)
}

/** 逐日聚合后的最小形状：趋势图与 KPI 只需要这几列。 */
export interface DayTrend {
  date: string
  conversations: number
  answered: number
  refused: number
}

/** 逐日延迟（当天没有样本时为 null，图上断开而不是画成 0）。 */
export interface DayLatency {
  date: string
  latency_p50_ms: number | null
  latency_p95_ms: number | null
}

/** 按日期升序的 X 轴刻度（缺日不补零，与后端 `series` 一致）。 */
export function datesOf(rows: DayTrend[]): string[] {
  return rows.map((row) => shortDay(row.date))
}

export function conversationsOf(rows: DayTrend[]): number[] {
  return rows.map((row) => row.conversations)
}

export function refusalRatesOf(rows: DayTrend[]): number[] {
  return rows.map((row) => rate(row.refused, row.answered + row.refused))
}

/** 延迟趋势：p50 / p95 都是可空的（当天没有样本时为 null，图上断开而不是画成 0）。 */
export function latencyOf(rows: DayLatency[]): { p50: (number | null)[]; p95: (number | null)[] } {
  return {
    p50: rows.map((row) => row.latency_p50_ms),
    p95: rows.map((row) => row.latency_p95_ms),
  }
}

export interface IntentRow {
  name: string
  count: number
}

/** 意图分布按次数降序，最多 `limit` 条。 */
export function intentRows(distribution: Record<string, number>, limit = 8): IntentRow[] {
  return Object.entries(distribution ?? {})
    .map(([name, count]) => ({ name, count: Number(count) || 0 }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
    .slice(0, limit)
}

const INTENT_LABELS: Record<string, string> = {
  kb_qa: '知识库问答',
  chitchat: '闲聊',
  human: '转人工',
  out_of_scope: '超出范围',
  action: '业务动作',
  clarify: '澄清',
  recall: '记忆回查',
  blocked: '拒答',
}

export function intentLabel(name: string): string {
  return INTENT_LABELS[name] ?? name
}

/** 单应用视图没有后端合计行，把逐日记录按计数列相加（分位数不汇总）。 */
export function sumTotals(rows: StatsTotals[]): StatsTotals {
  const total: StatsTotals = {
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
  for (const row of rows) {
    for (const key of Object.keys(total) as (keyof StatsTotals)[]) {
      total[key] += Number(row[key]) || 0
    }
  }
  return total
}

export function mergeIntents(
  rows: { intent_distribution: Record<string, number> }[],
): Record<string, number> {
  const merged: Record<string, number> = {}
  for (const row of rows) {
    for (const [name, count] of Object.entries(row.intent_distribution ?? {})) {
      merged[name] = (merged[name] ?? 0) + (Number(count) || 0)
    }
  }
  return merged
}
