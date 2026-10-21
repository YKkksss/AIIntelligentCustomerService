import { describe, expect, it } from 'vitest'
import {
  AVATAR_STYLES,
  DEFAULT_AVATAR_STYLE,
  avatarDataUri,
  isAvatarStyle,
  normalizeAvatar,
  randomSeed,
} from '@/utils/avatar'

describe('normalizeAvatar', () => {
  it('缺字段时用默认风格，并用 stableSeed 兜底', () => {
    expect(normalizeAvatar(null, 'app-1')).toEqual({
      style: DEFAULT_AVATAR_STYLE,
      seed: 'app-1',
      url: null,
    })
    expect(normalizeAvatar({}, 'app-1').seed).toBe('app-1')
  })

  it('保留已存的风格、seed 与 url', () => {
    expect(normalizeAvatar({ style: 'pixelArt', seed: 'abc', url: 'https://a/b.png' })).toEqual({
      style: 'pixelArt',
      seed: 'abc',
      url: 'https://a/b.png',
    })
  })

  it('未知风格回落到默认，而不是把脏值传给 DiceBear', () => {
    expect(normalizeAvatar({ style: 'gone', seed: 's' }).style).toBe(DEFAULT_AVATAR_STYLE)
    expect(normalizeAvatar({ style: null, seed: 's' }).style).toBe(DEFAULT_AVATAR_STYLE)
  })

  it('空白 seed 视为没有', () => {
    expect(normalizeAvatar({ seed: '   ' }, 'fallback').seed).toBe('fallback')
  })
})

describe('isAvatarStyle', () => {
  it('只认白名单里的五个', () => {
    for (const style of AVATAR_STYLES) expect(isAvatarStyle(style)).toBe(true)
    expect(isAvatarStyle('personas')).toBe(false)
    expect(isAvatarStyle(undefined)).toBe(false)
  })
})

describe('randomSeed', () => {
  it('每次不一样，且是 8 位安全字符', () => {
    const a = randomSeed()
    const b = randomSeed()
    expect(a).toMatch(/^[0-9a-f]{8}$/)
    expect(a).not.toBe(b)
  })
})

describe('avatarDataUri', () => {
  it('同参数只生成一次，输出 svg data URI', async () => {
    const avatar = normalizeAvatar({ style: 'notionists', seed: 'seed-1' })
    const first = await avatarDataUri(avatar, 40)
    const second = await avatarDataUri(avatar, 40)
    expect(first.startsWith('data:image/svg+xml')).toBe(true)
    expect(second).toBe(first)
  })

  it('换 seed 就换脸', async () => {
    const a = await avatarDataUri(normalizeAvatar({ seed: 'seed-1' }), 40)
    const b = await avatarDataUri(normalizeAvatar({ seed: 'seed-2' }), 40)
    expect(a).not.toBe(b)
  })
})
