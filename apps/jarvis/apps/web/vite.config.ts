import { defineConfig, loadEnv } from 'vite'
import { resolve } from 'path'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig(({ mode }) => {
  // Load .env from repo root (monorepo root contains single .env file)
  const repoRoot = resolve(__dirname, '../../../..')
  loadEnv(mode, repoRoot, '')

  // Detect if running in Docker (service names) or native (localhost)
  const isDocker = process.env.DOCKER === '1' || process.env.NODE_ENV === 'docker'
  const ZERG_BACKEND = isDocker ? 'http://zerg-backend:8000' : 'http://localhost:47300'
  const ZERG_WS = isDocker ? 'ws://zerg-backend:8000' : 'ws://localhost:47300'

  return {
  logLevel: 'warn',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'apple-touch-icon.png', 'maskable-icon-*.png'],
      manifest: {
        name: 'Jarvis - Swarm AI Assistant',
        short_name: 'Jarvis',
        description: 'Voice and text AI assistant powered by Swarm',
        theme_color: '#0a0a0f',
        background_color: '#0a0a0f',
        display: 'standalone',
        start_url: '/chat/',
        scope: '/chat/',
        icons: [
          {
            src: '/chat/maskable-icon-192.png',
            sizes: '192x192',
            type: 'image/png',
            purpose: 'any maskable'
          },
          {
            src: '/chat/maskable-icon-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable'
          }
        ]
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff,woff2}'],
        runtimeCaching: [
          {
            urlPattern: /^https:\/\/fonts\.googleapis\.com\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'google-fonts-cache',
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 24 * 365 // 1 year
              },
              cacheableResponse: {
                statuses: [0, 200]
              }
            }
          },
          {
            urlPattern: /^https:\/\/fonts\.gstatic\.com\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'gstatic-fonts-cache',
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 24 * 365
              },
              cacheableResponse: {
                statuses: [0, 200]
              }
            }
          }
        ]
      }
    })
  ],
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
    // Allow docker service hostnames for e2e testing + production domains
    allowedHosts: ['jarvis-web', 'reverse-proxy', 'localhost', 'swarmlet.com', '.swarmlet.com'],
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
    // HMR config - use nginx proxy port in Docker, direct port in native
    hmr: {
      protocol: 'ws',
      host: 'localhost',
      clientPort: isDocker
        ? (process.env.JARPXY_PORT ? parseInt(process.env.JARPXY_PORT) : 30080)
        : 8080,
    },
    proxy: {
      // Zerg backend (REST + SSE + Jarvis BFF)
      '/api/ws': {
        target: ZERG_WS,
        ws: true,
        changeOrigin: true,
      },
      '/ws': {
        target: ZERG_WS,
        ws: true,
        changeOrigin: true,
      },
      '/api': {
        target: ZERG_BACKEND,
        changeOrigin: true,
      },
    }
  }
  }
})
