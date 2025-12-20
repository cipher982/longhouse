import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AppProvider } from './context'
import App from './App'
import { registerSW } from 'virtual:pwa-register'

// CSS loaded via <link> tags in index.html (prevents FOUC)

// Umami Analytics - env-configurable via Vite (VITE_UMAMI_*)
// Only loads on production domains (not localhost)
const isLocalhost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
const umamiWebsiteId = import.meta.env.VITE_UMAMI_WEBSITE_ID;
const umamiScriptSrc = import.meta.env.VITE_UMAMI_SCRIPT_SRC || "https://analytics.drose.io/script.js";
const umamiDomains = import.meta.env.VITE_UMAMI_DOMAINS;

if (!isLocalhost && umamiWebsiteId) {
  const script = document.createElement("script");
  script.defer = true;
  script.src = umamiScriptSrc;
  script.dataset.websiteId = umamiWebsiteId;
  if (umamiDomains) {
    script.dataset.domains = umamiDomains;
  }
  document.head.appendChild(script);
}

// Register service worker - NO auto-update to prevent refresh loops in prod
// When running Vite dev server (which Coolify uses), HMR causes constant
// "new content" detection, triggering infinite reloads
const updateSW = registerSW({
  onNeedRefresh() {
    // Don't auto-reload - user can manually refresh if they want latest version
    // This prevents infinite refresh loops when running dev server in production
    console.log('[PWA] New content available - refresh page to update')
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
