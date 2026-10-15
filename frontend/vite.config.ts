import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时 /api、/healthz、/readyz 代理到本机后端（uv run aics-api，端口 8000）
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
      '/readyz': 'http://localhost:8000',
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
