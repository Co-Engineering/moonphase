import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
  plugins: [react()],
  // Read .env from the repo root, not apps/web. One .env configures the API
  // and the frontend together; without this the VITE_* vars are silently
  // absent and the app throws on mount.
  envDir: resolve(__dirname, '../..'),
  // Electron loads the build from disk with a file:// style base.
  base: './',
  // Bind IPv4 explicitly. Vite's default host of "localhost" resolves to ::1
  // on systems that prefer IPv6, and the Electron shell loads 127.0.0.1 — so
  // the dev server would be up and the app still unable to reach it.
  server: { host: '127.0.0.1', port: 8472, strictPort: true },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: true },
})
