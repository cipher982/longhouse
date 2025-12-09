import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AppProvider } from './context'
import App from './App'
import { registerSW } from 'virtual:pwa-register'

// CSS loaded via <link> tags in index.html (prevents FOUC)

// Register service worker with auto-update
const updateSW = registerSW({
  onNeedRefresh() {
    console.log('[PWA] New content available, reloading...')
    updateSW(true)
  },
  onOfflineReady() {
    console.log('[PWA] App ready to work offline')
  },
  onRegisteredSW(swUrl: string, registration: ServiceWorkerRegistration | undefined) {
    console.log('[PWA] Service worker registered:', swUrl)
    if (registration) {
      // Check for updates every hour
      setInterval(() => {
        registration.update()
      }, 60 * 60 * 1000)
    }
  },
  onRegisterError(error: Error) {
    console.error('[PWA] Service worker registration failed:', error)
  }
})

const container = document.getElementById('root')
if (!container) {
  throw new Error('Root element not found')
}

createRoot(container).render(
  <StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </StrictMode>
)
