import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  // Served by nginx at /app/ alongside the existing legacy static site
  // (see nginx/default.conf.template's `location ^~ /app/` block and
  // frontend/Dockerfile's frontend-app-build stage) -- every built asset
  // URL and the client-side router's basename (see src/App.tsx) both need
  // to agree with this.
  base: '/app/',
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      // Lets `npm run dev` talk to a locally running backend without
      // needing VITE_API_BASE_URL set or the backend's CORS list touched
      // -- same-origin from the browser's point of view, exactly like the
      // production nginx /api/ proxy.
      '/api': {
        target: process.env.VITE_DEV_API_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
