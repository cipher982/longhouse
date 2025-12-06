import { defineConfig } from 'vite'
import { resolve } from 'path'

export default defineConfig({
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
    fs: {
      allow: [resolve(__dirname, '..', '..')]
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
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      }
    }
  }
})
