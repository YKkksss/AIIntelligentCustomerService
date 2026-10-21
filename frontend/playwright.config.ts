import { defineConfig } from '@playwright/test'

/**
 * M4 里程碑级冒烟：真浏览器跑真实后端（默认 5174 前端 dev + 8010 后端）。
 * 需要先起 docker 栈、后端与前端，见 docs/06-development/02-开发环境搭建.md。
 * 未设置 `AICS_E2E=1` 时用例整体 skip，不拖慢常规 CI。
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.AICS_E2E_BASE_URL ?? 'http://localhost:5174',
    testIdAttribute: 'data-test',
    trace: 'retain-on-failure',
    viewport: { width: 1440, height: 900 },
  },
})
