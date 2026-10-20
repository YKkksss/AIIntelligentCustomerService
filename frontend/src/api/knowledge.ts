import { http } from '@/api/http'

/** 管理端知识库接口（M4.3 ②③，前缀 `/api/v1/admin/apps/{app_id}`）。
 *
 * 后端这些路由用 `JSONResponse` 直接返回字典（没有 `response_model`），`npm run gen:api`
 * 生成的 `schema.d.ts` 里响应体是空的，所以类型先手写（ADR-0018 §3）。
 */

export type ReviewStatus = 'none' | 'flagged' | 'approved'

export interface PageBody<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface DocumentRow {
  id: string
  title: string
  source_type: 'file' | 'url'
  source_uri: string | null
  mime_type: string | null
  size_bytes: number | null
  status: string
  progress: number | null
  error: string | null
  chunk_count: number
  index_version: number
  tags: string[]
  created_at: string
  updated_at: string
}

/** 单文档切块（`GET /documents/{id}/chunks`，只含生效构建号）。 */
export interface DocumentChunk {
  id: string
  ordinal: number
  heading_path: string | null
  text: string
  tokens: number
  page_from: number | null
  page_to: number | null
  enabled: boolean
  review_status: ReviewStatus
  flag_reason: string | null
}

/** 管理端跨文档切块（`GET /chunks`，带文档归属与复核时间）。 */
export interface ChunkRow extends DocumentChunk {
  document_id: string
  document_title: string
  reviewed_at: string | null
}

export interface JobRow {
  id: string
  app_id: string | null
  type: string
  status: string
  progress: number | null
  target_id: string | null
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface ChunkPreview {
  ordinal: number
  heading_path: string | null
  text: string
  tokens: number
  parent_ordinal: number | null
}

/** 切块预览（`POST /documents/preview`，文档处理流水线 §3.1）。 */
export interface PreviewResult {
  total_chunks: number
  total_parents: number
  total_tokens: number
  chunks: ChunkPreview[]
}

/** 写接口应答（`write_body`）：上传 / URL 导入 / 替换共用。 */
export interface WriteResult {
  id: string
  title: string
  status: string
  progress: number | null
  duplicate: boolean
  unchanged: boolean
  job_id: string | null
}

export interface JobAck {
  job_id: string
  status: string
}

export interface DocumentQuery {
  status?: string
  q?: string
  page?: number
  page_size?: number
}

export interface ChunkQuery {
  document_id?: string
  review_status?: ReviewStatus
  enabled?: boolean
  page?: number
  page_size?: number
}

export interface JobQuery {
  type?: string
  status?: string
  page?: number
  page_size?: number
}

export interface UploadOptions {
  title?: string
  /** 切块配置覆盖（后端校验 `ChunkConfig`，非法值回 422 `loc=["chunk_config", …]`）。 */
  chunkConfig?: Record<string, unknown>
}

function endpoint(appId: string): string {
  return `/admin/apps/${appId}`
}

function multipart(
  file: File,
  options: UploadOptions = {},
  extra: Record<string, string> = {},
): FormData {
  const form = new FormData()
  form.append('file', file)
  if (options.title) form.append('title', options.title)
  if (options.chunkConfig) form.append('chunk_config', JSON.stringify(options.chunkConfig))
  for (const [key, value] of Object.entries(extra)) form.append(key, value)
  return form
}

export const knowledgeApi = {
  async listDocuments(appId: string, query: DocumentQuery = {}): Promise<PageBody<DocumentRow>> {
    const { data } = await http.get<PageBody<DocumentRow>>(`${endpoint(appId)}/documents`, {
      params: query,
    })
    return data
  },
  async document(appId: string, documentId: string): Promise<DocumentRow> {
    const { data } = await http.get<DocumentRow>(`${endpoint(appId)}/documents/${documentId}`)
    return data
  },
  async documentChunks(
    appId: string,
    documentId: string,
    query: { page?: number; page_size?: number } = {},
  ): Promise<PageBody<DocumentChunk>> {
    const { data } = await http.get<PageBody<DocumentChunk>>(
      `${endpoint(appId)}/documents/${documentId}/chunks`,
      { params: query },
    )
    return data
  },
  /** 跨文档切块查询：`review_status=flagged` 即待复核列表。 */
  async chunks(appId: string, query: ChunkQuery = {}): Promise<PageBody<ChunkRow>> {
    const { data } = await http.get<PageBody<ChunkRow>>(`${endpoint(appId)}/chunks`, {
      params: query,
    })
    return data
  },
  async jobs(appId: string, query: JobQuery = {}): Promise<PageBody<JobRow>> {
    const { data } = await http.get<PageBody<JobRow>>(`${endpoint(appId)}/jobs`, { params: query })
    return data
  },
  async upload(appId: string, file: File, options: UploadOptions = {}): Promise<WriteResult> {
    const { data } = await http.post<WriteResult>(
      `${endpoint(appId)}/documents`,
      multipart(file, options),
    )
    return data
  },
  async importUrl(
    appId: string,
    body: { url: string; title?: string },
    chunkConfig?: Record<string, unknown>,
  ): Promise<WriteResult> {
    const { data } = await http.post<WriteResult>(`${endpoint(appId)}/documents/url`, body, {
      params: chunkConfig ? { chunk_config: JSON.stringify(chunkConfig) } : undefined,
    })
    return data
  },
  async replaceFile(
    appId: string,
    documentId: string,
    file: File,
    options: UploadOptions & { resetChunkConfig?: boolean } = {},
  ): Promise<WriteResult> {
    const extra: Record<string, string> = options.resetChunkConfig
      ? { reset_chunk_config: 'true' }
      : {}
    const { data } = await http.put<WriteResult>(
      `${endpoint(appId)}/documents/${documentId}/file`,
      multipart(file, options, extra),
    )
    return data
  },
  async reindex(
    appId: string,
    documentId: string,
    body: { chunk_config?: Record<string, unknown>; reset_chunk_config?: boolean } = {},
  ): Promise<JobAck> {
    const { data } = await http.post<JobAck>(
      `${endpoint(appId)}/documents/${documentId}/reindex`,
      body,
    )
    return data
  },
  async patchDocument(
    appId: string,
    documentId: string,
    patch: { title?: string; metadata?: Record<string, unknown> },
  ): Promise<DocumentRow> {
    const { data } = await http.patch<DocumentRow>(
      `${endpoint(appId)}/documents/${documentId}`,
      patch,
    )
    return data
  },
  async remove(appId: string, documentId: string): Promise<JobAck> {
    const { data } = await http.delete<JobAck>(`${endpoint(appId)}/documents/${documentId}`)
    return data
  },
  /** 上传前预览：不落库、不写向量（`POST /documents/preview`）。 */
  async previewUpload(
    appId: string,
    file: File,
    options: UploadOptions = {},
  ): Promise<PreviewResult> {
    const { data } = await http.post<PreviewResult>(
      `${endpoint(appId)}/documents/preview`,
      multipart(file, options),
    )
    return data
  },
  async previewDocument(
    appId: string,
    documentId: string,
    body: { chunk_config?: Record<string, unknown> } = {},
  ): Promise<PreviewResult> {
    const { data } = await http.post<PreviewResult>(
      `${endpoint(appId)}/documents/${documentId}/preview`,
      body,
    )
    return data
  },
  /** 切块启停与复核：`review_status` 只接受 `approved`（M4.3 ③）。 */
  async patchChunk(
    appId: string,
    chunkId: string,
    patch: { enabled?: boolean; review_status?: 'approved' },
  ): Promise<ChunkRow> {
    const { data } = await http.patch<ChunkRow>(`${endpoint(appId)}/chunks/${chunkId}`, patch)
    return data
  },
}
