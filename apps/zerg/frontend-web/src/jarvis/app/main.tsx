import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AppProvider } from './context'
import App from './App'

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

// NOTE: PWA/Service Worker support removed - vite-plugin-pwa was never installed
// Add back when PWA is actually needed

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
