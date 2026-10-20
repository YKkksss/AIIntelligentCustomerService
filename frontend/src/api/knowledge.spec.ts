import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({
  http: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/api/http'
import { knowledgeApi } from '@/api/knowledge'

const get = vi.mocked(http.get)
const post = vi.mocked(http.post)
const patch = vi.mocked(http.patch)
const put = vi.mocked(http.put)
const del = vi.mocked(http.delete)

const appId = 'app-1'
const base = `/admin/apps/${appId}`

function file(name = 'manual.md'): File {
  return new File(['# 标题\n正文'], name, { type: 'text/markdown' })
}

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue({ data: {} } as never)
  post.mockResolvedValue({ data: {} } as never)
  patch.mockResolvedValue({ data: {} } as never)
  put.mockResolvedValue({ data: {} } as never)
  del.mockResolvedValue({ data: {} } as never)
})

describe('knowledge api 与后端管理接口一一对应', () => {
  it('读取：列表、详情、切块、跨文档切块与任务', async () => {
    await knowledgeApi.listDocuments(appId, { status: 'done', q: '手', page: 2, page_size: 10 })
    expect(get.mock.calls[0]).toEqual([
      `${base}/documents`,
      { params: { status: 'done', q: '手', page: 2, page_size: 10 } },
    ])

    await knowledgeApi.document(appId, 'doc-1')
    expect(get.mock.calls[1]).toEqual([`${base}/documents/doc-1`])

    await knowledgeApi.documentChunks(appId, 'doc-1', { page: 1, page_size: 20 })
    expect(get.mock.calls[2]).toEqual([
      `${base}/documents/doc-1/chunks`,
      { params: { page: 1, page_size: 20 } },
    ])

    await knowledgeApi.chunks(appId, { review_status: 'flagged' })
    expect(get.mock.calls[3]).toEqual([`${base}/chunks`, { params: { review_status: 'flagged' } }])

    await knowledgeApi.jobs(appId, { type: 'ingest' })
    expect(get.mock.calls[4]).toEqual([`${base}/jobs`, { params: { type: 'ingest' } }])
  })

  it('上传：multipart 带 file / title / chunk_config', async () => {
    await knowledgeApi.upload(appId, file(), {
      title: '操作手册',
      chunkConfig: { chunk_size: 500, chunk_overlap: 50 },
    })
    expect(post.mock.calls[0]?.[0]).toBe(`${base}/documents`)
    const form = post.mock.calls[0]?.[1] as FormData
    expect(form).toBeInstanceOf(FormData)
    expect((form.get('file') as File).name).toBe('manual.md')
    expect(form.get('title')).toBe('操作手册')
    expect(JSON.parse(String(form.get('chunk_config')))).toEqual({
      chunk_size: 500,
      chunk_overlap: 50,
    })
  })

  it('上传：没给可选字段时不写空表单项（后端把空串当值）', async () => {
    await knowledgeApi.upload(appId, file())
    const form = post.mock.calls[0]?.[1] as FormData
    expect(form.has('title')).toBe(false)
    expect(form.has('chunk_config')).toBe(false)
  })

  it('URL 导入把切块配置放在查询串', async () => {
    await knowledgeApi.importUrl(appId, { url: 'https://example.com/a.md' }, { chunk_size: 300 })
    expect(post.mock.calls[0]).toEqual([
      `${base}/documents/url`,
      { url: 'https://example.com/a.md' },
      { params: { chunk_config: '{"chunk_size":300}' } },
    ])
  })

  it('写操作：替换、重建、改标题、删除与切块启停', async () => {
    await knowledgeApi.replaceFile(appId, 'doc-1', file('new.pdf'), { resetChunkConfig: true })
    const form = put.mock.calls[0]?.[1] as FormData
    expect(put.mock.calls[0]?.[0]).toBe(`${base}/documents/doc-1/file`)
    expect(form.get('reset_chunk_config')).toBe('true')

    await knowledgeApi.reindex(appId, 'doc-1', { chunk_config: { chunk_size: 400 } })
    expect(post.mock.calls[0]).toEqual([
      `${base}/documents/doc-1/reindex`,
      { chunk_config: { chunk_size: 400 } },
    ])

    await knowledgeApi.patchDocument(appId, 'doc-1', { title: '新标题' })
    expect(patch.mock.calls[0]).toEqual([`${base}/documents/doc-1`, { title: '新标题' }])

    await knowledgeApi.remove(appId, 'doc-1')
    expect(del.mock.calls[0]?.[0]).toBe(`${base}/documents/doc-1`)

    await knowledgeApi.patchChunk(appId, 'chunk-1', { review_status: 'approved' })
    expect(patch.mock.calls[1]).toEqual([`${base}/chunks/chunk-1`, { review_status: 'approved' }])
  })

  it('预览：上传预览与已入库文档预览都走 POST', async () => {
    await knowledgeApi.previewUpload(appId, file(), { chunkConfig: { chunk_size: 200 } })
    const form = post.mock.calls[0]?.[1] as FormData
    expect(post.mock.calls[0]?.[0]).toBe(`${base}/documents/preview`)
    expect(JSON.parse(String(form.get('chunk_config')))).toEqual({ chunk_size: 200 })

    await knowledgeApi.previewDocument(appId, 'doc-1', { chunk_config: { chunk_size: 600 } })
    expect(post.mock.calls[1]).toEqual([
      `${base}/documents/doc-1/preview`,
      { chunk_config: { chunk_size: 600 } },
    ])
  })
})
