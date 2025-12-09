import { defineConfig } from 'vitest/config'
import { resolve } from 'path'

export default defineConfig({
  resolve: {
    alias: {
      '@jarvis/core': resolve(__dirname, '../../packages/core/src'),
      '@jarvis/data-local': resolve(__dirname, '../../packages/data/local/src'),
      // Fix models.json resolution for tests - the relative import in @jarvis/core/model-config.ts
      // uses a path that assumes Docker container layout. This alias makes it work in local tests.
      '../../../config/models.json': resolve(__dirname, '../../../../config/models.json')
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts']
  }
})
