/**
 * 公共知识库（M4.7）：库是平台级资源，路径里没有 app_id。
 *
 * 后端六个库接口 + 客服侧关联两条；管理后台的「知识库」页用前一组，
 * 智能客服的「关联知识库」用后一组。
 */
import { http } from '@/api/http'

export interface KBAppLink {
  app_id: string
  name: string
}

export interface KnowledgeBaseRow {
  id: string
  name: string
  description: string
  status: 'active' | 'disabled' | 'deleting'
  chunk_config: Record<string, unknown>
  config_version: number
  document_count: number
  chunk_count: number
  apps: KBAppLink[]
  created_by: string | null
  linked?: boolean | null
  is_default?: boolean | null
  created_at: string
  updated_at: string
}

export interface KnowledgeBasePage {
  items: KnowledgeBaseRow[]
  total: number
  page: number
  page_size: number
}

export interface KBDocumentRow {
  id: string
  title: string
  status: string
  source_type: string
  chunk_count: number
  app_id: string
  app_name: string
  effective_at: string | null
  expires_at: string | null
  created_at: string
  updated_at: string
}

export interface KBDocumentPage {
  items: KBDocumentRow[]
  total: number
  page: number
  page_size: number
}

export interface KBListQuery {
  q?: string
  status?: string
  page?: number
  pageSize?: number
}

export interface KBCreateBody {
  name: string
  description?: string
  chunk_config?: Record<string, unknown>
}

export interface KBPatchBody {
  name?: string
  description?: string
  chunk_config?: Record<string, unknown>
  status?: string
  expected_config_version?: number
}

const BASE = '/admin/knowledge-bases'

export const kbApi = {
  async list(query: KBListQuery = {}): Promise<KnowledgeBasePage> {
    const { data } = await http.get<KnowledgeBasePage>(BASE, {
      params: {
        q: query.q || undefined,
        status: query.status || undefined,
        page: query.page ?? 1,
        page_size: query.pageSize ?? 20,
      },
    })
    return data
  },

  async detail(id: string): Promise<KnowledgeBaseRow> {
    const { data } = await http.get<KnowledgeBaseRow>(`${BASE}/${id}`)
    return data
  },

  async create(body: KBCreateBody): Promise<KnowledgeBaseRow> {
    const { data } = await http.post<KnowledgeBaseRow>(BASE, body)
    return data
  },

  async update(id: string, body: KBPatchBody): Promise<KnowledgeBaseRow> {
    const { data } = await http.patch<KnowledgeBaseRow>(`${BASE}/${id}`, body)
    return data
  },

  async remove(id: string): Promise<void> {
    await http.delete(`${BASE}/${id}`)
  },

  async documents(
    id: string,
    query: { page?: number; pageSize?: number } = {},
  ): Promise<KBDocumentPage> {
    const { data } = await http.get<KBDocumentPage>(`${BASE}/${id}/documents`, {
      params: { page: query.page ?? 1, page_size: query.pageSize ?? 20 },
    })
    return data
  },

  async upload(
    id: string,
    file: File,
    options: { title?: string; chunkConfig?: string } = {},
  ): Promise<{ document_id: string; job_id: string }> {
    const form = new FormData()
    form.append('file', file)
    if (options.title) form.append('title', options.title)
    if (options.chunkConfig) form.append('chunk_config', options.chunkConfig)
    const { data } = await http.post<{ document_id: string; job_id: string }>(
      `${BASE}/${id}/documents`,
      form,
    )
    return data
  },

  async appLinks(appId: string): Promise<KnowledgeBaseRow[]> {
    const { data } = await http.get<{ items: KnowledgeBaseRow[] }>(
      `/admin/apps/${appId}/knowledge-bases`,
    )
    return data.items
  },

  async setAppLinks(
    appId: string,
    body: { kb_ids: string[]; default_kb_id?: string | null },
  ): Promise<KnowledgeBaseRow[]> {
    const { data } = await http.put<{ items: KnowledgeBaseRow[] }>(
      `/admin/apps/${appId}/knowledge-bases`,
      body,
    )
    return data.items
  },
}
