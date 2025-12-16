// Runtime configuration for Jarvis frontend
// This file is loaded before the app for analytics injection

// Local dev detection
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

// Umami Analytics - only on production domain
if (!LOCAL_HOSTS.has(window.location.hostname)) {
  const script = document.createElement('script');
  script.defer = true;
  script.src = 'https://analytics.drose.io/script.js';
  script.dataset.websiteId = '486eaa80-2916-41ee-a2a2-f55209495028';
  script.dataset.domains = 'swarmlet.com';
  document.head.appendChild(script);
  console.log("Umami analytics loaded for production domain");
} else {
  console.log("Umami analytics skipped (localhost)");
}
