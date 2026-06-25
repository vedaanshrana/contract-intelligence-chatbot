import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// In dev, the React app runs on :5173 and the FastAPI backend on :8000.
// Proxy /api to the backend so the frontend can use relative URLs that also
// work in production (where the SPA is served from the same origin as the API).
//
// base:
//   - '/'                 (default) — FastAPI serves the build at the origin
//     root (python server.py → http://127.0.0.1:8000), and the Vite dev server.
//   - '/MASTER_CHATBOT/'  — only for GitHub Pages, where the app lives under
//     /<repo-name>/. Build with `GH_PAGES=1 npm run build` for that case.
// Setting the GH Pages base unconditionally would break the FastAPI build
// (assets would 404), so it's gated behind the GH_PAGES env var.
export default defineConfig({
  base: process.env.GH_PAGES ? '/MASTER_CHATBOT/' : '/',
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
