import { describe, expect, it, vi } from 'vitest'

const get = vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} }))
const post = vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} }))
const patch = vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} }))
const remove = vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} }))

vi.mock('@/api/http', () => ({
  http: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    patch: (...args: unknown[]) => patch(...args),
    delete: (...args: unknown[]) => remove(...args),
  },
}))

const { keyApi } = await import('@/api/keys')
const { logApi, feedbackApi } = await import('@/api/logs')
const { webhookApi } = await import('@/api/webhooks')

const APP = 'app-1'

describe('keyApi', () => {
  it('列表与创建走 /admin/apps/{id}/keys', async () => {
    get.mockResolvedValueOnce({ data: { items: [] } })
    await keyApi.list(APP)
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/keys`)

    await keyApi.create(APP, { name: 'k', scopes: ['chat'] })
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/keys`, { name: 'k', scopes: ['chat'] })
  })

  it('改限额、吊销与示例', async () => {
    await keyApi.update(APP, 'k1', { rpm_limit: 5 })
    expect(patch).toHaveBeenCalledWith(`/admin/apps/${APP}/keys/k1`, { rpm_limit: 5 })
    await keyApi.remove(APP, 'k1')
    expect(remove).toHaveBeenCalledWith(`/admin/apps/${APP}/keys/k1`)
    await keyApi.example(APP, 'k1')
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/keys/k1/example`)
  })
})

describe('logApi', () => {
  it('会话筛选映射成后端参数名并丢弃空值', async () => {
    get.mockResolvedValueOnce({ data: { items: [], next_cursor: null } })
    await logApi.conversations(APP, {
      channels: 'api,openai',
      handoffStatus: 'pending',
      downvoted: true,
      q: '退款',
      limit: 20,
    })
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/conversations`, {
      params: {
        channel: 'api,openai',
        status: undefined,
        handoff_status: 'pending',
        close_reason: undefined,
        needs_callback: undefined,
        intent: undefined,
        downvoted: true,
        since: undefined,
        until: undefined,
        q: '退款',
        limit: 20,
        cursor: undefined,
      },
    })
  })

  it('详情、强制释放与待回访', async () => {
    await logApi.detail(APP, 'c1')
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/conversations/c1`)

    await logApi.release(APP, 'c1', '备注')
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/conversations/c1/release`, { note: '备注' })

    await logApi.callbacks(APP, true)
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/callbacks`, {
      params: { include_cleared: true },
    })

    await logApi.clearCallback(APP, 'c1')
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/callbacks/c1`, { note: null })
  })

  it('反馈列表与标记已处理', async () => {
    await feedbackApi.list(APP, { rating: -1, handled: false })
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/feedback`, {
      params: { rating: -1, handled: false, limit: undefined },
    })
    await feedbackApi.handle(APP, 'f1')
    expect(patch).toHaveBeenCalledWith(`/admin/apps/${APP}/feedback/f1`, { handled: true })
  })
})

describe('webhookApi', () => {
  it('配置、投递记录、重发、测试与轮换密钥', async () => {
    await webhookApi.config(APP)
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/webhook`)

    get.mockResolvedValueOnce({ data: { items: [] } })
    await webhookApi.deliveries(APP, { status: 'failed' })
    expect(get).toHaveBeenCalledWith(`/admin/apps/${APP}/webhook-deliveries`, {
      params: { status: 'failed', limit: undefined, before: undefined },
    })

    await webhookApi.redeliver(APP, 'd1')
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/webhook-deliveries/d1/redeliver`)
    await webhookApi.test(APP)
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/webhook/test`)
    await webhookApi.rotateSecret(APP)
    expect(post).toHaveBeenCalledWith(`/admin/apps/${APP}/webhook/secret`)
  })
})
