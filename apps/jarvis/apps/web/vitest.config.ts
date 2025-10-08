import { defineConfig } from 'vitest/config'
import { resolve } from 'path'

export default defineConfig({
  resolve: {
    alias: {
      '@jarvis/core': resolve(__dirname, '../../packages/core/src'),
      '@jarvis/data-local': resolve(__dirname, '../../packages/data/local/src')
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts']
  }
})
