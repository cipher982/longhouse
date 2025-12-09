import { defineConfig } from 'vitest/config'
import { resolve } from 'path'

export default defineConfig({
  resolve: {
    alias: [
      // Package aliases
      { find: '@jarvis/core', replacement: resolve(__dirname, '../../packages/core/src') },
      { find: '@jarvis/data-local', replacement: resolve(__dirname, '../../packages/data/local/src') },
      // @swarm/config is resolved via workspace (swarm-packages/config symlink)
    ]
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts']
  }
})
