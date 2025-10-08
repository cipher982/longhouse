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
    }
  }
})
