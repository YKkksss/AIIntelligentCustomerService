import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时 /api、/healthz、/readyz、/docs、/openapi.json 代理到本机后端（uv run aics-api，默认端口 8000）
// 端口被占用或落在 Windows 保留区间时，用 AICS_API_TARGET（配合 AICS_API_PORT）改指向。
const apiTarget = process.env.AICS_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': apiTarget,
      '/healthz': apiTarget,
      '/readyz': apiTarget,
      '/docs': apiTarget,
      '/openapi.json': apiTarget,
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
