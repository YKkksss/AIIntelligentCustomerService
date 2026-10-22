import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({
  http: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/api/http'
import { kbApi } from '@/api/knowledge-bases'

const get = vi.mocked(http.get)
const post = vi.mocked(http.post)
const patch = vi.mocked(http.patch)
const put = vi.mocked(http.put)
const del = vi.mocked(http.delete)

const BASE = '/admin/knowledge-bases'

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } } as never)
  post.mockResolvedValue({ data: {} } as never)
  patch.mockResolvedValue({ data: {} } as never)
  put.mockResolvedValue({ data: { items: [] } } as never)
  del.mockResolvedValue({ data: {} } as never)
})

describe('公共知识库 api 与后端一致（M4.7 ③⑤）', () => {
  it('列表：路径、分页与筛选项（空值不下发）', async () => {
    await kbApi.list({ q: ' 售后 ', status: 'active', page: 2, pageSize: 50 })
    expect(get.mock.calls[0]).toEqual([
      BASE,
      { params: { q: ' 售后 ', status: 'active', page: 2, page_size: 50 } },
    ])

    await kbApi.list()
    expect(get.mock.calls[1]).toEqual([
      BASE,
      { params: { q: undefined, status: undefined, page: 1, page_size: 20 } },
    ])
  })

  it('详情 / 创建 / 改名 / 删除', async () => {
    await kbApi.detail('kb-1')
    expect(get.mock.calls[0]).toEqual([`${BASE}/kb-1`])

    await kbApi.create({ name: '售后政策库', description: '退货退款' })
    expect(post.mock.calls[0]).toEqual([BASE, { name: '售后政策库', description: '退货退款' }])

    await kbApi.update('kb-1', { name: '新名字', status: 'disabled' })
    expect(patch.mock.calls[0]).toEqual([`${BASE}/kb-1`, { name: '新名字', status: 'disabled' }])

    await kbApi.remove('kb-1')
    expect(del.mock.calls[0]).toEqual([`${BASE}/kb-1`])
  })

  it('库内文档：走 /documents 子路径并带分页', async () => {
    get.mockResolvedValue({ data: { items: [], total: 0, page: 3, page_size: 100 } } as never)
    const body = await kbApi.documents('kb-1', { page: 3, pageSize: 100 })
    expect(get.mock.calls[0]).toEqual([
      `${BASE}/kb-1/documents`,
      { params: { page: 3, page_size: 100 } },
    ])
    expect(body.page_size).toBe(100)
  })

  it('客服关联：GET 取关联、PUT 整体替换（含默认库）', async () => {
    await kbApi.appLinks('app-1')
    expect(get.mock.calls[0]).toEqual(['/admin/apps/app-1/knowledge-bases'])

    await kbApi.setAppLinks('app-1', { kb_ids: ['kb-1', 'kb-2'], default_kb_id: 'kb-2' })
    expect(put.mock.calls[0]).toEqual([
      '/admin/apps/app-1/knowledge-bases',
      { kb_ids: ['kb-1', 'kb-2'], default_kb_id: 'kb-2' },
    ])
  })
})
