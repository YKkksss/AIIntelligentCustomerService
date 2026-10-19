// 生成管理后台的类型：先把后端的 OpenAPI 文档 dump 成 JSON，再用 openapi-typescript 的 API
// 生成 `src/api/schema.d.ts`（不依赖 npx，避免 Windows 上 `.cmd` 与 execFileSync 的兼容坑）。
//
// 注意：后端多数路由用 `JSONResponse` 直接返回字典（没有 `response_model`），因此生成文件里
// 这些响应体是空的；请求体与路径参数仍有价值。类型补齐的路线见 ADR-0018 §3。
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { readFileSync, writeFileSync } from 'node:fs'
import openapiTS, { astToString } from 'openapi-typescript'

const here = dirname(fileURLToPath(import.meta.url))
const frontend = resolve(here, '..')
const backend = resolve(frontend, '..', 'backend')
const dump = join(frontend, 'openapi.json')

const script = [
  'import json, sys',
  'from aics.main import create_app',
  'with open(sys.argv[1], "w", encoding="utf-8") as fh:',
  '    json.dump(create_app().openapi(), fh, ensure_ascii=False)',
].join('\n')

execFileSync('uv', ['run', '--frozen', 'python', '-c', script, dump], {
  cwd: backend,
  stdio: ['ignore', 'ignore', 'inherit'],
})

const ast = await openapiTS(JSON.parse(readFileSync(dump, "utf8")))
writeFileSync(join(frontend, 'src', 'api', 'schema.d.ts'), astToString(ast), 'utf8')
console.log('ok: src/api/schema.d.ts')
