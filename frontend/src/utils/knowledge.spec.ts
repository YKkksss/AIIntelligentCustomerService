import { describe, expect, it } from 'vitest'
import {
  documentTagType,
  excerpt,
  formatBytes,
  formatChunkConfig,
  parseChunkConfig,
  reviewTagType,
} from '@/utils/knowledge'

describe('切块配置文本', () => {
  it('空文本表示跟随应用配置', () => {
    expect(parseChunkConfig('   ')).toEqual({})
    expect(parseChunkConfig('')).toEqual({})
  })

  it('解析合法 JSON 对象', () => {
    expect(parseChunkConfig('{"chunk_size":500,"chunk_overlap":50}')).toEqual({
      value: { chunk_size: 500, chunk_overlap: 50 },
    })
  })

  it('非法 JSON 与数组都报错', () => {
    expect(parseChunkConfig('{chunk_size:').error).toContain('不是合法 JSON')
    expect(parseChunkConfig('[1,2]').error).toContain('必须是 JSON 对象')
    expect(parseChunkConfig('"text"').error).toContain('必须是 JSON 对象')
  })

  it('回填用两空格缩进，空配置回空串', () => {
    expect(formatChunkConfig({ chunk_size: 500 })).toBe('{\n  "chunk_size": 500\n}')
    expect(formatChunkConfig({})).toBe('')
    expect(formatChunkConfig(null)).toBe('')
  })
})

describe('状态标签', () => {
  it('文档状态', () => {
    expect(documentTagType('done')).toBe('success')
    expect(documentTagType('failed')).toBe('danger')
    expect(documentTagType('deleting')).toBe('info')
    expect(documentTagType('processing')).toBe('warning')
    expect(documentTagType('queued')).toBe('warning')
  })

  it('复核状态', () => {
    expect(reviewTagType('approved')).toBe('success')
    expect(reviewTagType('flagged')).toBe('warning')
    expect(reviewTagType('none')).toBe('info')
  })
})

describe('展示辅助', () => {
  it('体积', () => {
    expect(formatBytes(null)).toBe('-')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })

  it('摘要折叠空白并截断', () => {
    expect(excerpt('  a\n\nb  ')).toBe('a b')
    const long = '甲'.repeat(10)
    expect(excerpt(long, 4)).toBe('甲甲甲甲…')
  })
})
