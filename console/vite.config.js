// ND-1: dev-only proxy so `npm run dev` (console on its own Vite port) can
// talk to the FastAPI backend without a CORS story -- the built app
// (`npm run build` -> console/dist/) is served SAME-ORIGIN by alphaos/api
// itself (mounted at "/"), so this proxy never applies in production; see
// alphaos/api/app.py.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5601,
    proxy: {
      '/api': 'http://127.0.0.1:8601',
    },
  },
  build: {
    outDir: 'dist',
  },
});
