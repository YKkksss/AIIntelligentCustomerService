import { describe, expect, it } from 'vitest'

import { changedFields } from '@/utils/diff'

describe('changedFields', () => {
  it('returns an empty object when nothing changed', () => {
    const base = { name: '售后', rag: { top_k: 5 } }
    expect(changedFields(base, { name: '售后', rag: { top_k: 5 } })).toEqual({})
  })

  it('keeps only the changed leaves', () => {
    const patch = changedFields(
      { name: '售后', rag: { top_k: 5, threshold: 0.3 } },
      { name: '售前', rag: { top_k: 5, threshold: 0.5 } },
    )
    expect(patch).toEqual({ name: '售前', rag: { threshold: 0.5 } })
  })

  it('treats arrays as whole values', () => {
    expect(changedFields({ custom_dict: ['a'] }, { custom_dict: ['a', 'b'] })).toEqual({
      custom_dict: ['a', 'b'],
    })
  })

  it('ignores keys that only exist in the base', () => {
    expect(changedFields({ name: '售后', extra: 1 }, { name: '售后' })).toEqual({})
  })

  it('takes a new key whose base value is missing', () => {
    expect(changedFields({}, { description: '新描述' })).toEqual({ description: '新描述' })
  })

  it('compares json-ish leaves including null and false', () => {
    expect(changedFields({ a: null, b: true }, { a: null, b: false })).toEqual({ b: false })
  })
})
