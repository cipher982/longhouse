import { defineConfig } from 'vite'
import { resolve } from 'path'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/chat/',
  // Pre-bundle dependencies from workspace packages
  optimizeDeps: {
    include: ['idb'],
  },
  build: {
    outDir: './dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html')
      }
    }
  },
  resolve: {
    alias: {
      '@jarvis/core': resolve(__dirname, '../../packages/core/src'),
      '@jarvis/data-local': resolve(__dirname, '../../packages/data/local/src')
    }
  },
  server: {
    host: '0.0.0.0',
    port: 8080,
    // Allow docker service hostnames for e2e testing
    allowedHosts: ['jarvis-web', 'localhost'],
    fs: {
      // Allow reading workspace root AND top-level config (models.json)
      allow: [
        resolve(__dirname, '..', '..'),
        resolve(__dirname, '../../..', 'config')
      ]
    },
    // Docker on macOS doesn't propagate filesystem events - must use polling
    watch: {
      usePolling: true,
      interval: 500,
    },
    // HMR config for Docker behind nginx proxy
    hmr: {
      protocol: 'ws',
      host: 'localhost',
      // Browser connects to nginx proxy port, not container port
      clientPort: process.env.JARPXY_PORT ? parseInt(process.env.JARPXY_PORT) : 30080,
    },
    proxy: {
      // Jarvis realtime bridge
      '/api/session': {
        target: 'http://jarvis-server:8787',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/api/tool': {
        target: 'http://jarvis-server:8787',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/api/sync': {
        target: 'http://jarvis-server:8787',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },

      // Zerg backend (REST + SSE)
      '/api/ws': {
        target: 'ws://zerg-backend:8000',
        ws: true,
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://zerg-backend:8000',
        ws: true,
        changeOrigin: true,
      },
      '/api': {
        target: 'http://zerg-backend:8000',
        changeOrigin: true,
      },
    }
  }
})
