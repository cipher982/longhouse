import { defineConfig } from 'vitest/config'
import { resolve } from 'path'

export default defineConfig({
  resolve: {
    alias: [
      // Package aliases
      { find: '@jarvis/core', replacement: resolve(__dirname, '../../packages/core/src') },
      { find: '@jarvis/data-local', replacement: resolve(__dirname, '../../packages/data/local/src') },
      // Fix models.json resolution for tests - the relative import in @jarvis/core/model-config.ts
      // uses a path that assumes Docker container layout. This alias makes it work in local tests.
      // Using regex to match the relative path from anywhere
      {
        find: /^\.\.\/\.\.\/\.\.\/config\/models\.json$/,
        replacement: resolve(__dirname, '../../../../config/models.json')
      }
    ]
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts']
  }
})
