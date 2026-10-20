/** 知识库页面的纯逻辑（M4.3 ④）。
 *
 * 放在 utils 里而不是组件内部，便于 Vitest 直接覆盖（ADR-0018 §4）。
 */

export type JsonObject = Record<string, unknown>

export interface ParsedConfig {
  value?: JsonObject
  error?: string
}

/** 切块配置文本框 → JSON 对象；空文本表示「跟随应用默认配置」（返回空对象）。 */
export function parseChunkConfig(raw: string): ParsedConfig {
  const text = raw.trim()
  if (!text) return {}
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch (e) {
    return { error: `不是合法 JSON：${(e as Error).message}` }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return { error: '必须是 JSON 对象，例如 {"chunk_size": 500}' }
  }
  return { value: value as JsonObject }
}

export function formatChunkConfig(config: JsonObject | null | undefined): string {
  if (!config || Object.keys(config).length === 0) return ''
  return JSON.stringify(config, null, 2)
}

type TagType = 'success' | 'warning' | 'info' | 'danger'

/** 文档状态：`done` 成功、处理中提示、失败危险、删除中信息。 */
export function documentTagType(status: string): TagType {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'deleting') return 'info'
  return 'warning'
}

export function reviewTagType(status: string): TagType {
  if (status === 'approved') return 'success'
  if (status === 'flagged') return 'warning'
  return 'info'
}

export function formatBytes(size: number | null | undefined): string {
  if (size === null || size === undefined) return '-'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`
}

/** 表格里的正文摘要：折叠空白并按字符截断（中文按字符算，不用 `slice` 以外的东西）。 */
export function excerpt(text: string, limit = 80): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length <= limit ? flat : `${flat.slice(0, limit)}…`
}
