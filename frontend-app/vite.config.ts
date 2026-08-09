import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  // Served by nginx at the site root ("/") -- this SPA ships in its own
  // standalone image (frontend/Dockerfile's `frontend-react-only` target,
  // paired with nginx/default.react.conf.template's SPA-fallback `location /`
  // block), not side-by-side with the legacy static site, so there's no
  // /app/ sub-path to reserve. Every built asset URL and the client-side
  // router's basename (see src/App.tsx) both need to agree with this.
  base: '/',
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
