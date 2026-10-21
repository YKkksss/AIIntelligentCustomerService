import { http } from '@/api/http'

/** 统计接口（M4.6 ②）：`/admin/stats/overview` 与 `/admin/apps/{app_id}/stats`。
 *
 * 日聚合表 `app_daily_stats` 按 `Asia/Shanghai` 切日，只统计 `api` / `openai` 两个渠道。
 * 分位数不跨日汇总，所以 `series` 里逐日给出、`totals` 只累加 17 个计数列。
 */

export interface StatsTotals {
  conversations: number
  messages: number
  answered: number
  refused: number
  handoff: number
  errors: number
  handoff_requested: number
  handoff_accepted: number
  handoff_released: number
  handoff_queue_timeout: number
  handoff_offline: number
  handoff_accepted_idle_closed: number
  human_agent_messages: number
  idle_closed: number
  negative_feedback: number
  tokens_in: number
  tokens_out: number
}

export interface DailyRow extends StatsTotals {
  date: string
  handoff_accept_p50_s: number | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
  first_token_p95_ms: number | null
  intent_distribution: Record<string, number>
}

export interface AppTotals extends StatsTotals {
  app_id: string
  name: string
}

export interface AppStats {
  app_id: string
  days: number
  since: string
  until: string
  items: DailyRow[]
}

/** 平台视角的逐日序列：后端已按日把各应用相加，只含 17 个计数列（不含分位数）。 */
export type DailyPoint = { date: string } & StatsTotals

export interface StatsOverview {
  days: number
  since: string
  until: string
  totals: StatsTotals
  apps: AppTotals[]
  series: DailyPoint[]
  intent_distribution: Record<string, number>
}

export const statsApi = {
  async overview(days = 7): Promise<StatsOverview> {
    const { data } = await http.get<StatsOverview>('/admin/stats/overview', { params: { days } })
    return data
  },
  async app(appId: string, days = 7): Promise<AppStats> {
    const { data } = await http.get<AppStats>(`/admin/apps/${appId}/stats`, {
      params: { days },
    })
    return data
  },
}
