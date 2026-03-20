const CACHE_NAME = "loop-shell-v1";
const SHELL_URLS = ["/loop", "/site.webmanifest", "/apple-touch-icon.png?v=2", "/maskable-icon-192.png", "/maskable-icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function shouldHandle(request, url) {
  if (request.method !== "GET") return false;
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith("/api/")) return false;

  return (
    url.pathname === "/loop" ||
    url.pathname.startsWith("/loop/") ||
    url.pathname.startsWith("/assets/") ||
    url.pathname === "/site.webmanifest" ||
    url.pathname.endsWith(".png") ||
    url.pathname.endsWith(".ico")
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (!shouldHandle(request, url)) return;

  if (request.mode === "navigate" && (url.pathname === "/loop" || url.pathname.startsWith("/loop/"))) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put("/loop", cloned));
          return response;
        })
        .catch(async () => {
          const cached = await caches.match("/loop");
          return cached || Response.error();
        }),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request)
        .then((response) => {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
          return response;
        })
        .catch(() => cached);
      return cached || networkFetch;
    }),
  );
});
