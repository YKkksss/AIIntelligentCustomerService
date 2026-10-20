/** 配置表单的「只提交改动字段」（M4.2 ④）。
 *
 * 后端 PATCH 会把传进来的每个键都写一遍，未改动的键也会触发审计与版本递增；
 * 所以前端在提交前用本函数把 `form` 与加载时的快照做一次深比较，只保留真正变化的叶子。
 */

export type JsonObject = Record<string, unknown>

function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function sameLeaf(before: unknown, next: unknown): boolean {
  return JSON.stringify(before) === JSON.stringify(next)
}

export function changedFields(base: unknown, next: unknown): JsonObject {
  const patch: JsonObject = {}
  if (!isObject(next)) return patch
  for (const [key, value] of Object.entries(next)) {
    const before = isObject(base) ? base[key] : undefined
    if (isObject(value) && isObject(before)) {
      const nested = changedFields(before, value)
      if (Object.keys(nested).length > 0) patch[key] = nested
    } else if (!sameLeaf(before, value)) {
      patch[key] = value
    }
  }
  return patch
}
