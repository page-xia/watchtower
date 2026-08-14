import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { inspectAttr } from 'kimi-plugin-inspect-react'

// 后端地址可用 WATCH_BACKEND 覆盖（默认对接本地盯盘后端 8788）
const backend = process.env.WATCH_BACKEND || 'http://127.0.0.1:8788'

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [inspectAttr(), react()],
  server: {
    port: 7100,
    host: true,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
      },
      '/ws': {
        target: backend.replace(/^http/, 'ws'),
        ws: true,
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
