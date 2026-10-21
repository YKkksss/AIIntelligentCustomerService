import { describe, expect, it } from 'vitest'
import {
  conversationsOf,
  dateRange,
  datesOf,
  formatCount,
  formatPercent,
  intentLabel,
  intentRows,
  kpis,
  latencyOf,
  mergeIntents,
  rate,
  refusalRatesOf,
  shortDay,
  sumTotals,
  type DayTrend,
} from '@/utils/stats'
import type { DailyRow, StatsTotals } from '@/api/stats'

const TOTALS: StatsTotals = {
  conversations: 100,
  messages: 260,
  answered: 200,
  refused: 20,
  handoff: 12,
  errors: 1,
  handoff_requested: 12,
  handoff_accepted: 9,
  handoff_released: 6,
  handoff_queue_timeout: 2,
  handoff_offline: 1,
  handoff_accepted_idle_closed: 1,
  human_agent_messages: 40,
  idle_closed: 5,
  negative_feedback: 5,
  tokens_in: 1000,
  tokens_out: 2000,
}

function row(overrides: Partial<DailyRow> = {}): DailyRow {
  return {
    ...TOTALS,
    date: '2026-10-08',
    handoff_accept_p50_s: 12,
    latency_p50_ms: 800,
    latency_p95_ms: 2100,
    first_token_p95_ms: 2400,
    intent_distribution: { kb_qa: 3 },
    ...overrides,
  }
}

describe('rate / format', () => {
  it('百分比保留一位小数', () => {
    expect(rate(1, 3)).toBe(33.3)
    expect(rate(0, 10)).toBe(0)
  })

  it('分母为 0 或非法时给 0 而不是 NaN', () => {
    expect(rate(3, 0)).toBe(0)
    expect(rate(Number.NaN, 10)).toBe(0)
  })

  it('格式化为带一位小数的百分数与千分位计数', () => {
    expect(formatPercent(6)).toBe('6.0%')
    expect(formatCount(12637)).toBe('12,637')
    expect(formatCount(-3)).toBe('-3')
  })
})

describe('日期区间', () => {
  it('取 MMDD 并拼区间', () => {
    expect(shortDay('2026-10-08')).toBe('10-08')
    expect(dateRange('2026-10-08', '2026-10-14')).toBe('10-08 ~ 10-14')
  })
})

describe('kpis', () => {
  it('四张卡的值与口径', () => {
    const cards = kpis(TOTALS)
    expect(cards.map((card) => card.key)).toEqual([
      'conversations',
      'refusal',
      'handoff',
      'downvote',
    ])
    expect(cards[0].value).toBe('100')
    expect(cards[1].value).toBe('9.1%') // 20 / (200 + 20)
    expect(cards[2].value).toBe('12.0%') // 12 / 100
    expect(cards[3].value).toBe('2.5%') // 5 / 200
  })
})

describe('趋势序列', () => {
  const rows: DayTrend[] = [
    { date: '2026-10-08', conversations: 10, answered: 8, refused: 2 },
    { date: '2026-10-09', conversations: 0, answered: 0, refused: 0 },
  ]

  it('X 轴取 MM-DD，数值按顺序', () => {
    expect(datesOf(rows)).toEqual(['10-08', '10-09'])
    expect(conversationsOf(rows)).toEqual([10, 0])
  })

  it('拒答率分母为作答 + 拒答', () => {
    expect(refusalRatesOf(rows)).toEqual([20, 0])
  })

  it('延迟保留 null（没有样本的日期断开）', () => {
    const series = latencyOf([
      row(),
      row({ date: '2026-10-09', latency_p50_ms: null, latency_p95_ms: null }),
    ])
    expect(series.p50).toEqual([800, null])
    expect(series.p95).toEqual([2100, null])
  })
})

describe('意图分布', () => {
  it('按次数降序、同数按名称、限制条数', () => {
    expect(intentRows({ kb_qa: 2, chitchat: 5, recall: 2 }, 2)).toEqual([
      { name: 'chitchat', count: 5 },
      { name: 'kb_qa', count: 2 },
    ])
  })

  it('未知意图名原样返回', () => {
    expect(intentLabel('kb_qa')).toBe('知识库问答')
    expect(intentLabel('custom')).toBe('custom')
  })

  it('合并多个应用的意图计数', () => {
    expect(
      mergeIntents([
        { intent_distribution: { kb_qa: 2 } },
        { intent_distribution: { kb_qa: 1, chitchat: 4 } },
      ]),
    ).toEqual({ kb_qa: 3, chitchat: 4 })
  })
})

describe('sumTotals', () => {
  it('逐列相加，忽略分位数', () => {
    const total = sumTotals([
      row(),
      row({ conversations: 1, answered: 1, refused: 0, negative_feedback: 0 }),
    ])
    expect(total.conversations).toBe(101)
    expect(total.answered).toBe(201)
    expect(total.negative_feedback).toBe(5)
    expect(total.tokens_in).toBe(2000)
  })

  it('空数组返回全零', () => {
    expect(sumTotals([]).conversations).toBe(0)
  })
})
