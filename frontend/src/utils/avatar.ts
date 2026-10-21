/** 智能客服头像：后端只存 `{style, seed, url}`，SVG 全在浏览器里生成。 */
import type { Style } from '@dicebear/core'

export const AVATAR_STYLES = ['notionists', 'lorelei', 'openPeeps', 'thumbs', 'pixelArt'] as const
export type AvatarStyle = (typeof AVATAR_STYLES)[number]

export const DEFAULT_AVATAR_STYLE: AvatarStyle = 'notionists'

export const STYLE_LABELS: Record<AvatarStyle, string> = {
  notionists: '线稿（默认）',
  lorelei: '柔和插画',
  openPeeps: '手绘小人',
  thumbs: '简笔表情',
  pixelArt: '像素',
}

export interface AvatarSetting {
  style: AvatarStyle
  seed: string
  url: string | null
}

/** 接口原样返回的松散结构（`style` 是 string，历史数据可能是任意值）。 */
export interface AvatarInput {
  style?: string | null
  seed?: string | null
  url?: string | null
}

/** 归一：缺字段用默认值；`seed` 为空时回落到调用方给的 stableSeed（通常是应用 id）。 */
export function normalizeAvatar(
  raw: AvatarInput | null | undefined,
  stableSeed = '',
): AvatarSetting {
  return {
    style: isAvatarStyle(raw?.style) ? raw.style : DEFAULT_AVATAR_STYLE,
    seed: raw?.seed?.trim() ? raw.seed.trim() : stableSeed,
    url: raw?.url ?? null,
  }
}

export function isAvatarStyle(value: unknown): value is AvatarStyle {
  return typeof value === 'string' && (AVATAR_STYLES as readonly string[]).includes(value)
}

/** 换一个头像：8 位随机串足够避免重复，又不至于长得像 UUID。 */
export function randomSeed(): string {
  const uuid =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`
  return uuid.replaceAll('-', '').slice(0, 8)
}

type AnyStyle = Style<Record<string, unknown>>

/** 每种风格单独一个 chunk：用到哪种才下载哪种（五套素材合计 ~1 MB，不能进首屏）。 */
const LOADERS: Record<AvatarStyle, () => Promise<AnyStyle>> = {
  notionists: async () => (await import('@dicebear/collection')).notionists as unknown as AnyStyle,
  lorelei: async () => (await import('@dicebear/collection')).lorelei as unknown as AnyStyle,
  openPeeps: async () => (await import('@dicebear/collection')).openPeeps as unknown as AnyStyle,
  thumbs: async () => (await import('@dicebear/collection')).thumbs as unknown as AnyStyle,
  pixelArt: async () => (await import('@dicebear/collection')).pixelArt as unknown as AnyStyle,
}

const cache = new Map<string, string>()

/** 生成 `data:image/svg+xml` 地址；相同 style+seed+size 只算一次。 */
export async function avatarDataUri(avatar: AvatarSetting, size = 96): Promise<string> {
  const { style, seed } = avatar
  const key = `${style}:${seed}:${size}`
  const hit = cache.get(key)
  if (hit) return hit
  const { createAvatar } = await import('@dicebear/core')
  const definition = await LOADERS[style]()
  const uri = createAvatar(definition, { seed, size }).toDataUri()
  cache.set(key, uri)
  return uri
}
