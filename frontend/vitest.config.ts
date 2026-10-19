import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 单元测试只跑逻辑层：工具函数、store、指令与请求层（`.vue` 的渲染覆盖交给里程碑的
// Playwright 冒烟，见 ADR-0018 §5）。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.ts'],
    restoreMocks: true,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.ts'],
      exclude: ['src/**/*.spec.ts', 'src/api/schema.d.ts', 'src/env.d.ts', 'src/main.ts'],
      reporter: ['text'],
      thresholds: { lines: 80, statements: 80 },
    },
  },
})