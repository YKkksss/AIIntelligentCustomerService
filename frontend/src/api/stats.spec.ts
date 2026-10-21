import { describe, expect, it, vi } from 'vitest'
import { http } from '@/api/http'
import { statsApi, type StatsOverview } from '@/api/stats'

vi.mock('@/api/http', () => ({ http: { get: vi.fn() } }))

const get = vi.mocked(http.get)

describe('statsApi', () => {
  it('overview 带上 days 查询参数', async () => {
    const body = { days: 14 } as StatsOverview
    get.mockResolvedValueOnce({ data: body } as never)

    await expect(statsApi.overview(14)).resolves.toBe(body)
    expect(get).toHaveBeenCalledWith('/admin/stats/overview', { params: { days: 14 } })
  })

  it('overview 默认近 7 天', async () => {
    get.mockResolvedValueOnce({ data: { days: 7 } } as never)

    await statsApi.overview()
    expect(get).toHaveBeenLastCalledWith('/admin/stats/overview', { params: { days: 7 } })
  })

  it('单应用统计走 /admin/apps/{app_id}/stats', async () => {
    get.mockResolvedValueOnce({ data: { app_id: 'a1', items: [] } } as never)

    const body = await statsApi.app('a1', 30)
    expect(body.app_id).toBe('a1')
    expect(get).toHaveBeenLastCalledWith('/admin/apps/a1/stats', { params: { days: 30 } })
  })
})
